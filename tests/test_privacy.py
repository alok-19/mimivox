"""Retention expiry, don't-save mode, delete-all, delete hooks and the offline lock."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox import privacy, session_archive
from mimivox.api_adapter import APISessionState, api_state, router
from mimivox.session_control import SessionControl

NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "sessions"
    monkeypatch.setattr(session_archive, "_SESSIONS_DIR", root)
    monkeypatch.setattr(session_archive, "_INDEX_PATH", root / "index.json")
    monkeypatch.setattr(session_archive, "_delete_hooks", [])
    return root


def _archive(sid: str, age_days: float) -> None:
    started = (NOW - timedelta(days=age_days)).isoformat()
    session_archive.archive_session(sid, [{"type": "x"}], started_at=started)


def test_policy_parsing_falls_back():
    assert privacy.normalize_retention("7D") == "7d"
    assert privacy.normalize_retention("bogus") == "forever"
    assert privacy.normalize_retention(None, "1d") == "1d"
    assert privacy.saves_transcripts({"retention_policy": "none"}) is False
    assert privacy.saves_transcripts({}) is True


def test_expiry_uses_injected_clock(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    _archive("old", 10)
    _archive("recent", 2)
    assert privacy.purge_expired("30d", now=NOW) == []
    assert privacy.purge_expired("7d", now=NOW) == ["old"]
    assert [e["id"] for e in session_archive.list_sessions()] == ["recent"]
    assert privacy.purge_expired("7d", now=NOW + timedelta(days=6)) == ["recent"]
    assert session_archive.list_sessions() == []


def test_expiry_skips_malformed_index_ids(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    _archive("old", 10)
    entries = session_archive._read_index()
    entries.append({"id": "../outside", "started_at": (NOW - timedelta(days=10)).isoformat()})
    session_archive.index_path().write_text(json.dumps(entries), encoding="utf-8")

    assert privacy.purge_expired("7d", now=NOW) == ["old"]


def test_expiry_skips_archive_symlink_outside_directory(tmp_path, monkeypatch):
    root = _root(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"id": "sid"}', encoding="utf-8")
    session_archive.session_file("sid").symlink_to(outside)
    session_archive.index_path().write_text(
        json.dumps([{"id": "sid", "started_at": (NOW - timedelta(days=10)).isoformat()}]),
        encoding="utf-8",
    )

    assert privacy.purge_expired("7d", now=NOW) == []
    assert outside.read_text(encoding="utf-8") == '{"id": "sid"}'


def test_forever_and_none_never_expire_existing(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    _archive("ancient", 900)
    assert privacy.purge_expired("forever", now=NOW) == []
    assert privacy.purge_expired("none", now=NOW) == []
    assert session_archive.load_session("ancient") is not None


def test_expiry_removes_derived_exports_and_logs(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    _archive("old", 10)
    exports = tmp_path / "transcripts"
    exports.mkdir()
    (exports / "old.md").write_text("secret")
    (exports / "old.json").write_text("secret")
    logs = tmp_path / "logs"
    logs.mkdir()
    stale = logs / "meeting_v3_log.jsonl"
    stale.write_text("secret")
    old_ts = (NOW - timedelta(days=10)).timestamp()
    os.utime(stale, (old_ts, old_ts))
    privacy.purge_expired("7d", now=NOW)
    assert list(exports.iterdir()) == []
    assert not stale.exists()


def test_delete_session_runs_hooks_and_survives_a_bad_one(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    seen: list[str] = []

    def boom(_sid: str) -> None:
        raise RuntimeError("index down")

    session_archive.register_delete_hook(boom)
    session_archive.register_delete_hook(seen.append)
    _archive("a", 1)
    assert session_archive.delete_session("a") is True
    assert seen == ["a"]


def test_delete_session_rejects_path_tricks(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    (tmp_path / "keep.json").write_text("{}")
    assert session_archive.delete_session("../keep") is False
    assert (tmp_path / "keep.json").exists()


def test_delete_all_history_clears_everything_and_fires_hooks(tmp_path, monkeypatch):
    root = _root(monkeypatch, tmp_path)
    seen: list[str] = []
    session_archive.register_delete_hook(seen.append)
    _archive("a", 1)
    _archive("b", 400)
    (root / "orphan.json").write_text("{}")  # file missing from the index
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "a.md").write_text("secret")
    (tmp_path / "transcripts" / "nosession.md").write_text("secret")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "m_v3_log.jsonl").write_text("secret")
    result = privacy.delete_all_history()
    assert result["sessions"] == 3
    assert sorted(seen) == ["a", "b", "orphan"]
    assert session_archive.list_sessions() == []
    assert [p.name for p in root.iterdir()] == ["index.json"]
    assert list((tmp_path / "transcripts").iterdir()) == []
    assert list((tmp_path / "logs").iterdir()) == []


def _finish_session(state: APISessionState, settings: dict) -> None:
    state.settings = settings
    state.session_id = "sess-1"
    state.control = SessionControl()
    state.handle_pipeline_event({"kind": "session_start", "wav": "x.wav"})
    state.handle_pipeline_event({"kind": "session_end"})


def test_dont_save_mode_never_writes_to_disk(tmp_path, monkeypatch):
    root = _root(monkeypatch, tmp_path)
    _finish_session(APISessionState(), {"retention_policy": "none"})
    assert not root.exists()


def test_default_policy_still_archives(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    _finish_session(APISessionState(), {"retention_policy": "7d"})
    assert session_archive.load_session("sess-1") is not None


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_delete_all_and_settings_validation(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    monkeypatch.setattr("mimivox.api_adapter.save_settings", lambda s: None)
    monkeypatch.setattr("mimivox.api_adapter.refresh_glossary_from_settings", lambda s: None)
    monkeypatch.setattr(api_state, "settings", {"retention_policy": "30d"})
    client = _client()
    r = client.post("/api/settings", json={"retention_policy": "bogus"})
    assert r.json()["settings"]["retention_policy"] == "30d"
    r = client.post("/api/settings", json={"retention_policy": "1d"})
    assert r.json()["settings"]["retention_policy"] == "1d"
    _archive("b", 0.1)
    assert client.delete("/api/sessions").json()["sessions"] == 1
    assert session_archive.list_sessions() == []


def test_changing_policy_expires_immediately(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    monkeypatch.setattr("mimivox.api_adapter.save_settings", lambda s: None)
    monkeypatch.setattr("mimivox.api_adapter.refresh_glossary_from_settings", lambda s: None)
    monkeypatch.setattr(api_state, "settings", {"retention_policy": "forever"})
    session_archive.archive_session(
        "ancient", [], started_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    )
    _client().post("/api/settings", json={"retention_policy": "1d"})
    assert session_archive.list_sessions() == []


def test_sweeper_applies_current_policy(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    session_archive.archive_session(
        "ancient", [], started_at=(datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    )
    sweeper = privacy.RetentionSweeper(lambda: {"retention_policy": "7d"}, interval_s=3600)
    assert sweeper.sweep() == ["ancient"]


# -- offline lock -----------------------------------------------------------


def _reset_offline(monkeypatch):
    monkeypatch.setattr(privacy, "_offline_state", {"locked": False, "reason": "not evaluated"})
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", privacy.OFFLINE_ENV):
        monkeypatch.delenv(name, raising=False)


def test_default_never_blocks_downloads_even_with_models_cached(monkeypatch):
    """Cached speech models must not stop the notes model or a new pair downloading."""
    _reset_offline(monkeypatch)
    monkeypatch.setattr(privacy, "models_cached_locally", lambda s=None: True)
    status = privacy.apply_offline_lock({})
    assert status["offline_locked"] is False
    assert "POST /api/models/download" in status["egress_paths"]
    assert "HF_HUB_OFFLINE" not in os.environ


def test_offline_lock_engages_when_opted_in_and_models_cached(monkeypatch):
    _reset_offline(monkeypatch)
    monkeypatch.setenv(privacy.OFFLINE_ENV, "1")
    monkeypatch.setattr(privacy, "models_cached_locally", lambda s=None: True)
    status = privacy.apply_offline_lock({})
    assert status["offline_locked"] is True
    assert status["network_egress_enabled"] is False
    assert status["egress_paths"] == []
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_offline_lock_stays_open_when_models_missing(monkeypatch):
    _reset_offline(monkeypatch)
    monkeypatch.setenv(privacy.OFFLINE_ENV, "1")
    monkeypatch.setattr(privacy, "models_cached_locally", lambda s=None: False)
    status = privacy.apply_offline_lock({})
    assert status["offline_locked"] is False
    assert status["network_egress_enabled"] is True
    assert "POST /api/models/download" in status["egress_paths"]
    assert "HF_HUB_OFFLINE" not in os.environ


def test_locked_server_refuses_model_downloads_and_reports_status(monkeypatch):
    _reset_offline(monkeypatch)
    monkeypatch.setenv(privacy.OFFLINE_ENV, "1")
    monkeypatch.setattr(privacy, "models_cached_locally", lambda s=None: True)
    privacy.apply_offline_lock({})
    client = _client()
    for cmd in ("download", "update"):
        assert client.post(f"/api/models/{cmd}").status_code == 409
    privacy_block = client.get("/api/status").json()["privacy"]
    assert privacy_block["offline_locked"] is True
    assert privacy_block["on_device"] is True
    assert privacy_block["retention_policy"] in privacy.RETENTION_POLICIES


def test_only_models_service_touches_the_network():
    """Static guard: nothing else in the package may fetch from the network."""
    src = Path(privacy.__file__).parent
    network = re.compile(
        r"^\s*(import|from)\s+(requests|httpx|aiohttp|urllib\.request|http\.client|websockets?)\b"
        r"|snapshot_download|hf_hub_download|urlopen",
        re.M,
    )
    offenders = {
        p.name
        for p in src.glob("*.py")
        if p.name != "models_service.py" and network.search(p.read_text())
    }
    assert offenders == set()


def test_delete_session_treats_glob_characters_literally(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    exports = tmp_path / "transcripts"
    exports.mkdir()
    (exports / "other.md").write_text("keep")
    session_archive.delete_session("*")
    assert (exports / "other.md").exists()


def _tone():
    import numpy as np

    return np.full(1600, 0.1, dtype=np.float32)


def test_dont_save_mode_never_writes_gap_audio(tmp_path, monkeypatch):
    root = _root(monkeypatch, tmp_path)
    state = APISessionState()
    state.settings = {"retention_policy": "none"}
    assert state.save_gap_audio("sess-1", 1, _tone()) is False
    assert not root.exists()


def test_switching_to_dont_save_mid_session_drops_gap_audio(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    state = APISessionState()
    state.settings = {"retention_policy": "7d"}
    assert state.save_gap_audio("sess-1", 1, _tone()) is True
    _finish_session(state, {"retention_policy": "none"})
    assert not session_archive.gaps_dir("sess-1").exists()


def test_discarding_the_live_session_drops_its_gap_audio(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    monkeypatch.setattr(api_state, "settings", {"retention_policy": "7d"})
    monkeypatch.setattr(api_state, "session_id", "sess-1")
    monkeypatch.setattr(api_state, "_archived", False)
    monkeypatch.setattr(api_state, "pipeline_task", None)
    assert api_state.save_gap_audio("sess-1", 1, _tone()) is True
    assert _client().post("/api/session/delete").status_code == 200
    assert not session_archive.gaps_dir("sess-1").exists()


def test_expiry_sweeps_gap_audio_of_sessions_never_archived(tmp_path, monkeypatch):
    _root(monkeypatch, tmp_path)
    _archive("kept", 1)
    for sid in ("kept", "stale", "fresh"):
        session_archive.save_gap_audio(sid, 1, _tone())
    old_ts = (NOW - timedelta(days=10)).timestamp()
    for sid in ("kept", "stale"):
        os.utime(session_archive.gaps_dir(sid), (old_ts, old_ts))
    fresh_ts = NOW.timestamp()
    os.utime(session_archive.gaps_dir("fresh"), (fresh_ts, fresh_ts))
    privacy.purge_expired("7d", now=NOW)
    assert session_archive.gaps_dir("kept").exists()
    assert not session_archive.gaps_dir("stale").exists()
    assert session_archive.gaps_dir("fresh").exists()
