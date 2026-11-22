"""mimivox.privacy — transcript retention and the offline lock.

Retention
  Setting ``retention_policy`` (persisted in settings.json) is one of
  ``forever``, ``30d``, ``7d``, ``1d`` or ``none`` ("don't save transcripts").
  ``none`` keeps sessions off disk. The other policies delete archived
  sessions older than the cutoff, on server start and then periodically.
  Everything derived from a session goes through
  ``session_archive.delete_session``; this module never unlinks session
  files itself.

Offline lock
  Off by default: model downloads always work. Start the server with
  ``MIMIVOX_OFFLINE=1`` to opt in. Once the Whisper and translation models are
  in the local cache, it then sets ``HF_HUB_OFFLINE`` / ``TRANSFORMERS_OFFLINE``
  so nothing can phone home, and gates the only code path that downloads
  (``/api/models/download|update``).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mimivox import session_archive

RETENTION_POLICIES: dict[str, float | None] = {
    "forever": None,
    "30d": 30,
    "7d": 7,
    "1d": 1,
    "none": 0,
}
DEFAULT_RETENTION = "forever"
OFFLINE_ENV = "MIMIVOX_OFFLINE"

_OFFLINE_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")


# -- retention policy -------------------------------------------------------


def normalize_retention(value: Any, fallback: str = DEFAULT_RETENTION) -> str:
    """A valid policy key, or ``fallback`` for anything else."""
    if isinstance(value, str) and value.strip().lower() in RETENTION_POLICIES:
        return value.strip().lower()
    return fallback


def retention_policy(settings: dict[str, Any] | None) -> str:
    return normalize_retention((settings or {}).get("retention_policy"))


def saves_transcripts(settings: dict[str, Any] | None) -> bool:
    return retention_policy(settings) != "none"


def _parse_time(raw: Any, fallback_path: Path | None = None) -> datetime | None:
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if fallback_path is not None:
        try:
            return datetime.fromtimestamp(fallback_path.stat().st_mtime, timezone.utc)
        except OSError:
            return None
    return None


def purge_expired(
    policy: str, *, now: datetime | None = None, clock_mtime: bool = True
) -> list[str]:
    """Delete archived sessions older than the policy allows. Returns ids deleted.

    ``now`` injects the clock. Also sweeps exports and pipeline logs whose
    mtime is past the cutoff, since those hold transcript text too.
    """
    days = RETENTION_POLICIES.get(normalize_retention(policy))
    if not days:  # forever (None) or none (0: nothing new is saved, old ones stay)
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    deleted: list[str] = []
    for entry in session_archive.list_sessions():
        sid = entry.get("id")
        if not sid:
            continue
        try:
            archive_path = session_archive.session_file(sid)
        except ValueError:
            continue
        started = _parse_time(entry.get("started_at"), archive_path)
        if started is not None and started < cutoff and session_archive.delete_session(sid):
            deleted.append(sid)
    if clock_mtime:
        for path in _derived_files():
            started = _parse_time(None, path)
            if started is not None and started < cutoff:
                _unlink(path)
        for gaps in _orphan_gap_dirs():
            started = _parse_time(None, gaps)
            if started is not None and started < cutoff:
                shutil.rmtree(gaps, ignore_errors=True)
    return deleted


def delete_all_history() -> dict[str, int]:
    """Delete every archived session and everything derived from it."""
    ids = {e["id"] for e in session_archive.list_sessions()}
    directory = session_archive.sessions_dir()
    if directory.is_dir():
        ids |= {
            p.stem for p in directory.glob("*.json") if p.name != session_archive.index_path().name
        }
    sessions = sum(1 for sid in sorted(ids) if session_archive.delete_session(sid))
    for orphan in _orphan_gap_dirs():
        shutil.rmtree(orphan, ignore_errors=True)
    leftovers = 0
    for path in _derived_files():
        leftovers += _unlink(path)
    return {"sessions": sessions, "files": sessions + leftovers}


def _derived_files() -> list[Path]:
    """Exports and pipeline logs that hold transcript text outside the archive."""
    base = session_archive.sessions_dir().parent
    found: list[Path] = []
    for sub, pattern in (("transcripts", "*"), ("logs", "*_v3_log.jsonl")):
        folder = base / sub
        if folder.is_dir():
            found.extend(p for p in folder.glob(pattern) if p.is_file())
    # The search index holds transcript text too; drop it so nothing lingers in free pages.
    library = session_archive.library_path()
    found.extend(p for p in library.parent.glob(library.name + "*") if p.is_file())
    return found


def _orphan_gap_dirs() -> list[Path]:
    """Gap audio left by a session that was never archived (crash, discarded run)."""
    directory = session_archive.sessions_dir()
    if not directory.is_dir():
        return []
    found: list[Path] = []
    for path in directory.glob("*.gaps"):
        session_id = path.name.removesuffix(".gaps")
        try:
            archive_path = session_archive.session_file(session_id)
        except ValueError:
            continue
        if not archive_path.exists():
            found.append(path)
    return found


def _unlink(path: Path) -> int:
    try:
        path.unlink()
        return 1
    except OSError:
        return 0


class RetentionSweeper:
    """Background loop that applies the current policy every ``interval_s``."""

    def __init__(self, get_settings, interval_s: float = 3600.0) -> None:
        self._get_settings = get_settings
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sweep(self) -> list[str]:
        return purge_expired(retention_policy(self._get_settings()))

    def start(self) -> None:
        if self._thread is not None:
            return
        self.sweep()

        def _loop() -> None:
            while not self._stop.wait(self._interval):
                with contextlib.suppress(Exception):  # a failed sweep retries next interval
                    self.sweep()

        self._thread = threading.Thread(target=_loop, name="retention-sweeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


# -- offline lock -----------------------------------------------------------

_offline_state: dict[str, Any] = {"locked": False, "reason": "not evaluated"}


def models_cached_locally(settings: dict[str, Any] | None = None) -> bool:
    """True when both the ASR and translation models resolve without network."""
    from mimivox.config import CAT_MODEL_ID, WHISPER_MODEL
    from mimivox.models_service import model_present

    paths = (settings or {}).get("model_paths")
    paths = paths if isinstance(paths, dict) else {}
    ids = (
        str(paths.get("whisper") or WHISPER_MODEL),
        str(paths.get("cat_translate") or CAT_MODEL_ID),
    )
    return all(model_present(model_id)[0] for model_id in ids)


def apply_offline_lock(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """With MIMIVOX_OFFLINE=1, lock the process offline once the models are local.

    Call before loading models.
    """
    if os.environ.get(OFFLINE_ENV) != "1":
        _offline_state.update(locked=False, reason=f"downloads allowed ({OFFLINE_ENV} not set)")
    elif not models_cached_locally(settings):
        _offline_state.update(locked=False, reason="models not cached locally yet")
    else:
        for name in _OFFLINE_ENV:
            os.environ[name] = "1"
        try:  # huggingface_hub reads its flag at import time, so patch it too
            import huggingface_hub.constants as hf_constants

            hf_constants.HF_HUB_OFFLINE = True
        except ImportError:
            pass
        _offline_state.update(locked=True, reason="models cached locally")
    return offline_status()


def offline_locked() -> bool:
    return bool(_offline_state["locked"])


def offline_status() -> dict[str, Any]:
    env_on = all(os.environ.get(name) == "1" for name in _OFFLINE_ENV)
    locked = offline_locked() and env_on
    return {
        "offline_locked": locked,
        "network_egress_enabled": not locked,
        "egress_paths": [] if locked else ["POST /api/models/download", "POST /api/models/update"],
        "reason": _offline_state["reason"],
    }


def privacy_payload(settings: dict[str, Any] | None) -> dict[str, Any]:
    policy = retention_policy(settings)
    return {
        "on_device": True,
        "retention_policy": policy,
        "retention_days": RETENTION_POLICIES[policy],
        **offline_status(),
    }
