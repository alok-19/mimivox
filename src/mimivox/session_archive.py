"""mimivox.session_archive — Persist completed sessions for Review.

Layout under data/sessions/:
  {session_id}.json  — full payload (events, settings, performance_summary)
  index.json         — [{id, started_at, label, event_count}, ...] newest first
  library.sqlite     — derived full-text index (see session_library); rebuildable
  {session_id}.gaps/ — 16 kHz WAVs of speech a catch-up skipped, one per gap id
Tags ("tags": [...]) and a user-set title ("title") live in the session JSON. The index
"headline" is derived: the user's title, else the notes' title, else the summary's
first sentence.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import wave
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mimivox import session_library
from mimivox.config import DATA_DIR, SAMPLE_RATE

_SESSIONS_DIR = Path(DATA_DIR) / "sessions"
_INDEX_PATH = _SESSIONS_DIR / "index.json"
_io_lock = threading.Lock()
_delete_hooks: list[Callable[[str], None]] = []


def register_delete_hook(hook: Callable[[str], None]) -> None:
    """Run ``hook(session_id)`` after a session is deleted.

    Anything derived from a session (a search index, caches) registers here so
    expiry, "delete all history" and DELETE /api/sessions/{id} all clean it up.
    """
    if hook not in _delete_hooks:
        _delete_hooks.append(hook)


def sessions_dir() -> Path:
    return _SESSIONS_DIR


def index_path() -> Path:
    return _INDEX_PATH


def session_file(session_id: str) -> Path:
    return _contained_session_path(session_id, ".json")


def validate_session_id(session_id: str | None) -> str | None:
    if session_id is None:
        return None
    if not _safe_id(session_id):
        raise ValueError("invalid session ID")
    return session_id


def _contained_session_path(session_id: str, suffix: str) -> Path:
    session_id = validate_session_id(session_id)
    if session_id is None:
        raise ValueError("session ID is required")
    return _direct_child(_SESSIONS_DIR, f"{session_id}{suffix}")


def _direct_child(directory: Path, name: str) -> Path:
    path = directory / name
    try:
        try:
            resolved = os.path.realpath(path, strict=True)
        except FileNotFoundError:
            resolved = os.path.realpath(path)
        if resolved != str(directory.resolve() / name):
            raise ValueError("session path must stay inside the sessions directory")
    except (OSError, RuntimeError) as exc:
        raise ValueError("session path must stay inside the sessions directory") from exc
    return path


def library_path() -> Path:
    return _SESSIONS_DIR / "library.sqlite"


def _reindex(session_id: str) -> None:
    """Refresh the search index; never let it break archiving (startup backfill heals)."""
    with contextlib.suppress(Exception):
        session_library.index_file(library_path(), session_file(session_id))


def _remove_from_library(session_id: str) -> None:
    session_library.remove_session(library_path(), session_id)


register_delete_hook(_remove_from_library)


def gaps_dir(session_id: str) -> Path:
    return _contained_session_path(session_id, ".gaps")


def gap_audio_file(session_id: str, gap_id: int) -> Path:
    return _direct_child(gaps_dir(session_id), f"{int(gap_id)}.wav")


def save_gap_audio(session_id: str, gap_id: int, audio: Any) -> bool:
    import numpy as np

    if not _safe_id(session_id):
        return False
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2")
    try:
        gaps_dir(session_id).mkdir(parents=True, exist_ok=True)
        with wave.open(str(gap_audio_file(session_id, gap_id)), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())
    except (OSError, ValueError):
        return False
    return True


def remove_gap_audio(session_id: str) -> None:
    if _safe_id(session_id):
        try:
            shutil.rmtree(gaps_dir(session_id), ignore_errors=True)
        except ValueError:
            return


register_delete_hook(remove_gap_audio)


def _ensure_dir() -> Path:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return _SESSIONS_DIR


def _read_index() -> list[dict[str, Any]]:
    path = _INDEX_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict) and _safe_id(e.get("id"))]
    if isinstance(data, dict) and isinstance(data.get("sessions"), list):
        return [e for e in data["sessions"] if isinstance(e, dict) and _safe_id(e.get("id"))]
    return []


_CAPTION_EVENT_TYPES = frozenset(
    {"AsrCompletedEvent", "TranslationCompletedEvent", "TranscriptRevisionEvent", "CaptionGapEvent"}
)
_HEADLINE_MAX = 80
TITLE_MAX = 120


def _duration_seconds(events: list[dict[str, Any]]) -> float | None:
    marks = []
    for event in events:
        if event.get("type") not in _CAPTION_EVENT_TYPES:
            continue
        mark = event.get("end") if event.get("end") is not None else event.get("start")
        if isinstance(mark, (int, float)):
            marks.append(float(mark))
    return max(marks) if marks else None


def _clip(text: str) -> str:
    return text if len(text) <= _HEADLINE_MAX else text[: _HEADLINE_MAX - 1].rstrip() + "\u2026"


def _headline(data: Any) -> str | None:
    """The meeting's display title, from a session payload."""
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    if isinstance(title, str) and title.strip():
        return " ".join(title.split())
    notes = data.get("notes")
    if not isinstance(notes, dict):
        return None
    generated = notes.get("title_en")
    if isinstance(generated, str) and generated.strip():
        return _clip(" ".join(generated.split()))
    summary = notes.get("summary_en")
    if not isinstance(summary, str) or not summary.strip():
        return None
    text = " ".join(summary.split())
    end = next(
        (i for i, ch in enumerate(text) if ch in ".!?" and text[i + 1 : i + 2] in ("", " ")), None
    )
    return _clip(text if end is None else text[: end + 1])


def _has_summary(entry: dict[str, Any]) -> bool:
    return "duration_seconds" in entry


def _summary_fields(data: dict[str, Any]) -> dict[str, Any]:
    events = [e for e in data.get("events") or [] if isinstance(e, dict)]
    return {"duration_seconds": _duration_seconds(events), "headline": _headline(data)}


def _write_index(entries: list[dict[str, Any]]) -> None:
    _ensure_dir()
    _INDEX_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def archive_session(
    session_id: str,
    events: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    performance_summary: dict[str, Any] | None = None,
    *,
    started_at: str | None = None,
    label: str | None = None,
    pinned_ids: list[int] | None = None,
) -> Path:
    """Write session archive and update index. Returns path to session JSON."""
    if validate_session_id(session_id) is None:
        raise ValueError("session_id required")
    started = started_at or datetime.now(timezone.utc).isoformat()
    lab = (label or "session").strip() or "session"
    event_list = list(events or [])
    previous = load_session(session_id) or {}
    payload = {
        "id": session_id,
        "started_at": started,
        "label": lab,
        "event_count": len(event_list),
        "events": event_list,
        "settings": dict(settings or {}),
        "performance_summary": performance_summary,
        "pinned_ids": sorted(set(pinned_ids or [])),
        "tags": [t for t in previous.get("tags") or [] if isinstance(t, str)],
    }
    if isinstance(previous.get("title"), str):
        payload["title"] = previous["title"]
    with _io_lock:
        _ensure_dir()
        path = session_file(session_id)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        entries = [e for e in _read_index() if e.get("id") != session_id]
        entries.insert(
            0,
            {
                "id": session_id,
                "started_at": started,
                "label": lab,
                "event_count": len(event_list),
                **_summary_fields(payload),
            },
        )
        _write_index(entries)
        _reindex(session_id)
    return path


def list_sessions() -> list[dict[str, Any]]:
    with _io_lock:
        entries = _read_index()
        unsummarised = [e for e in entries if not _has_summary(e)]
        for entry in unsummarised:
            data = load_session(entry["id"])
            entry.update(
                _summary_fields(data) if data else {"duration_seconds": None, "headline": None}
            )
        if unsummarised:
            _write_index(entries)
        return list(entries)


def load_session(session_id: str) -> dict[str, Any] | None:
    try:
        path = session_file(session_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _safe_id(session_id: str) -> bool:
    return (
        isinstance(session_id, str)
        and bool(session_id)
        and session_id.casefold() != "index"
        and "/" not in session_id
        and "\\" not in session_id
        and "\x00" not in session_id
        and not session_id.startswith(".")
    )


def delete_session(session_id: str) -> bool:
    """Delete a session and everything derived from it.

    Removes the archive file, its index entry, exports under data/transcripts/
    named after it, then runs registered delete hooks. True if an archive file
    was removed.
    """
    if not _safe_id(session_id):
        return False
    with _io_lock:
        try:
            path = session_file(session_id)
        except ValueError:
            return False
        existed = path.exists()
        if existed:
            try:
                path.unlink()
            except OSError:
                return False
        all_entries = _read_index()
        entries = [e for e in all_entries if e.get("id") != session_id]
        if existed or len(entries) != len(all_entries):
            _write_index(entries)
    exports = _SESSIONS_DIR.parent / "transcripts"
    if exports.is_dir():
        # Exports are `<id>.<ext>` or `<id>-<what>.<ext>`; another id that merely
        # starts with this one (`<id>-2`) owns its own exports.
        others = [str(e.get("id")) for e in entries if str(e.get("id", "")).startswith(session_id)]
        for export in exports.iterdir():
            name = export.name
            if not name.startswith((f"{session_id}.", f"{session_id}-")):
                continue
            if any(name.startswith((f"{other}.", f"{other}-")) for other in others):
                continue
            with contextlib.suppress(OSError):
                export.unlink()
    for hook in list(_delete_hooks):
        with contextlib.suppress(
            Exception
        ):  # one broken hook must not leave the rest of the session behind
            hook(session_id)
    return existed


def relabel_speaker(session_id: str, speaker_id: str, label: str) -> bool:
    """Rename a speaker across an archived session's events. True if changed."""
    with _io_lock:
        data = load_session(session_id)
        if data is None:
            return False
        try:
            path = session_file(session_id)
        except ValueError:
            return False
        changed = False
        for ev in data.get("events") or []:
            sp = ev.get("speaker") if isinstance(ev, dict) else None
            if isinstance(sp, dict) and sp.get("id") == speaker_id:
                sp["label"] = label
                changed = True
        if changed:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _reindex(session_id)
        return changed


def replace_events(session_id: str, events: list[dict[str, Any]]) -> bool:
    with _io_lock:
        data = load_session(session_id)
        if data is None:
            return False
        data["events"] = events
        data["event_count"] = len(events)
        session_file(session_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        entries = [
            {**e, "event_count": len(events)} if e.get("id") == session_id else e
            for e in _read_index()
        ]
        _write_index(entries)
        _reindex(session_id)
        return True


def save_notes(session_id: str, notes: dict[str, Any]) -> bool:
    """Cache generated notes inside the archive file. True if the session exists."""
    with _io_lock:
        data = load_session(session_id)
        if data is None:
            return False
        data["notes"] = notes
        session_file(session_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _set_index_headline(session_id, _headline(data))
        _reindex(session_id)
        return True


def _set_index_headline(session_id: str, headline: str | None) -> None:
    entries = _read_index()
    indexed = [e for e in entries if e.get("id") == session_id]
    for entry in indexed:
        entry["headline"] = headline
    if indexed:
        _write_index(entries)


def rename_session(session_id: str, title: str) -> str | None:
    """Set a meeting's title; a blank title goes back to the generated one.

    Returns the new headline ("" when there is none yet), None if the session is
    unknown. Raises ValueError for an over-long title.
    """
    clean = " ".join(title.split())
    if len(clean) > TITLE_MAX:
        raise ValueError(f"title is longer than {TITLE_MAX} characters")
    with _io_lock:
        data = load_session(session_id)
        if data is None:
            return None
        if clean:
            data["title"] = clean
        else:
            data.pop("title", None)
        session_file(session_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        headline = _headline(data)
        _set_index_headline(session_id, headline)
        _reindex(session_id)
        return headline or ""


def update_tags(session_id: str, add: list[str] = (), remove: list[str] = ()) -> list[str] | None:
    """Add/remove tags on an archived session. Returns the new tags, None if unknown.

    Raises ValueError for an empty or over-long tag.
    """
    adds = [session_library.normalize_tag(t) for t in add]
    removes = [session_library.normalize_tag(t) for t in remove]
    with _io_lock:
        data = load_session(session_id)
        if data is None:
            return None
        data["tags"] = session_library.merge_tags(
            [t for t in data.get("tags") or [] if isinstance(t, str)], adds, removes
        )
        session_file(session_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _reindex(session_id)
        return data["tags"]


def backfill_index() -> dict[str, int]:
    """Index archives missing from (or changed since) the search index. Idempotent."""
    with _io_lock:
        return session_library.backfill(library_path(), _SESSIONS_DIR)


def search_sessions(**kwargs: Any) -> dict[str, Any]:
    return session_library.search(library_path(), **kwargs)


def set_pins(session_id: str, pinned_ids: list[int]) -> bool:
    """Store an archived session's pinned line ids. True if the session exists."""
    with _io_lock:
        data = load_session(session_id)
        if data is None:
            return False
        data["pinned_ids"] = sorted(set(pinned_ids))
        session_file(session_id).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _reindex(session_id)
        return True
