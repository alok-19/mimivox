"""First-run model setup: state machine, endpoints, gating, sample source."""

from __future__ import annotations

import asyncio
import errno
import io
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox import setup_service
from mimivox.api_adapter import api_state, router
from mimivox.setup_service import (
    Downloading,
    Failed,
    Loading,
    NeedsDownload,
    SetupManager,
    classify_error,
    setup_manager,
)

GB = 1_000_000_000


class FakeProc:
    """Stands in for the worker process: runs until ``finish`` is called."""

    def __init__(self, stderr: str = "") -> None:
        self.returncode: int | None = None
        self.stderr = io.StringIO(stderr)
        self.terminated = False

    def poll(self):
        return self.returncode

    def finish(self, code: int = 0) -> None:
        self.returncode = code

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self) -> None:
        pass


def _manager(*, proc: FakeProc | None = None, **kw):
    proc = proc or FakeProc()
    events: list[dict] = []
    spawned: list[list[str]] = []
    loads: list[int] = []

    def spawn(repos):
        spawned.append(repos)
        return proc

    mgr = SetupManager(
        load_models=lambda: loads.append(1),
        spawn=spawn,
        fetch_sizes=kw.pop("fetch_sizes", lambda repos: {r: GB for r in repos}),
        free_bytes=kw.pop("free_bytes", lambda: 100 * GB),
        poll_s=0.01,
    )
    # Two repos keep the byte arithmetic simple; Haas has its own test.
    mgr.settings_fn = lambda: {"asr_engine": "whisper"}
    mgr.emit = events.append
    return mgr, proc, events, spawned, loads


@pytest.fixture
def cache(monkeypatch):
    """Whether the models are in the HF cache, and how many bytes are on disk per repo."""
    box = {"present": False, "bytes": 0}
    monkeypatch.setattr(
        "mimivox.models_service.model_present", lambda _id: (box["present"], "", None)
    )
    monkeypatch.setattr(setup_service, "cached_bytes", lambda _repo: box["bytes"])
    return box


async def _until(mgr: SetupManager, phase: str, timeout: float = 2.0):
    async def wait():
        while mgr.state.phase != phase:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


async def test_cached_models_load_without_user_action(cache):
    cache["present"] = True
    mgr, _, events, spawned, loads = _manager()
    await mgr.boot()
    await _until(mgr, "ready")
    assert [e["setup"]["phase"] for e in events] == ["loading", "ready"]
    assert loads == [1] and spawned == []


async def test_missing_models_wait_for_consent(cache):
    mgr, _, _, spawned, loads = _manager()
    await mgr.boot()
    await asyncio.sleep(0.05)
    state = mgr.state
    assert isinstance(state, NeedsDownload)
    assert state.total_bytes > 1.5 * GB and state.free_disk_bytes == 100 * GB
    assert spawned == [] and loads == []


async def test_download_reports_bytes_then_loads(cache):
    mgr, proc, events, _, loads = _manager()
    await mgr.boot()
    await mgr.download()
    await _until(mgr, "downloading")
    cache["bytes"] = GB // 2  # per repo: 1 GB of 2 GB total
    await asyncio.sleep(0.1)
    state = mgr.state
    assert isinstance(state, Downloading)
    assert state.total_bytes == 2 * GB and state.done_bytes == GB
    cache["bytes"] = GB
    cache["present"] = True
    proc.finish(0)
    await _until(mgr, "ready")
    phases = [e["setup"]["phase"] for e in events]
    assert phases[0] == "downloading" and phases[-2:] == ["loading", "ready"]
    assert loads == [1]


async def test_progress_has_rate_and_eta(cache):
    mgr, _, events, _, _ = _manager()
    await mgr.boot()
    await mgr.download()
    await _until(mgr, "downloading")
    for step in range(1, 6):
        cache["bytes"] = step * 100_000_000
        await asyncio.sleep(0.03)
    latest = [e["setup"] for e in events if e["setup"]["phase"] == "downloading"][-1]
    assert latest["bytes_per_s"] > 0 and latest["eta_s"] is not None
    assert latest["done_bytes"] <= latest["total_bytes"]
    await mgr.cancel()


async def test_download_is_idempotent_while_running(cache):
    mgr, _, _, spawned, _ = _manager()
    await mgr.boot()
    await mgr.download()
    await _until(mgr, "downloading")
    await mgr.download()
    await mgr.download()
    assert len(spawned) == 1
    await mgr.cancel()


async def test_cancel_stops_worker_and_returns_to_needs_download(cache):
    mgr, proc, _, _, loads = _manager()
    await mgr.boot()
    await mgr.download()
    await _until(mgr, "downloading")
    state = await mgr.cancel()
    assert proc.terminated and isinstance(state, NeedsDownload)
    assert loads == []
    mgr._spawn = lambda repos: FakeProc()
    await mgr.download()  # a fresh run is allowed
    await _until(mgr, "downloading")
    await mgr.cancel()


async def test_cancel_during_spawn_stops_the_worker_it_started(cache):
    mgr, proc, _, _, _ = _manager()
    entered, release = threading.Event(), threading.Event()

    def slow_spawn(repos):
        entered.set()
        release.wait(2)
        return proc

    mgr._spawn = slow_spawn
    await mgr.boot()
    await mgr.download()
    await asyncio.to_thread(entered.wait, 2)
    cancelling = asyncio.create_task(mgr.cancel())
    await asyncio.sleep(0.05)
    release.set()
    state = await cancelling
    assert proc.terminated and isinstance(state, NeedsDownload)


async def test_offline_from_hub_metadata_is_failed_offline(cache):
    def offline(_repos):
        raise ConnectionError("network unreachable")

    mgr, _, _, spawned, _ = _manager(fetch_sizes=offline)
    await mgr.boot()
    await mgr.download()
    await _until(mgr, "failed")
    state = mgr.state
    assert isinstance(state, Failed) and state.reason == "offline"
    assert "internet" in state.message and spawned == []


async def test_offline_from_worker_then_retry_succeeds(cache):
    line = json.dumps({"reason": "offline", "message": "x"})
    first = FakeProc(stderr=line + "\n")
    first.finish(1)
    mgr, _, _, _, loads = _manager(proc=first)
    await mgr.boot()
    await mgr.download()
    await _until(mgr, "failed")
    assert mgr.state.reason == "offline"

    second = FakeProc()
    second.finish(0)
    mgr._spawn = lambda repos: second
    cache["present"] = True
    await mgr.download()
    await _until(mgr, "ready")
    assert loads == [1]


async def test_disk_check_blocks_download(cache):
    mgr, _, _, spawned, _ = _manager(free_bytes=lambda: GB)
    await mgr.boot()
    await mgr.download()
    await _until(mgr, "failed")
    state = mgr.state
    assert state.reason == "disk_full" and "free" in state.message and spawned == []


async def test_already_downloaded_bytes_count_against_disk_need(cache):
    cache["bytes"] = GB  # both repos already complete
    mgr, _, _, spawned, _ = _manager(free_bytes=lambda: 1)
    await mgr.boot()
    await mgr.download()
    await _until(mgr, "downloading")
    assert len(spawned) == 1
    await mgr.cancel()


async def test_load_failure_is_failed_error_not_crash(cache):
    cache["present"] = True
    mgr, *_ = _manager()

    def boom():
        raise RuntimeError("corrupt weights")

    mgr._load_models = boom
    await mgr.boot()
    await _until(mgr, "failed")
    assert mgr.state.reason == "error" and "corrupt weights" in mgr.state.message


async def test_before_load_runs_ahead_of_loading_models(cache):
    cache["present"] = True
    mgr, *_ = _manager()
    order: list[str] = []
    mgr.before_load = lambda: order.append("lock")
    mgr._load_models = lambda: order.append("load")
    await mgr.boot()
    await _until(mgr, "ready")
    assert order == ["lock", "load"]


def test_classify_error():
    assert classify_error(ConnectionError("x")) == "offline"
    assert classify_error(OSError(errno.ENOSPC, "No space left on device")) == "disk_full"
    assert classify_error(ValueError("bad")) == "error"
    try:
        try:
            raise ConnectionError("dns")
        except ConnectionError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert classify_error(outer) == "offline"
    from huggingface_hub.errors import LocalEntryNotFoundError, OfflineModeIsEnabled

    assert classify_error(LocalEntryNotFoundError("not cached")) == "offline"
    assert classify_error(OfflineModeIsEnabled("off")) == "offline"


def test_cached_bytes_counts_incomplete_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_service, "_hub_cache", lambda: tmp_path)
    blobs = tmp_path / "models--org--name" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "a").write_bytes(b"x" * 10)
    (blobs / "b.incomplete").write_bytes(b"x" * 5)
    assert setup_service.cached_bytes("org/name") == 15
    assert setup_service.cached_bytes("org/missing") == 0


# -- endpoints ----------------------------------------------------------------


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _reset_session():
    api_state.pipeline_task = None
    api_state.state = "idle"
    api_state.control = None
    api_state.session_id = None
    api_state.events = []


def test_status_exposes_setup_state():
    setup_manager.reset(NeedsDownload(2 * GB, 50 * GB))
    body = _client().get("/api/status").json()
    assert body["setup"] == {
        "phase": "needs_download",
        "total_bytes": 2 * GB,
        "free_disk_bytes": 50 * GB,
    }


@pytest.mark.parametrize(
    "state",
    [
        Loading(),
        NeedsDownload(GB, GB),
        Downloading(1, 2, 3.0, 4.0),
        Failed("offline", "no network"),
    ],
)
def test_routes_that_need_models_return_409_until_ready(state):
    setup_manager.reset(state)
    _reset_session()
    client = _client()
    for path, body in (
        ("/api/session/start", {"source": "microphone"}),
        ("/api/translate", {"text": "こんにちは"}),
        ("/api/session/notes", {}),
        ("/api/session/ask", {"question": "what?"}),
        ("/api/sessions/any-session/recover", {}),
    ):
        r = client.post(path, json=body)
        assert r.status_code == 409, path
        payload = r.json()
        assert payload["error"] == "models_not_ready"
        assert payload["setup"]["phase"] == state.phase


def test_setup_download_and_cancel_endpoints(monkeypatch):
    calls: list[str] = []

    async def fake_download():
        calls.append("download")
        return Downloading(0, 2 * GB, 0.0, None)

    async def fake_cancel():
        calls.append("cancel")
        return NeedsDownload(2 * GB, 9 * GB)

    monkeypatch.setattr(setup_manager, "download", fake_download)
    monkeypatch.setattr(setup_manager, "cancel", fake_cancel)
    client = _client()
    assert client.post("/api/setup/download").json()["setup"]["phase"] == "downloading"
    assert client.post("/api/setup/cancel").json()["setup"]["phase"] == "needs_download"
    assert calls == ["download", "cancel"]


def test_models_download_route_delegates_to_setup_when_not_ready(monkeypatch):
    from mimivox import privacy

    monkeypatch.setitem(privacy._offline_state, "locked", False)
    setup_manager.reset(NeedsDownload(GB, GB))

    async def fake_download():
        return Downloading(0, GB, 0.0, None)

    monkeypatch.setattr(setup_manager, "download", fake_download)
    r = _client().post("/api/models/download")
    assert r.status_code == 200 and r.json()["setup"]["phase"] == "downloading"


def test_haas_is_downloaded_only_while_selected():
    from mimivox.config import HAAS_MODEL

    mgr, *_ = _manager()
    mgr.settings_fn = dict
    assert HAAS_MODEL in mgr.repo_ids()
    mgr.settings_fn = lambda: {"asr_engine": "whisper"}
    assert HAAS_MODEL not in mgr.repo_ids()
