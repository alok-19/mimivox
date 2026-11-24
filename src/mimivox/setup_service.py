"""mimivox.setup_service — first-run model setup as a state machine.

The server binds its port first; this module then brings the speech and
translation models to ``ready`` in the background.

    needs_download -> downloading -> loading -> ready
          ^                |             |
          +---- cancel ----+             +-> failed(error)
    downloading -> failed(offline | disk_full | error) -> download again

Cached models load on their own (``loading`` -> ``ready``). Missing models wait
in ``needs_download`` until the client calls ``download()``; a multi-GB fetch
never starts without consent. The fetch runs in a child process so cancel can
stop it, and progress is the size of the Hugging Face cache files on disk.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from mimivox.config import CAT_MODEL_ID, HAAS_JA_MODEL, WHISPER_MODEL
from mimivox.models_service import hub_repo_sizes

FailureReason = Literal["offline", "disk_full", "error"]

# Sizes of the pinned models, used when the Hub metadata call is unavailable.
PINNED_MODEL_BYTES: dict[str, int] = {
    "LibraxisAI/whisper-large-v3-turbo-mlx-q8": 916_174_573,
    "hotchpotch/CAT-Translate-1.4b-mlx-q4": 798_693_535,
}
_UNKNOWN_MODEL_BYTES = 1_000_000_000
_DISK_MARGIN = 1.05
_RATE_WINDOW_S = 10.0


@dataclass(frozen=True)
class NeedsDownload:
    total_bytes: int
    free_disk_bytes: int
    phase: Literal["needs_download"] = "needs_download"


@dataclass(frozen=True)
class Downloading:
    done_bytes: int
    total_bytes: int
    bytes_per_s: float
    eta_s: float | None
    phase: Literal["downloading"] = "downloading"


@dataclass(frozen=True)
class Loading:
    phase: Literal["loading"] = "loading"


@dataclass(frozen=True)
class Ready:
    phase: Literal["ready"] = "ready"


@dataclass(frozen=True)
class Failed:
    reason: FailureReason
    message: str
    phase: Literal["failed"] = "failed"


SetupState = NeedsDownload | Downloading | Loading | Ready | Failed


def state_json(state: SetupState) -> dict[str, Any]:
    return asdict(state)


# -- failure classification ---------------------------------------------------


def _error_chain(exc: BaseException):
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def classify_error(exc: BaseException) -> FailureReason:
    """Map a download exception to the reason the UI acts on."""
    names = {c.__name__ for link in _error_chain(exc) for c in type(link).__mro__}
    for link in _error_chain(exc):
        if isinstance(link, OSError) and link.errno in (errno.ENOSPC, errno.EDQUOT):
            return "disk_full"
    offline_names = {
        "OfflineModeIsEnabled",
        "LocalEntryNotFoundError",
        "ConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "Timeout",
        "TimeoutError",
        "TimeoutException",
        "NetworkError",
        "gaierror",
    }
    return "offline" if names & offline_names else "error"


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


def failure_message(reason: FailureReason, total_bytes: int, detail: str = "") -> str:
    if reason == "offline":
        return (
            f"No internet connection. MimiVox needs to download its speech models once "
            f"({_gb(total_bytes)}). Connect to the internet and try again."
        )
    if reason == "disk_full":
        return detail or f"Not enough free disk space for the {_gb(total_bytes)} speech models."
    return f"The model download failed: {detail}" if detail else "The model download failed."


# -- cache and Hub helpers ----------------------------------------------------


def _hub_cache() -> Path:
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE)


def cached_bytes(repo_id: str) -> int:
    """Bytes under the repo's blob dir, finished files and ``.incomplete`` parts."""
    blobs = _hub_cache() / ("models--" + repo_id.replace("/", "--")) / "blobs"
    try:
        return sum(p.stat().st_size for p in blobs.iterdir() if p.is_file())
    except OSError:
        return 0


def spawn_worker(repo_ids: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "mimivox.setup_worker", *repo_ids],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,  # only the failure line; progress bars are off so the pipe never fills
        text=True,
        env={
            **os.environ,
            "HF_HUB_DISABLE_PROGRESS_BARS": "1",
            "MIMIVOX_SERVER_PID": str(os.getpid()),
        },
    )


def _load_all_models() -> None:
    from mimivox.pipeline import load_all_models

    load_all_models()


def _no_op() -> None:
    return None


class SetupManager:
    """Owns the setup state and the tasks that move it forward."""

    def __init__(
        self,
        *,
        load_models: Callable[[], None] = _load_all_models,
        spawn: Callable[[list[str]], Any] = spawn_worker,
        fetch_sizes: Callable[[list[str]], dict[str, int]] = hub_repo_sizes,
        free_bytes: Callable[[], int] | None = None,
        poll_s: float = 0.5,
    ) -> None:
        self._load_models = load_models
        self._spawn = spawn
        self._fetch_sizes = fetch_sizes
        self._free_bytes = free_bytes or self._disk_free
        self._poll_s = poll_s
        self.settings_fn: Callable[[], dict[str, Any]] = dict
        self.emit: Callable[[dict[str, Any]], None] = lambda event: None
        self.before_load: Callable[[], None] = _no_op
        self._state: SetupState = Loading()
        self._task: asyncio.Task | None = None

    # -- state ------------------------------------------------------------

    @property
    def state(self) -> SetupState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return isinstance(self._state, Ready)

    def snapshot(self) -> dict[str, Any]:
        return state_json(self._state)

    def reset(self, state: SetupState) -> None:
        """Set the state without side effects (tests and process startup)."""
        self._state = state

    def _set(self, state: SetupState) -> None:
        self._state = state
        self.emit({"type": "SetupStateEvent", "setup": state_json(state)})

    # -- inputs -----------------------------------------------------------

    def repo_ids(self) -> list[str]:
        """Hub repos the configured models need; local paths need no download."""
        from mimivox.models_service import _is_local_path

        paths = self.settings_fn().get("model_paths")
        paths = paths if isinstance(paths, dict) else {}
        ids = (
            str(paths.get("whisper") or WHISPER_MODEL),
            HAAS_JA_MODEL,
            str(paths.get("cat_translate") or CAT_MODEL_ID),
        )
        return [i for i in ids if not _is_local_path(i)]

    def _missing(self) -> list[str]:
        from mimivox.models_service import model_present

        return [r for r in self.repo_ids() if not model_present(r)[0]]

    def _disk_free(self) -> int:
        cache = _hub_cache()
        while not cache.exists() and cache != cache.parent:
            cache = cache.parent
        return shutil.disk_usage(cache).free

    def _total_bytes(self, repos: list[str]) -> int:
        return sum(PINNED_MODEL_BYTES.get(r, _UNKNOWN_MODEL_BYTES) for r in repos)

    # -- lifecycle --------------------------------------------------------

    async def boot(self) -> None:
        """Pick the first state from the cache. Returns at once; loading runs in a task."""
        missing = await asyncio.to_thread(self._missing)
        if missing:
            self._state = NeedsDownload(self._total_bytes(missing), self._free_bytes())
            return
        self._start(self._load())

    async def download(self) -> SetupState:
        """Start the download. Safe to call again: a running job is left alone."""
        if self._task is not None and not self._task.done():
            return self._state
        if self.is_ready:
            return self._state
        self._start(self._download_then_load())
        return self._state

    async def cancel(self) -> SetupState:
        """Stop a running download. Partial files stay so the next run resumes."""
        task = self._task
        if task is not None and not task.done() and not isinstance(self._state, Loading):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self._state

    async def shutdown(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _start(self, coro) -> None:
        self._task = asyncio.get_running_loop().create_task(coro)

    # -- jobs -------------------------------------------------------------

    async def _load(self) -> None:
        self._set(Loading())
        try:
            self.before_load()
            await asyncio.get_running_loop().run_in_executor(None, self._load_models)
        except Exception as exc:
            self._set(Failed("error", f"The models could not be loaded: {exc}"))
            return
        self._set(Ready())

    async def _download_then_load(self) -> None:
        repos = await asyncio.to_thread(self._missing)
        if not repos:
            await self._load()
            return
        ok = await self._download(repos)
        if ok:
            await self._load()

    async def _download(self, repos: list[str]) -> bool:
        proc = None
        total = self._total_bytes(repos)
        try:
            try:
                sizes = await asyncio.to_thread(self._fetch_sizes, repos)
            except Exception as exc:
                if classify_error(exc) == "offline":
                    self._set(Failed("offline", failure_message("offline", total)))
                    return False
                sizes = {r: PINNED_MODEL_BYTES.get(r, _UNKNOWN_MODEL_BYTES) for r in repos}
            total = sum(sizes.values())
            have = sum(min(cached_bytes(r), sizes[r]) for r in repos)
            free = self._free_bytes()
            if free < (total - have) * _DISK_MARGIN:
                detail = (
                    f"Not enough free disk space. The speech models need {_gb(total - have)} "
                    f"and this Mac has {_gb(free)} free."
                )
                self._set(Failed("disk_full", failure_message("disk_full", total, detail)))
                return False

            self._set(Downloading(have, total, 0.0, None))
            spawning = asyncio.get_running_loop().run_in_executor(None, self._spawn, repos)
            try:
                proc = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                proc = await spawning
                raise
            samples: deque[tuple[float, int]] = deque([(time.monotonic(), have)])
            while proc.poll() is None:
                await asyncio.sleep(self._poll_s)
                now = time.monotonic()
                done = min(sum(cached_bytes(r) for r in repos), total)
                samples.append((now, done))
                while len(samples) > 2 and now - samples[0][0] > _RATE_WINDOW_S:
                    samples.popleft()
                span = now - samples[0][0]
                rate = max(0.0, (done - samples[0][1]) / span) if span > 0 else 0.0
                eta = (total - done) / rate if rate > 0 else None
                self._set(
                    Downloading(done, total, round(rate), None if eta is None else round(eta))
                )
        except asyncio.CancelledError:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                await asyncio.to_thread(proc.wait)
            self._set(NeedsDownload(total, self._free_bytes()))
            raise
        if proc.returncode != 0:
            reason, detail = _worker_failure(proc)
            self._set(Failed(reason, failure_message(reason, total, detail)))
            return False
        return True


def _worker_failure(proc: Any) -> tuple[FailureReason, str]:
    """Read the ``{"reason", "message"}`` line the worker prints on failure."""
    try:
        lines = [ln for ln in (proc.stderr.read() or "").splitlines() if ln.strip()]
        info = json.loads(lines[-1])
        return info["reason"], str(info["message"])
    except Exception:
        return "error", "the download process exited unexpectedly"


setup_manager = SetupManager()
