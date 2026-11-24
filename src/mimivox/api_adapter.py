"""Swift-native /api/* adapter for the MeetingTranslator macOS client.

Maps the minimal MimiVox control + SSE protocol onto the richer contract
expected by macos/MeetingTranslator (see MeetingTranslatorCore/APIClient.swift).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from mimivox import notes_service, session_archive
from mimivox.audio import PushAudioSource
from mimivox.catch_up import apply_catch_up
from mimivox.config import DATA_DIR, shared_mlx_lock
from mimivox.diarization import SpeakerTracker
from mimivox.glossary import find_profile
from mimivox.languages import (
    DEFAULT_PAIR,
    LanguagePair,
    UnsupportedPair,
    pair_catalog,
    resolve_pair,
)
from mimivox.latency_health import CatchUpMode, HealthState, LatencyMonitor, thermal_speed_limit
from mimivox.learning import annotate as annotate_learning
from mimivox.learning import learning_enabled
from mimivox.learning import warm as warm_learning
from mimivox.pair_translate import unready_reason
from mimivox.pipeline import run
from mimivox.privacy import (
    OFFLINE_ENV,
    delete_all_history,
    normalize_retention,
    offline_locked,
    privacy_payload,
    purge_expired,
    retention_policy,
    saves_transcripts,
)
from mimivox.session_control import SessionControl
from mimivox.settings_store import (
    SYSTEM_AUDIO_MISSING_MESSAGE,
    apply_settings_for_session,
    cloud_status_message,
    default_settings,
    get_effective_glossary,
    load_settings,
    merge_settings,
    model_path_status,
    refresh_glossary_from_settings,
    resolve_asr_model_live,
    resolve_microphone_device,
    resolve_replay_duration_s,
    resolve_system_audio_device,
    save_settings,
)
from mimivox.settings_store import (
    catch_up_mode as _catch_up_mode,
)
from mimivox.settings_store import (
    partial_en_enabled as _partial_en_enabled,
)
from mimivox.setup_service import setup_manager, state_json

router = APIRouter(prefix="/api")

_DEFAULT_REPLAY = str(Path(DATA_DIR) / "fixtures" / "meeting-recording.wav")
_AUDIO_LEVEL_INTERVAL_S = 0.1
_HEALTH_INTERVAL_S = 1.0
_MAX_NATIVE_AUDIO_BODY_BYTES = 8 * 1024 * 1024


async def _read_bounded_request_body(request: Request, max_bytes: int) -> bytes | None:
    content_length = request.headers.get("content-length", "")
    if content_length.isdecimal() and int(content_length) > max_bytes:
        return None

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            return None
        body.extend(chunk)
    return bytes(body)


def _default_settings() -> dict[str, Any]:
    return default_settings()


def _default_model_readiness(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build model_readiness from live HF-cache verify + glossary count."""
    settings = settings if settings is not None else default_settings()
    from mimivox.models_service import get_download_state
    from mimivox.models_service import verify as verify_models

    readiness = verify_models(settings)
    glossary = get_effective_glossary(settings)
    # Preserve path-mismatch notes from settings_store when verify omits them.
    paths = model_path_status(settings)
    for step in paths.get("next_steps") or []:
        if step not in readiness["next_steps"]:
            readiness["next_steps"].append(step)
    readiness["glossary"] = {
        "ready": True,
        "message": f"{len(glossary)} effective terms",
        "display_name": "Glossary",
        "term_count": len(glossary),
    }
    dl = get_download_state()
    if dl.get("busy"):
        readiness["downloading"] = True
        readiness["ready"] = False
        if "loading_models" not in readiness["next_steps"]:
            readiness["next_steps"].insert(0, "loading_models")
        readiness["message"] = dl.get("message") or "Downloading models…"
    return readiness


def _default_health(*, active: bool = False) -> dict[str, Any]:
    return {
        "audio_input_active": active,
        "asr_worker_active": active,
        "translation_worker_active": active,
        "queue_lag_s": 0.0,
        "realtime_lag_s": 0.0,
        "dropped_stale_chunks": 0,
        "dropped_silence_chunks": 0,
        "active_speech": False,
        "model_ready": True,
        "degraded": False,
        "session_label": "Listening" if active else "Ready",
        "audio_label": "Listening" if active else "Idle",
        "asr_label": "Live" if active else "Idle",
        "translation_label": "Live" if active else "Idle",
        "catching_up": False,
        "stopped": not active,
        "latency": LatencyMonitor().snapshot().as_dict(),
    }


class APISessionState:
    """In-memory session/settings state shared by /api/* routes."""

    def __init__(self) -> None:
        self.pipeline_task: asyncio.Task | None = None
        self.session_id: str | None = None
        self.state: str = "idle"
        self.error: str | None = None
        self.events: list[dict[str, Any]] = []
        self.settings: dict[str, Any] = _default_settings()
        self._event_index: int = 0
        self._clients: list[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._session_start_perf: float | None = None
        self.control: SessionControl | None = None
        # Set for system-audio sessions captured natively by the macOS app.
        self.audio_push: PushAudioSource | None = None
        # Per-session speaker tracker (None when the model is unavailable).
        self.speakers: SpeakerTracker | None = None
        self._meter_task: asyncio.Task | None = None
        self._models_task: asyncio.Task | None = None
        self.session_label: str | None = None
        self.session_pair: LanguagePair = DEFAULT_PAIR
        self.started_at_iso: str | None = None
        self._archived: bool = False
        # Line (ASR) indexes the viewer pinned this session; archived and exported.
        self.pinned_ids: list[int] = []
        # Notes for a session not yet archived: {session_id: {"line_count", "notes"}}.
        self.notes_cache: dict[str, dict[str, Any]] = {}
        # Notes being written for a meeting that just ended: {session_id: task}.
        self.auto_notes: dict[str, asyncio.Task] = {}

    def load_persisted_settings(self) -> dict[str, Any]:
        """Load settings from disk and refresh the active glossary."""
        self.settings = load_settings()
        refresh_glossary_from_settings(self.settings)
        return self.settings

    # -- client fan-out -----------------------------------------------------

    def add_client(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._clients.append(q)

    def remove_client(self, q: asyncio.Queue) -> None:
        with self._lock, contextlib.suppress(ValueError):
            self._clients.remove(q)

    def _push(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False)
        with self._lock:
            clients = list(self._clients)
        dead: list[asyncio.Queue] = []
        for q in clients:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.remove_client(q)

    def emit(self, event: dict[str, Any], *, store: bool = True) -> None:
        if store:
            self.events.append(event)
        self._push(event)

    def next_index(self) -> int:
        self._event_index += 1
        return self._event_index

    def _maybe_archive(self) -> None:
        """Persist completed session for Review (once per session_id)."""
        if self._archived:
            return
        sid = self.session_id
        if not sid:
            return
        if not saves_transcripts(self.settings):
            session_archive.remove_gap_audio(sid)
            return
        try:
            from mimivox.session_archive import archive_session

            perf = self.control.performance_summary() if self.control is not None else None
            archive_session(
                sid,
                list(self.events),
                {
                    **self.settings,
                    "source_lang": self.session_pair.source,
                    "target_lang": self.session_pair.target,
                },
                perf,
                started_at=self.started_at_iso,
                label=self.session_label or "session",
                pinned_ids=list(self.pinned_ids),
            )
            self._archived = True
        except Exception:
            # Archive must never break session teardown.
            pass

    def _start_auto_notes(self) -> None:
        """Write notes, and so the meeting's title, once a saved meeting ends.

        Runs only when the optional notes model is on disk. The notes go into
        the archive, and a ``NotesReadyEvent`` tells clients to refresh.
        """
        from mimivox import meeting_notes
        from mimivox.config import NOTES_MODEL_ID
        from mimivox.models_service import model_present

        sid = self.session_id
        if not (sid and self._archived) or sid in self.auto_notes:
            return
        lines = meeting_notes.build_lines(self.events)
        if len(lines) < AUTO_NOTES_MIN_LINES or not model_present(NOTES_MODEL_ID)[0]:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._write_auto_notes(sid, lines))
        self.auto_notes[sid] = task
        task.add_done_callback(lambda _t: self.auto_notes.pop(sid, None))

    async def _write_auto_notes(self, sid: str, lines: list[Any]) -> None:
        try:
            notes, _meta = await notes_service.notes_for(
                lines, priority=notes_service.Priority.AFTER_MEETING
            )
        except Exception:
            return  # Notes stay one click away in Meetings; a failure here is silent.
        if session_archive.save_notes(sid, notes.to_dict()):
            self.emit({"type": "NotesReadyEvent", "message": sid, "timestamp_s": 0.0}, store=False)

    def save_gap_audio(self, session_id: str, gap_id: int, audio: Any) -> bool:
        return saves_transcripts(self.settings) and session_archive.save_gap_audio(
            session_id, gap_id, audio
        )

    def rename_speaker(self, speaker_id: str, label: str) -> bool:
        """Rename a speaker for this session: later lines, stored rows, archive."""
        label = label.strip()
        if self.speakers is None or not self.speakers.rename(speaker_id, label):
            return False
        for ev in self.events:
            sp = ev.get("speaker")
            if isinstance(sp, dict) and sp.get("id") == speaker_id:
                sp["label"] = label
        if self.session_id and self._archived:
            from mimivox.session_archive import relabel_speaker

            relabel_speaker(self.session_id, speaker_id, label)
        self.emit(
            {
                "type": "SpeakerRenamedEvent",
                # message keeps each rename distinct for clients that dedupe by event id.
                "message": f"{speaker_id}={label}",
                "speaker": {"id": speaker_id, "label": label},
                "timestamp_s": self._parse_ts_seconds(None),
            },
            store=False,
        )
        return True

    # -- status -------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return bool(self.pipeline_task and not self.pipeline_task.done())

    def status_payload(self, include_events: bool = True) -> dict[str, Any]:
        active = self.is_running
        if active and self.state in ("idle", "completed", "failed"):
            state = "listening"
        else:
            state = self.state

        if self.control is not None and active:
            health = self.control.health_payload(state=state, model_ready=True)
        else:
            health = _default_health(active=active)
            if state == "completed":
                health["session_label"] = "Stopped"
                health["stopped"] = True
            elif state == "failed":
                health["session_label"] = "Failed"
                health["degraded"] = True
                health["stopped"] = True
            elif state == "paused":
                health["session_label"] = "Paused"
                health["audio_label"] = "Paused"
                health["asr_label"] = "Paused"
                health["translation_label"] = "Paused"

        perf = None
        if self.control is not None and (
            active or self.control.asr_count > 0 or self.control.translation_count > 0
        ):
            perf = self.control.performance_summary()

        # Only surface errors while failed or still running. Sticky faults from a
        # previous completed/idle session must not keep the UI in "Needs attention".
        if not active and state in ("completed", "idle") and self.error is not None:
            self.error = None
        exposed_error = self.error if (state == "failed" or active) else None

        payload: dict[str, Any] = {
            "state": state,
            "session_id": self.session_id,
            # Only expose events while a session is active — after session_end
            # events are cleared, but guard defensively against any retained state
            # (e.g. failed session) leaking stale prior-session data to clients.
            "events": list(self.events) if active and include_events else [],
            "settings": self.settings,
            "setup": setup_manager.snapshot(),
            "model_readiness": _default_model_readiness(self.settings),
            "privacy": privacy_payload(self.settings),
            "health": health,
            "performance_summary": perf,
            "error": exposed_error,
        }
        cloud_msg = cloud_status_message(self.settings)
        if cloud_msg:
            payload["message"] = cloud_msg
        return payload

    def start_meter_loop(self) -> None:
        """Emit AudioLevelEvent at a fixed interval while the session runs."""
        self.stop_meter_loop()

        async def _loop() -> None:
            try:
                last_health = 0.0
                while self.is_running and self.control is not None:
                    now = time.monotonic()
                    if now - last_health >= _HEALTH_INTERVAL_S:
                        last_health = now
                        self._tick_health()
                    level = self.control.get_audio_level()
                    ts = 0.0
                    if self._session_start_perf is not None:
                        ts = max(0.0, time.perf_counter() - self._session_start_perf)
                    self.emit(
                        {
                            "type": "AudioLevelEvent",
                            "level": round(level, 3),
                            "timestamp_s": round(ts, 3),
                        },
                        store=False,
                    )
                    await asyncio.sleep(_AUDIO_LEVEL_INTERVAL_S)
            except asyncio.CancelledError:
                pass

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._meter_task = loop.create_task(_loop())

    def _tick_health(self) -> None:
        control = self.control
        if control is None:
            return
        snap = control.latency_snapshot()
        if (
            _catch_up_mode(self.settings) == "auto"
            and snap.state is HealthState.OVERLOADED
            and not control.catch_up_active
            and apply_catch_up(control) is not None
        ):
            snap = control.latency_snapshot()
        self.emit(
            {
                "type": "LatencyHealthEvent",
                "health": snap.as_dict(),
                "timestamp_s": round(self._parse_ts_seconds(None), 3),
            },
            store=False,
        )

    def stop_meter_loop(self) -> None:
        if self._meter_task is not None and not self._meter_task.done():
            self._meter_task.cancel()
        self._meter_task = None

    # -- mimivox event → Swift MeetingEvent ---------------------------------

    def _parse_ts_seconds(self, ts: Any) -> float:
        if isinstance(ts, (int, float)):
            return float(ts)
        if isinstance(ts, str) and ":" in ts:
            try:
                mm, ss = ts.split(":", 1)
                return int(mm) * 60 + float(ss)
            except ValueError:
                pass
        if self._session_start_perf is not None:
            return max(0.0, time.perf_counter() - self._session_start_perf)
        return 0.0

    def handle_pipeline_event(self, event: dict[str, Any]) -> None:
        """Convert a pipeline event into API events."""
        annotate_learning(event, self.settings)
        kind = event.get("kind")

        if kind == "session_start":
            self.state = "listening"
            self.error = None
            self.events = []
            self.pinned_ids = []
            self._event_index = 0
            self._session_start_perf = time.perf_counter()
            self.emit(
                {
                    "type": "SessionStartedEvent",
                    "message": f"source={event.get('wav', 'live')}",
                    "timestamp_s": 0.0,
                },
                store=True,
            )
            return

        if kind == "session_end":
            self.state = "completed"
            self._session_start_perf = None
            self.emit(
                {
                    "type": "SessionEndedEvent",
                    "message": "session complete",
                    "timestamp_s": self._parse_ts_seconds(None),
                },
                store=True,
            )
            self._maybe_archive()
            self._start_auto_notes()
            # Drop stored events after archive so a late SSE connect cannot
            # replay the prior session into a freshly cleared transcript panel.
            self.events = []
            return

        if kind == "partial":
            # Partials are preview-only; Swift timeline keys on completed ASR.
            # Emit as a soft preview event that the UI can ignore safely.
            start = self._parse_ts_seconds(event.get("ts"))
            self.emit(
                {
                    "type": "AsrPartialEvent",
                    "text": event.get("ja") or "",
                    "start": start,
                    "timestamp_s": start,
                },
                store=False,
            )
            return

        if kind == "translating":
            if self.state != "stopping":
                self.state = "processing"
            start = self._parse_ts_seconds(event.get("ts"))
            # Store so a client that connects mid-utterance (or right after start)
            # still sees translation phase on SSE replay — needed for Replay mode.
            self.emit(
                {
                    "type": "TranslationStartedEvent",
                    "text": event.get("ja") or "",
                    "start": start,
                    "timestamp_s": start,
                },
                store=True,
            )
            return

        if kind == "en_token":
            # Token streaming not modeled in Swift MeetingEvent; skip.
            return

        if kind in ("partial_en_token", "partial_en"):
            # Streamed EN preview during ASR. Preview-only: not stored on the
            # Swift timeline (final TranslationCompletedEvent is authoritative).
            start = self._parse_ts_seconds(event.get("ts"))
            self.emit(
                {
                    "type": "PartialEnEvent",
                    "text": event.get("text") or event.get("en") or "",
                    "partial_token": kind == "partial_en_token",
                    "start": start,
                    "timestamp_s": start,
                },
                store=False,
            )
            return

        if kind == "final":
            # Do not un-pause the session label when a final lands during pause.
            if self.state not in ("paused", "stopping"):
                self.state = "listening" if self.is_running else "completed"
            # Pipeline utterance id doubles as the row index so a later revision
            # event can address this row; older senders fall back to a counter.
            idx = event.get("id")
            if isinstance(idx, int):
                self._event_index = max(self._event_index, idx)
            else:
                idx = self.next_index()
            start = self._parse_ts_seconds(event.get("ts"))
            total_ms = event.get("total_ms") or 0
            display_latency_s = (total_ms / 1000.0) if total_ms else None
            asr_ms = event.get("asr_ms")
            translate_ms = event.get("translate_ms")
            asr_latency_s = (asr_ms / 1000.0) if asr_ms is not None else None
            translate_latency_s = (translate_ms / 1000.0) if translate_ms is not None else None
            vad_wait_s = (event.get("vad_wait_ms") or 0) / 1000.0
            queue_wait_s = (event.get("queue_wait_ms") or 0) / 1000.0
            perceived_s = (display_latency_s or 0.0) + vad_wait_s + queue_wait_s
            ja = event.get("ja") or ""
            en = event.get("en") or ""
            duration = event.get("duration_s")
            try:
                duration_f = float(duration) if duration is not None else 0.0
            except (TypeError, ValueError):
                duration_f = 0.0
            end = start + duration_f if duration_f > 0 else start
            speaker = event.get("speaker")
            speaker_field = {"speaker": dict(speaker)} if isinstance(speaker, dict) else {}
            confidence = event.get("confidence")
            confidence_field = (
                {"confidence": dict(confidence)} if isinstance(confidence, dict) else {}
            )
            tokens_field = (
                {"tokens": event["tokens"]} if isinstance(event.get("tokens"), list) else {}
            )

            if ja:
                asr_ev: dict[str, Any] = {
                    "type": "AsrCompletedEvent",
                    "index": idx,
                    "start": start,
                    "end": end,
                    "text": ja,
                    "timestamp_s": start,
                    **speaker_field,
                    **confidence_field,
                    **tokens_field,
                }
                if event.get("translation_skipped"):
                    asr_ev["translation_skipped"] = event["translation_skipped"]
                elif not en:
                    asr_ev["translation_empty"] = True
                if asr_latency_s is not None:
                    asr_ev["asr_latency_s"] = asr_latency_s
                self.emit(asr_ev)
            if en:
                tr_ev: dict[str, Any] = {
                    "type": "TranslationCompletedEvent",
                    "index": idx,
                    "start": start,
                    "end": end,
                    "text": en,
                    "source_indexes": [idx],
                    "timestamp_s": start,
                    "display_latency_s": display_latency_s,
                    "is_late": bool(display_latency_s and display_latency_s > 2.0),
                    "vad_wait_s": vad_wait_s,
                    "queue_wait_s": queue_wait_s,
                    "perceived_latency_s": perceived_s,
                    **speaker_field,
                    **confidence_field,
                }
                if asr_latency_s is not None:
                    tr_ev["asr_latency_s"] = asr_latency_s
                if translate_latency_s is not None:
                    tr_ev["translate_latency_s"] = translate_latency_s
                self.emit(tr_ev)
            return

        if kind == "revision":
            # An earlier row's JA/EN was rewritten (VAD split a sentence).
            start = self._parse_ts_seconds(event.get("ts"))
            duration_f = float(event.get("duration_s") or 0.0)
            confidence = event.get("confidence")
            self.emit(
                {
                    "type": "TranscriptRevisionEvent",
                    "index": event.get("target_id"),
                    "start": start,
                    "end": start + duration_f if duration_f > 0 else start,
                    "text": event.get("en") or "",
                    "ja": event.get("ja") or "",
                    "merged_indexes": list(event.get("merged_ids") or []),
                    "reason": event.get("reason") or "merged_fragment",
                    "timestamp_s": start,
                    **({"confidence": dict(confidence)} if isinstance(confidence, dict) else {}),
                    **(
                        {"tokens": event["tokens"]} if isinstance(event.get("tokens"), list) else {}
                    ),
                },
                store=True,
            )
            return

        if kind == "gap":
            start = self._parse_ts_seconds(event.get("ts"))
            end = float(event.get("end") or start)
            self.emit(
                {
                    "type": "CaptionGapEvent",
                    "index": event.get("id"),
                    "start": start,
                    "end": end,
                    "reason": event.get("reason") or "catch_up",
                    "recoverable": bool(event.get("recoverable")),
                    "message": f"Skipped {end - start:.0f}s of speech while catching up",
                    "timestamp_s": start,
                },
                store=True,
            )
            idx = event.get("id")
            if isinstance(idx, int):
                self._event_index = max(self._event_index, idx)
            return

        if kind == "error":
            self.error = event.get("detail") or "pipeline error"
            self.emit(
                {
                    "type": "ErrorEvent",
                    "message": self.error,
                    "timestamp_s": self._parse_ts_seconds(event.get("ts")),
                }
            )
            return


# Module-level singleton; server.py injects pipeline_task ownership here.
api_state = APISessionState()
notes_service.meeting_is_live = lambda: api_state.is_running


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
AUTO_NOTES_MIN_LINES = 3


def models_not_ready() -> JSONResponse | None:
    """A 409 for routes that need the models while setup is not ``ready``."""
    if setup_manager.is_ready:
        return None
    return JSONResponse(
        {
            "error": "models_not_ready",
            "message": "The speech models are not ready yet. Finish model setup first.",
            "setup": setup_manager.snapshot(),
        },
        status_code=409,
    )


@router.post("/setup/download")
async def api_setup_download():
    return JSONResponse({"setup": state_json(await setup_manager.download())})


@router.post("/setup/cancel")
async def api_setup_cancel():
    return JSONResponse({"setup": state_json(await setup_manager.cancel())})


@router.get("/status")
async def api_status(events: bool = True):
    return JSONResponse(api_state.status_payload(include_events=events))


@router.get("/events")
async def api_events(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    api_state.add_client(q)

    q.put_nowait(json.dumps({"type": "SetupStateEvent", "setup": setup_manager.snapshot()}))

    # Replay stored events so a late-connecting client sees the current
    # active session. Only replay while a session is running — after
    # session_end events are cleared, but guard defensively so a stale
    # or failed session never replays old data to a fresh connection.
    if api_state.is_running:
        for ev in list(api_state.events):
            try:
                q.put_nowait(json.dumps(ev, ensure_ascii=False))
            except asyncio.QueueFull:
                break

    async def event_generator() -> AsyncGenerator[str, None]:
        yield ": connected\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            api_state.remove_client(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/languages")
async def api_languages():
    """Supported language pairs for pickers; ``ready`` is false while the
    on-device translation model a pair needs is not downloaded."""
    return JSONResponse(
        {
            "pairs": [
                {**row, "ready": unready_reason(LanguagePair(row["source"], row["target"])) is None}
                for row in pair_catalog()
            ],
            "selected": {
                "source": resolve_pair(api_state.settings).source,
                "target": resolve_pair(api_state.settings).target,
            },
        }
    )


@router.get("/settings")
async def api_get_settings():
    return JSONResponse({"settings": api_state.settings})


@router.post("/settings")
async def api_post_settings(body: dict):
    # Accept either bare settings or {settings: {...}}
    incoming = body.get("settings") if isinstance(body.get("settings"), dict) else body
    previous_retention = retention_policy(api_state.settings)
    if isinstance(incoming, dict):
        api_state.settings = merge_settings(api_state.settings, incoming)
        # Persist under data/settings.json; refresh glossary immediately.
        api_state.settings["retention_policy"] = normalize_retention(
            api_state.settings.get("retention_policy"), previous_retention
        )
        save_settings(api_state.settings)
        refresh_glossary_from_settings(api_state.settings)
        if learning_enabled(api_state.settings):
            threading.Thread(target=warm_learning, daemon=True).start()
        purge_expired(retention_policy(api_state.settings))
    resp: dict[str, Any] = {"settings": api_state.settings}
    cloud_msg = cloud_status_message(api_state.settings)
    if cloud_msg:
        resp["message"] = cloud_msg
    return JSONResponse(resp)


@router.get("/devices")
async def api_devices():
    try:
        import sounddevice as sd
    except ImportError:
        return JSONResponse({"input_devices": []})
    inputs: list[dict[str, str]] = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            raw = d["name"]
            clean = raw.split(",")[0].strip() if raw else f"Device {i}"
            inputs.append({"id": str(i), "name": clean})
    return JSONResponse({"input_devices": inputs})


@router.post("/session/start")
async def api_session_start(body: dict):
    if api_state.is_running:
        return JSONResponse({"message": "already running", "session_id": api_state.session_id})
    if (not_ready := models_not_ready()) is not None:
        return not_ready

    source = (body.get("source") or "microphone").strip()
    capture = (body.get("capture") or "").strip()
    settings = api_state.settings

    # Optional per-meeting glossary: absent -> settings' active profile,
    # "" -> none, unknown name -> 400 rather than silently running without it.
    glossary_profile = body.get("glossary_profile")
    if (
        glossary_profile is not None
        and glossary_profile != ""
        and (
            not isinstance(glossary_profile, str)
            or find_profile(settings, glossary_profile) is None
        )
    ):
        return JSONResponse(
            {"message": f"Unknown glossary profile: {glossary_profile}", "session_id": None},
            status_code=400,
        )

    try:
        pair = resolve_pair(settings, body)
    except UnsupportedPair as exc:
        return JSONResponse({"message": str(exc), "session_id": None}, status_code=400)
    blocked = unready_reason(pair)
    if blocked:
        return JSONResponse({"message": blocked, "session_id": None}, status_code=400)

    live = False
    wav: str | None = None
    device: int | str | None = None
    max_duration_s: float | None = None
    audio_push: PushAudioSource | None = None

    if source == "microphone":
        live = True
        device = resolve_microphone_device(settings)
    elif source == "system-audio" and capture == "native":
        # The client captures system output itself (Core Audio process tap)
        # and streams PCM to /api/session/audio; no loopback driver needed.
        live = True
        audio_push = PushAudioSource()
    elif source == "system-audio":
        live = True
        device = resolve_system_audio_device(settings)
        if device is None:
            return JSONResponse(
                {
                    "message": SYSTEM_AUDIO_MISSING_MESSAGE,
                    "session_id": None,
                },
                status_code=400,
            )
    elif source == "replay-file":
        wav = settings.get("replay_audio_path") or _DEFAULT_REPLAY
        max_duration_s = resolve_replay_duration_s(settings)
    else:
        # Treat unknown source as a WAV path (handy for direct paths).
        candidate = Path(source)
        if candidate.exists():
            wav = str(candidate)
            max_duration_s = resolve_replay_duration_s(settings)
        else:
            return JSONResponse(
                {"message": f"Unknown source: {source}", "session_id": None},
                status_code=400,
            )

    if wav and not Path(wav).exists():
        return JSONResponse(
            {"message": f"WAV not found: {wav}", "session_id": None},
            status_code=400,
        )

    # Session-start-only: latency VAD/chunk presets + glossary refresh.
    # Cloud toggles are stored but never enable network translation.
    apply_settings_for_session(
        {**settings, "source_lang": pair.source, "target_lang": pair.target}, glossary_profile
    )

    api_state.session_pair = pair
    api_state.session_id = str(uuid.uuid4())
    api_state.state = "listening"
    api_state.error = None
    api_state.events = []
    api_state.pinned_ids = []
    api_state._event_index = 0
    api_state.control = SessionControl()
    api_state.control.monitor.gpu_probe = notes_service.holding_gpu
    api_state.control.monitor.thermal_probe = thermal_speed_limit
    sid = api_state.session_id
    api_state.control.gap_sink = lambda gap_id, audio: api_state.save_gap_audio(sid, gap_id, audio)
    api_state.control.audio_input_active = live
    api_state.audio_push = audio_push
    api_state.speakers = SpeakerTracker.create()
    api_state.session_label = Path(wav).name if wav else source
    api_state.started_at_iso = datetime.now(timezone.utc).isoformat()
    api_state._archived = False

    if pair.uses_cat:
        notes_service.release_for_meeting()  # captions get the notes model's memory

    api_state.handle_pipeline_event(
        {
            "kind": "session_start",
            "wav": wav or source,
            "source_lang": pair.source,
            "target_lang": pair.target,
        }
    )

    api_state.pipeline_task = asyncio.create_task(
        run(
            wav_path=wav,
            live=live,
            asr_only=False,
            audio_device=device,
            sse_broadcast=api_state.handle_pipeline_event,
            control=api_state.control,
            max_duration_s=max_duration_s,
            asr_model_id=resolve_asr_model_live(settings) if live else None,
            partial_en_enabled=_partial_en_enabled(settings),
            save_log=saves_transcripts(settings),
            show_transcript=False,
            audio_push=audio_push,
            speaker_tracker=api_state.speakers,
            pair=pair,
        )
    )
    api_state.start_meter_loop()

    def _on_done(task: asyncio.Task) -> None:
        api_state.stop_meter_loop()
        if api_state.audio_push is audio_push:
            api_state.audio_push = None
        try:
            task.result()
        except asyncio.CancelledError:
            api_state.state = "completed"
            # Normal stop/cancel — do not keep mid-session faults as a sticky alarm.
            api_state.error = None
        except Exception:
            api_state.state = "failed"
            api_state.error = "The audio or translation service stopped. Start a new session."
        else:
            if api_state.state not in ("failed",):
                api_state.state = "completed"
                api_state.error = None
        api_state.handle_pipeline_event({"kind": "session_end"})

    api_state.pipeline_task.add_done_callback(_on_done)

    return JSONResponse(
        {
            "message": "started",
            "session_id": api_state.session_id,
            "path": wav,
        }
    )


@router.post("/session/audio")
async def api_session_audio(request: Request, session_id: str = ""):
    """Receive natively captured system audio: mono float32 LE @ 16 kHz.

    409 tells the client its session is gone so it can stop capturing.
    """
    push = api_state.audio_push
    if push is None or not api_state.is_running or session_id != api_state.session_id:
        return JSONResponse(
            {"message": "no native system-audio session", "session_id": api_state.session_id},
            status_code=409,
        )
    body = await _read_bounded_request_body(request, _MAX_NATIVE_AUDIO_BODY_BYTES)
    if body is None:
        return JSONResponse({"message": "audio body is too large"}, status_code=413)
    if len(body) % 4:
        return JSONResponse({"message": "body must be float32 samples"}, status_code=400)
    push.push(np.frombuffer(body, dtype="<f4"))
    return Response(status_code=204)


@router.post("/session/stop")
async def api_session_stop():
    if api_state.is_running:
        api_state.state = "stopping"
        if api_state.control is not None:
            api_state.control.request_stop()
        api_state.stop_meter_loop()
        return JSONResponse({"message": "stopping", "session_id": api_state.session_id})
    api_state.state = "completed"
    api_state.error = None
    return JSONResponse({"message": "not running", "session_id": api_state.session_id})


def _inactive_session_message(action: str) -> str:
    """Return a context-aware message for pause/resume when not running."""
    if api_state.state == "failed":
        return f"cannot {action}: session failed — {api_state.error or 'unknown error'}"
    if api_state.state == "completed":
        return f"cannot {action}: session has ended — start a new session first"
    if api_state.session_id is not None:
        return f"cannot {action}: session is no longer active"
    return f"cannot {action}: no active session"


@router.post("/session/pause")
async def api_session_pause():
    if not api_state.is_running:
        return JSONResponse(
            {
                "message": _inactive_session_message("pause"),
                "session_id": api_state.session_id,
            },
            status_code=400,
        )
    if api_state.control is None:
        return JSONResponse(
            {
                "message": "cannot pause: session control unavailable",
                "session_id": api_state.session_id,
            },
            status_code=400,
        )
    api_state.control.pause()
    api_state.state = "paused"
    return JSONResponse({"message": "paused", "session_id": api_state.session_id})


@router.post("/session/resume")
async def api_session_resume():
    if not api_state.is_running:
        return JSONResponse(
            {
                "message": _inactive_session_message("resume"),
                "session_id": api_state.session_id,
            },
            status_code=400,
        )
    if api_state.control is None:
        return JSONResponse(
            {
                "message": "cannot resume: session control unavailable",
                "session_id": api_state.session_id,
            },
            status_code=400,
        )
    api_state.control.resume()
    api_state.state = "listening"
    return JSONResponse({"message": "resumed", "session_id": api_state.session_id})


@router.post("/session/catch-up")
async def api_session_catch_up(body: dict | None = None):
    """Skip ahead: see mimivox.catch_up. Optional {"mode": "fast" | "skip"}."""
    control = api_state.control
    if not api_state.is_running or control is None:
        return JSONResponse(
            {"message": _inactive_session_message("catch up"), "session_id": api_state.session_id},
            status_code=400,
        )
    raw = (body or {}).get("mode")
    try:
        mode = CatchUpMode(raw) if raw else None
    except ValueError:
        return JSONResponse(
            {"message": f"unknown mode: {raw!r} (use fast or skip)"}, status_code=400
        )
    result = apply_catch_up(control, mode)
    if result is None:
        return JSONResponse(
            {
                "message": "already caught up",
                "session_id": api_state.session_id,
                "health": control.latency_snapshot().as_dict(),
            }
        )
    return JSONResponse(
        {
            "message": "caught up",
            "session_id": api_state.session_id,
            **result.as_dict(),
            "health": control.latency_snapshot().as_dict(),
        }
    )


@router.post("/session/speakers/rename")
async def api_session_speakers_rename(body: dict):
    speaker_id = str(body.get("id") or "").strip()
    label = str(body.get("label") or "").strip()
    if not speaker_id or not label:
        return JSONResponse({"message": "id and label are required"}, status_code=400)
    if not api_state.rename_speaker(speaker_id, label):
        return JSONResponse({"message": f"Unknown speaker: {speaker_id}"}, status_code=404)
    return JSONResponse({"message": "renamed", "speaker": {"id": speaker_id, "label": label}})


@router.post("/sessions/{session_id}/speakers/rename")
async def api_archived_session_speakers_rename(session_id: str, body: dict | None = None):
    """Rename a speaker in one saved meeting and update its search index."""
    from mimivox.session_archive import relabel_speaker

    speaker_id = str((body or {}).get("id") or "").strip()
    label = str((body or {}).get("label") or "").strip()
    if not speaker_id or not label:
        return JSONResponse({"message": "id and label are required"}, status_code=400)
    if not relabel_speaker(session_id, speaker_id, label):
        return JSONResponse({"message": f"Unknown speaker: {speaker_id}"}, status_code=404)
    return JSONResponse({"message": "renamed", "speaker": {"id": speaker_id, "label": label}})


@router.post("/session/pins")
async def api_session_pins(body: dict):
    """Replace the pinned line ids of the live session (or an archived one)."""
    raw = body.get("pinned_ids")
    if not isinstance(raw, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) for i in raw
    ):
        return JSONResponse({"message": "pinned_ids must be a list of integers"}, status_code=400)
    ids = sorted(set(raw))
    archived_id = body.get("session_id")
    if archived_id and archived_id != api_state.session_id:
        from mimivox.session_archive import set_pins

        if not set_pins(str(archived_id), ids):
            return JSONResponse({"message": f"Session not found: {archived_id}"}, status_code=404)
        return JSONResponse({"message": "pinned", "pinned_ids": ids})
    api_state.pinned_ids = ids
    if api_state.session_id and api_state._archived:
        from mimivox.session_archive import set_pins

        set_pins(api_state.session_id, ids)
    return JSONResponse({"message": "pinned", "pinned_ids": ids})


@router.post("/session/export")
async def api_session_export(
    format: str = "json",
    session_id: str | None = None,
    pinned_only: bool = False,
):
    from mimivox.export_session import SUPPORTED_FORMATS, write_export
    from mimivox.session_archive import load_session

    fmt = (format or "json").strip().lower()
    if fmt not in SUPPORTED_FORMATS:
        return JSONResponse(
            {
                "message": (
                    f"Unknown format: {format!r}. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
                ),
                "session_id": api_state.session_id,
                "path": None,
            },
            status_code=400,
        )

    events = api_state.events
    sid = api_state.session_id
    settings = api_state.settings
    pinned_ids = list(api_state.pinned_ids)
    notes = (api_state.notes_cache.get(sid or "") or {}).get("notes")
    if session_id:
        archived = load_session(session_id)
        if archived is None:
            return JSONResponse(
                {
                    "message": f"Session not found: {session_id}",
                    "session_id": session_id,
                    "path": None,
                },
                status_code=404,
            )
        events = list(archived.get("events") or [])
        sid = session_id
        settings = archived.get("settings") or settings
        pinned_ids = list(archived.get("pinned_ids") or [])
        notes = archived.get("notes")

    path = write_export(
        fmt,
        events,
        session_id=sid,
        settings=settings,
        out_dir=Path(DATA_DIR) / "transcripts",
        pinned_ids=pinned_ids,
        pinned_only=pinned_only,
        notes=notes,
    )
    return JSONResponse(
        {
            "message": f"exported as {fmt}",
            "session_id": sid,
            "path": str(path),
        }
    )


def _notes_source(session_id: str | None):
    """(session_id, events, archived) for the current or an archived session.

    Returns a JSONResponse error instead when there is nothing to read.
    """
    from mimivox.session_archive import load_session

    live = api_state.session_id
    if session_id and not (session_id == live and api_state.is_running):
        archived = load_session(session_id)
        if archived is None:
            return JSONResponse({"message": f"Session not found: {session_id}"}, status_code=404)
        return session_id, list(archived.get("events") or []), archived
    sid = session_id or live
    if not sid or not api_state.events:
        return JSONResponse({"message": "No session with a transcript yet"}, status_code=404)
    return sid, list(api_state.events), (load_session(sid) if api_state._archived else None)


def _vocabulary_rows(
    session_id: str | None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | JSONResponse:
    from mimivox.export_session import pair_events

    src = _notes_source(session_id)
    if isinstance(src, JSONResponse):
        return src
    sid, events, archived = src
    settings = (
        archived.get("settings") or {}
        if archived is not None
        else {
            **api_state.settings,
            "source_lang": api_state.session_pair.source,
            "target_lang": api_state.session_pair.target,
        }
    )
    return sid, pair_events(events), settings


@router.post("/session/vocabulary")
async def api_session_vocabulary(body: dict | None = None):
    """Words from a session with reading, gloss, count and an example line."""
    from mimivox import learning

    src = _vocabulary_rows((body or {}).get("session_id"))
    if isinstance(src, JSONResponse):
        return src
    sid, rows, settings = src
    words = await asyncio.to_thread(learning.vocabulary, rows, settings)
    return JSONResponse({"session_id": sid, "words": words, "count": len(words)})


@router.post("/session/vocabulary/export")
async def api_session_vocabulary_export(format: str = "csv", session_id: str | None = None):
    from mimivox import learning

    fmt = (format or "csv").strip().lower()
    if fmt not in learning.VOCAB_FORMATS:
        return JSONResponse(
            {
                "message": f"Unknown format: {format!r}. Supported: {', '.join(learning.VOCAB_FORMATS)}"
            },
            status_code=400,
        )
    src = _vocabulary_rows(session_id)
    if isinstance(src, JSONResponse):
        return src
    sid, rows, settings = src
    words = await asyncio.to_thread(learning.vocabulary, rows, settings)
    if not words:
        return JSONResponse({"message": "No vocabulary in this session yet"}, status_code=422)
    path = learning.write_vocabulary(fmt, words, sid, Path(DATA_DIR) / "transcripts")
    return JSONResponse(
        {"message": f"exported {len(words)} words as {fmt}", "session_id": sid, "path": str(path)}
    )


@router.post("/session/notes")
async def api_session_notes(body: dict | None = None):
    """Summary, decisions and action items (EN + JA) for a session, cached in its archive."""
    from mimivox import meeting_notes, notes_service
    from mimivox.session_archive import load_session, save_notes

    if (not_ready := models_not_ready()) is not None:
        return not_ready
    body = body or {}
    src = _notes_source(body.get("session_id"))
    if isinstance(src, JSONResponse):
        return src
    sid, events, archived = src
    lines = meeting_notes.build_lines(events)
    if not lines:
        return JSONResponse({"message": "The transcript has no lines yet"}, status_code=422)

    if not body.get("regenerate"):
        pending = api_state.auto_notes.get(sid)
        # The meeting just ended; its notes are on the way. Once another
        # meeting starts they wait for it to end, so write them now instead.
        while pending is not None and not pending.done() and not api_state.is_running:
            await asyncio.wait({pending}, timeout=0.5)
        if pending is not None and pending.done():
            archived = load_session(sid) or archived
        cached = (archived or {}).get("notes")
        if cached is None:
            hit = api_state.notes_cache.get(sid)
            cached = hit["notes"] if hit and hit["line_count"] == len(lines) else None
        if cached is not None:
            return JSONResponse({**cached, "meta": {"cached": True}})

    try:
        notes, meta = await notes_service.notes_for(lines, body.get("chunk_budget"))
    except meeting_notes.NotesError:
        return JSONResponse({"message": "The notes model returned invalid JSON."}, status_code=502)
    except Exception:
        return JSONResponse({"message": "The notes model is unavailable."}, status_code=503)

    payload = notes.to_dict()
    if archived is not None:
        save_notes(sid, payload)
    else:
        api_state.notes_cache[sid] = {"line_count": len(lines), "notes": payload}
    return JSONResponse({**payload, "meta": {**meta, "cached": False, "lines": len(lines)}})


@router.post("/session/ask")
async def api_session_ask(body: dict):
    """Answer a question from the transcript so far, citing line ids."""
    from mimivox import meeting_notes, notes_service

    if (not_ready := models_not_ready()) is not None:
        return not_ready
    question = str(body.get("question") or "").strip()
    if not question:
        return JSONResponse({"message": "question is required"}, status_code=400)
    src = _notes_source(body.get("session_id"))
    if isinstance(src, JSONResponse):
        return src
    sid, events, _archived = src
    lines = meeting_notes.build_lines(events)
    if not lines:
        return JSONResponse({"message": "The transcript has no lines yet"}, status_code=422)
    # Only a meeting still under way has a room to put an unanswered question to.
    live = api_state.is_running and sid == api_state.session_id
    try:
        answer, meta = await notes_service.ask(lines, question, live=live)
    except meeting_notes.NotesError:
        return JSONResponse({"message": "The notes model returned invalid JSON."}, status_code=502)
    except Exception:
        return JSONResponse({"message": "The notes model is unavailable."}, status_code=503)
    return JSONResponse({**answer.to_dict(), "meta": {**meta, "lines": len(lines)}})


@router.get("/sessions")
async def api_sessions_list():
    from mimivox.session_archive import list_sessions

    return JSONResponse({"sessions": list_sessions()})


@router.get("/sessions/search")
async def api_sessions_search(
    q: str = "",
    since: str | None = None,
    until: str | None = None,
    speaker: str | None = None,
    tag: str | None = None,
    limit: int = 20,
):
    """Full-text search over archived sessions (JA/EN text, speakers, titles, tags)."""
    from mimivox.session_archive import search_sessions

    try:
        return JSONResponse(
            search_sessions(q=q, since=since, until=until, speaker=speaker, tag=tag, limit=limit)
        )
    except ValueError:
        return JSONResponse({"message": "Use valid dates for the search range."}, status_code=400)


@router.post("/sessions/{session_id}/tags")
async def api_sessions_tags(session_id: str, body: dict[str, Any] | None = None):
    """Add and/or remove tags: {"add": ["budget"], "remove": ["old"]}."""
    from mimivox.session_archive import update_tags
    from mimivox.session_library import MAX_TAG_LEN

    body = body or {}
    try:
        tags = update_tags(
            session_id,
            [str(t) for t in body.get("add") or []],
            [str(t) for t in body.get("remove") or []],
        )
    except ValueError:
        return JSONResponse(
            {"message": f"Each tag must contain 1 to {MAX_TAG_LEN} characters."},
            status_code=400,
        )
    if tags is None:
        return JSONResponse({"message": f"Session not found: {session_id}"}, status_code=404)
    return JSONResponse({"session_id": session_id, "tags": tags})


@router.post("/sessions/{session_id}/title")
async def api_sessions_title(session_id: str, body: dict[str, Any] | None = None):
    """Rename a meeting: {"title": "..."}; an empty title restores the generated one."""
    from mimivox.session_archive import TITLE_MAX, rename_session

    raw = (body or {}).get("title")
    try:
        headline = rename_session(session_id, raw if isinstance(raw, str) else "")
    except ValueError:
        return JSONResponse(
            {"message": f"Use at most {TITLE_MAX} characters for the meeting title."},
            status_code=400,
        )
    if headline is None:
        return JSONResponse({"message": f"Session not found: {session_id}"}, status_code=404)
    return JSONResponse({"session_id": session_id, "headline": headline or None})


@router.post("/sessions/{session_id}/recover")
async def api_sessions_recover(session_id: str, body: dict | None = None):
    """Recover skipped captions from the archive (see mimivox.gap_recovery).

    Optional {"index": n} limits it to one gap / untranslated line. Refused
    while a session is running, since it would take the GPU from live captions.
    """
    from mimivox import gap_recovery

    if (not_ready := models_not_ready()) is not None:
        return not_ready
    if api_state.is_running:
        return JSONResponse(
            {"message": "stop the running session before recovering skipped captions"},
            status_code=409,
        )
    raw = (body or {}).get("index")
    indexes = {int(raw)} if isinstance(raw, int) else None
    report = await gap_recovery.recover_session(
        session_id, gap_recovery.default_transcribe, gap_recovery.default_translate, indexes
    )
    if report is None:
        return JSONResponse({"message": f"Session not found: {session_id}"}, status_code=404)
    return JSONResponse({"session_id": session_id, **report.as_dict()})


@router.get("/sessions/{session_id}")
async def api_sessions_get(session_id: str):
    from mimivox.session_archive import load_session

    data = load_session(session_id)
    if data is None:
        return JSONResponse(
            {"message": f"Session not found: {session_id}"},
            status_code=404,
        )
    return JSONResponse(data)


@router.delete("/sessions/{session_id}")
async def api_sessions_delete(session_id: str):
    from mimivox.session_archive import delete_session

    deleted = delete_session(session_id)
    if not deleted:
        return JSONResponse(
            {"message": f"Session not found: {session_id}", "session_id": session_id},
            status_code=404,
        )
    return JSONResponse({"message": "deleted", "session_id": session_id})


@router.delete("/sessions")
async def api_sessions_delete_all():
    """Delete all archived history and everything derived from it."""
    return JSONResponse({"message": "deleted", **delete_all_history()})


@router.post("/session/delete")
async def api_session_delete():
    if api_state.is_running:
        if api_state.control is not None:
            api_state.control.request_stop()
        api_state.stop_meter_loop()
        api_state.pipeline_task.cancel()  # type: ignore[union-attr]
    if api_state.session_id and not api_state._archived:
        session_archive.remove_gap_audio(api_state.session_id)
    api_state.events = []
    api_state._event_index = 0
    api_state.session_id = None
    api_state.state = "idle"
    api_state.error = None
    api_state.control = None
    return JSONResponse({"message": "session deleted"})


async def _translate_en_ja(text: str):
    """Reply mode: EN→JA plus a JA→EN back-translation so the user can check it."""
    from mimivox.config import _models
    from mimivox.translate import translate_directed

    if _models.cat_model is None or _models.cat_tokenizer is None:
        return JSONResponse(
            {
                "source_text": text,
                "translated_text": "",
                "error": "Translation model not loaded — start mimivox-server first.",
            },
            status_code=503,
        )
    try:
        shared_mlx_lock()
        ja = await translate_directed(text, "en-ja")
        back = await translate_directed(ja, "ja-en") if ja else ""
        return JSONResponse({"source_text": text, "translated_text": ja, "back_translation": back})
    except Exception:
        return JSONResponse(
            {
                "source_text": text,
                "translated_text": "",
                "error": "Could not translate the text.",
            },
            status_code=500,
        )


@router.post("/dictate")
async def api_dictate(request: Request, hint: str = "ja"):
    """Text from a short mic clip (mono float32 LE @ 16 kHz) for the Translate tab."""
    from mimivox import dictation

    if (not_ready := models_not_ready()) is not None:
        return not_ready
    if api_state.is_running:
        return JSONResponse(
            {"message": "Dictation is off while a meeting is being captioned."}, status_code=409
        )
    body = await _read_bounded_request_body(request, dictation.MAX_BYTES)
    if body is None:
        return JSONResponse(
            {"message": f"Dictation is limited to {dictation.MAX_SECONDS} seconds."},
            status_code=413,
        )
    audio = dictation.decode_clip(body)
    if isinstance(audio, str):
        return JSONResponse({"message": audio}, status_code=400)
    hint = hint if hint in dictation.LANGUAGES else "ja"
    lock = shared_mlx_lock()
    try:
        async with lock:
            result = await asyncio.to_thread(dictation.transcribe, audio, hint)
    except Exception:
        return JSONResponse({"message": "Could not transcribe the audio."}, status_code=500)
    return JSONResponse({"text": result.text, "language": result.language})


@router.post("/translate")
async def api_translate(body: dict):
    if (not_ready := models_not_ready()) is not None:
        return not_ready
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse(
            {"source_text": "", "translated_text": ""},
            status_code=400,
        )
    from mimivox.translate import DIRECTIONS

    direction = body.get("direction") or "ja-en"
    if direction not in DIRECTIONS:
        return JSONResponse(
            {
                "source_text": text,
                "translated_text": "",
                "error": f"unknown direction: {direction!r} (use 'ja-en' or 'en-ja')",
            },
            status_code=400,
        )
    if direction == "en-ja":
        return await _translate_en_ja(text)
    from mimivox.config import _models
    from mimivox.translate import (
        _is_mostly_english,
        apply_glossary,
        get_translator,
        normalize_punctuation,
    )

    ja_raw = normalize_punctuation(text)
    ja = apply_glossary(ja_raw)

    if _is_mostly_english(ja_raw):
        return JSONResponse({"source_text": text, "translated_text": ja.rstrip("。")})

    if _models.cat_model is None or _models.cat_tokenizer is None:
        return JSONResponse(
            {
                "source_text": text,
                "translated_text": "",
                "error": "Translation model not loaded — start mimivox-server first.",
            },
            status_code=503,
        )

    try:
        shared_mlx_lock()
        translator = get_translator()
        en = (await translator.translate(ja, lambda _token: None))[0]
        return JSONResponse({"source_text": text, "translated_text": en or ""})
    except Exception:
        return JSONResponse(
            {
                "source_text": text,
                "translated_text": "",
                "error": "Could not translate the text.",
            },
            status_code=500,
        )


@router.post("/models/{name}")
async def api_models(name: str, body: dict | None = None):
    """verify / download / update / reset-cache against HF cache."""
    from mimivox.models_service import (
        _set_download_state,
        download_models,
        get_download_state,
        reset_cache,
    )
    from mimivox.models_service import (
        verify as verify_models,
    )

    body = body or {}
    cmd = (name or "").strip().lower()
    settings = api_state.settings

    if cmd in ("download", "update") and offline_locked():
        return JSONResponse(
            {
                "message": (
                    f"Offline mode is on ({OFFLINE_ENV}=1), so downloads are disabled. "
                    f"Restart the server without {OFFLINE_ENV} to fetch models."
                ),
                "session_id": api_state.session_id,
            },
            status_code=409,
        )

    if cmd == "verify":
        readiness = verify_models(settings)
        readiness["glossary"] = _default_model_readiness(settings).get("glossary")
        msg = (
            "Models verified and ready"
            if readiness["ready"]
            else "Models not fully ready — see next_steps"
        )
        return JSONResponse(
            {
                "message": msg,
                "session_id": api_state.session_id,
                "model_readiness": readiness,
            }
        )

    if cmd == "reset-cache":
        scoped_path = body.get("path")
        if scoped_path is None or scoped_path == "":
            scoped_path = body.get("scoped_path")
        result = reset_cache(
            yes=body.get("yes") is True,
            scoped_path=scoped_path,
        )
        status = 200 if result.get("ok") else 400
        return JSONResponse(
            {
                "message": result["message"],
                "session_id": api_state.session_id,
                "ok": result.get("ok", False),
                "path": result.get("path"),
            },
            status_code=status,
        )

    if cmd == "unload-notes":
        from mimivox import notes_service

        freed = await notes_service.unload()
        return JSONResponse(
            {
                "message": "notes model unloaded" if freed else "notes model was not loaded",
                "session_id": api_state.session_id,
                "model_readiness": _default_model_readiness(settings),
            }
        )

    if cmd == "download" and not body.get("notes_llm") and not setup_manager.is_ready:
        state = await setup_manager.download()
        return JSONResponse(
            {
                "message": f"setup: {state.phase}",
                "session_id": api_state.session_id,
                "setup": state_json(state),
                "model_readiness": _default_model_readiness(settings),
            }
        )

    if cmd in ("download", "update"):
        state = get_download_state()
        if state.get("busy"):
            return JSONResponse(
                {
                    "message": state.get("message") or "Download already in progress",
                    "session_id": api_state.session_id,
                    "model_readiness": _default_model_readiness(settings),
                }
            )

        force = bool(body.get("force")) if cmd == "update" else False
        only = (
            "notes_llm" if body.get("notes_llm") else None
        )  # {"notes_llm": true}: just the optional notes model
        # update defaults to force_download=False (revision refresh / fill gaps)
        _set_download_state(
            busy=True,
            command=cmd,
            message=f"{cmd} started",
            error=None,
            progress=None,
        )

        async def _run() -> None:
            loop = asyncio.get_running_loop()

            def _progress(msg: str) -> None:
                _set_download_state(message=msg, progress=msg)

            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: download_models(
                        settings, force=force, on_progress=_progress, only=only
                    ),
                )
                if result.get("ok"):
                    _set_download_state(
                        busy=False,
                        message=f"{cmd} complete",
                        error=None,
                        progress=None,
                    )
                else:
                    err = "; ".join(result.get("errors") or ["download failed"])
                    _set_download_state(
                        busy=False,
                        message=f"{cmd} finished with errors: {err}",
                        error=err,
                        progress=None,
                    )
            except Exception:
                _set_download_state(
                    busy=False,
                    message=f"{cmd} failed",
                    error="Could not download the models.",
                    progress=None,
                )

        # Cancel prior completed task handle only; never stack concurrent downloads.
        if api_state._models_task is not None and not api_state._models_task.done():
            return JSONResponse(
                {
                    "message": "Download already in progress",
                    "session_id": api_state.session_id,
                }
            )
        api_state._models_task = asyncio.create_task(_run())
        return JSONResponse(
            {
                "message": f"{cmd} started in background",
                "session_id": api_state.session_id,
                "model_readiness": _default_model_readiness(settings),
            }
        )

    return JSONResponse(
        {
            "message": f"Unknown model command: {name}",
            "session_id": api_state.session_id,
        },
        status_code=400,
    )
