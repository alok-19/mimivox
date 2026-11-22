"""Unit tests for the Swift /api/* event adapter (no models required)."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox.api_adapter import APISessionState, api_state, router
from mimivox.session_control import SessionControl


def test_final_event_emits_asr_and_translation():
    state = APISessionState()
    state.handle_pipeline_event({"kind": "session_start", "wav": "live"})
    state.handle_pipeline_event(
        {
            "kind": "final",
            "ts": "00:12",
            "ja": "では始めさせていただきます。",
            "en": "Let me begin.",
            "asr_ms": 320,
            "translate_ms": 410,
            "total_ms": 730,
            "duration_s": 2.5,
        }
    )

    types = [e["type"] for e in state.events]
    assert "SessionStartedEvent" in types
    assert "AsrCompletedEvent" in types
    assert "TranslationCompletedEvent" in types

    asr = next(e for e in state.events if e["type"] == "AsrCompletedEvent")
    tr = next(e for e in state.events if e["type"] == "TranslationCompletedEvent")
    assert asr["index"] == tr["index"] == 1
    assert asr["text"] == "では始めさせていただきます。"
    assert tr["text"] == "Let me begin."
    assert tr["source_indexes"] == [1]
    assert asr["start"] == 12.0
    assert asr["end"] == 14.5
    assert tr["start"] == 12.0
    assert tr["end"] == 14.5
    assert tr["display_latency_s"] == 0.73
    assert tr["is_late"] is False


def test_late_translation_flag():
    state = APISessionState()
    state.handle_pipeline_event(
        {
            "kind": "final",
            "ts": "01:05",
            "ja": "次です",
            "en": "Next.",
            "total_ms": 2500,
        }
    )
    tr = next(e for e in state.events if e["type"] == "TranslationCompletedEvent")
    assert tr["is_late"] is True
    assert tr["start"] == 65.0
    assert tr["end"] == 65.0


def test_partial_not_stored():
    state = APISessionState()
    state.handle_pipeline_event({"kind": "partial", "ts": "00:01", "ja": "はい"})
    assert state.events == []


def test_partial_en_preview_not_stored():
    """Streamed EN preview is preview-only (not stored on the Swift timeline)."""
    state = APISessionState()
    state.handle_pipeline_event({"kind": "partial_en_token", "ts": "00:01", "text": "Ex"})
    state.handle_pipeline_event({"kind": "partial_en", "ts": "00:01", "en": "Excuse me"})
    assert state.events == []


def test_final_event_carries_latency_breakdown():
    state = APISessionState()
    state.handle_pipeline_event(
        {
            "kind": "final",
            "ts": "00:12",
            "ja": "すみません",
            "en": "Excuse me",
            "asr_ms": 320,
            "translate_ms": 110,
            "total_ms": 430,
            "vad_wait_ms": 672,
            "queue_wait_ms": 250,
            "duration_s": 1.2,
        }
    )
    tr = next(e for e in state.events if e["type"] == "TranslationCompletedEvent")
    asr = next(e for e in state.events if e["type"] == "AsrCompletedEvent")
    assert tr["display_latency_s"] == 0.43
    assert tr["asr_latency_s"] == pytest.approx(0.32)
    assert tr["translate_latency_s"] == pytest.approx(0.11)
    assert asr["asr_latency_s"] == pytest.approx(0.32)
    assert tr["vad_wait_s"] == pytest.approx(0.672)
    assert tr["queue_wait_s"] == pytest.approx(0.25)
    assert tr["perceived_latency_s"] == pytest.approx(0.43 + 0.672 + 0.25)


def test_partial_emits_asr_partial_live():
    state = APISessionState()
    pushed: list[dict] = []
    state._push = pushed.append  # type: ignore[method-assign]
    state.handle_pipeline_event({"kind": "partial", "ts": "00:01", "ja": "こんにちは 世界"})
    assert state.events == []
    assert len(pushed) == 1
    assert pushed[0]["type"] == "AsrPartialEvent"
    assert pushed[0]["text"] == "こんにちは 世界"
    assert pushed[0]["start"] == 1.0
    assert pushed[0]["timestamp_s"] == 1.0


def test_drain_events_do_not_reopen_stopping_session():
    state = APISessionState()
    state.state = "stopping"

    state.handle_pipeline_event({"kind": "translating", "ja": "最後", "ts": 1.0})
    assert state.state == "stopping"

    state.handle_pipeline_event(
        {
            "kind": "final",
            "ja": "最後",
            "en": "The final line",
            "ts": 1.0,
            "total_ms": 100,
        }
    )
    assert state.state == "stopping"


def test_translating_emits_translation_started_live():
    state = APISessionState()
    pushed: list[dict] = []
    state._push = pushed.append  # type: ignore[method-assign]
    state.handle_pipeline_event({"kind": "translating", "ts": "00:02", "ja": "はい"})
    assert state.state == "processing"
    # Stored so mid-session SSE subscribers (Replay) still see translation phase.
    assert len(state.events) == 1
    assert state.events[0]["type"] == "TranslationStartedEvent"
    assert len(pushed) == 1
    assert pushed[0]["type"] == "TranslationStartedEvent"
    assert pushed[0]["text"] == "はい"
    assert pushed[0]["start"] == 2.0


def test_en_token_ignored():
    state = APISessionState()
    pushed: list[dict] = []
    state._push = pushed.append  # type: ignore[method-assign]
    state.handle_pipeline_event({"kind": "en_token", "text": "Hello"})
    assert state.events == []
    assert pushed == []


def test_status_payload_shape():
    state = APISessionState()
    payload = state.status_payload()
    assert payload["state"] == "idle"
    assert payload["model_readiness"]["ready"] is True
    assert "whisper" in payload["model_readiness"]
    assert "cat_translate" in payload["model_readiness"]
    assert "settings" in payload
    assert payload["settings"]["privacy_mode"] == "local-only"
    assert "health" in payload
    assert payload["performance_summary"] is None


def test_status_clears_sticky_error_when_completed():
    """Completed/idle sessions must not keep a prior fault in the status payload."""
    state = APISessionState()
    state.state = "completed"
    state.error = "[Errno 5] Input/output error"
    payload = state.status_payload()
    assert payload["state"] == "completed"
    assert payload["error"] is None
    assert state.error is None
    assert payload["health"]["degraded"] is False


def test_status_keeps_error_when_failed():
    state = APISessionState()
    state.state = "failed"
    state.error = "model OOM"
    payload = state.status_payload()
    assert payload["state"] == "failed"
    assert payload["error"] == "model OOM"
    assert payload["health"]["degraded"] is True


def test_status_health_from_control():
    state = APISessionState()
    state.control = SessionControl()
    state.control.audio_input_active = True
    state.control.asr_busy = True
    state.control.set_audio_level(0.55, speech=True)
    state.control.record_final(0.8, had_ja=True, had_en=True)
    state.state = "listening"
    # Pretend a pipeline task is running.
    state.pipeline_task = MagicMock()
    state.pipeline_task.done.return_value = False

    payload = state.status_payload()
    assert payload["state"] == "listening"
    assert payload["health"]["audio_input_active"] is True
    assert payload["health"]["asr_worker_active"] is True
    assert payload["health"]["audio_level"] == 0.55
    assert payload["health"]["active_speech"] is True
    assert payload["performance_summary"] is not None
    assert payload["performance_summary"]["asr_count"] == 1
    assert payload["performance_summary"]["display_latency_s"]["p50"] == 0.8


def test_pause_resume_via_control():
    state = APISessionState()
    state.control = SessionControl()
    state.pipeline_task = MagicMock()
    state.pipeline_task.done.return_value = False
    state.state = "listening"

    state.control.pause()
    state.state = "paused"
    payload = state.status_payload()
    assert payload["state"] == "paused"
    assert payload["health"]["session_label"] == "Paused"

    state.control.resume()
    state.state = "listening"
    payload = state.status_payload()
    assert payload["state"] == "listening"
    assert payload["health"]["session_label"] == "Listening"


def _api_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_status_events_query_omits_the_transcript_for_pollers():
    api_state.pipeline_task = MagicMock()
    api_state.pipeline_task.done.return_value = False
    api_state.state = "listening"
    api_state.control = None
    api_state.events = [{"type": "AsrCompletedEvent", "index": 0, "text": "はい"}]
    client = _api_client()
    try:
        assert client.get("/api/status").json()["events"] == api_state.events
        light = client.get("/api/status?events=0").json()
        assert light["events"] == []
        assert light["state"] == "listening"
    finally:
        api_state.pipeline_task = None
        api_state.state = "idle"
        api_state.events = []


def test_pause_rejects_when_not_running():
    # Ensure clean module state.
    api_state.pipeline_task = None
    api_state.state = "idle"
    api_state.control = None
    client = _api_client()
    resp = client.post("/api/session/pause")
    assert resp.status_code == 400
    assert "cannot pause" in resp.json()["message"]


def test_resume_rejects_when_not_running():
    api_state.pipeline_task = None
    api_state.state = "idle"
    api_state.control = None
    client = _api_client()
    resp = client.post("/api/session/resume")
    assert resp.status_code == 400
    assert "cannot resume" in resp.json()["message"]


def test_pause_resume_http_when_running():
    api_state.pipeline_task = MagicMock()
    api_state.pipeline_task.done.return_value = False
    api_state.state = "listening"
    api_state.session_id = "test-sid"
    api_state.control = SessionControl()
    client = _api_client()

    resp = client.post("/api/session/pause")
    assert resp.status_code == 200
    assert resp.json()["message"] == "paused"
    assert api_state.state == "paused"
    assert api_state.control.paused is True

    resp = client.post("/api/session/resume")
    assert resp.status_code == 200
    assert resp.json()["message"] == "resumed"
    assert api_state.state == "listening"
    assert api_state.control.paused is False

    # Cleanup module singleton for other tests.
    api_state.pipeline_task = None
    api_state.state = "idle"
    api_state.control = None
    api_state.session_id = None


def test_final_preserves_paused_state():
    state = APISessionState()
    state.state = "paused"
    state.pipeline_task = MagicMock()
    state.pipeline_task.done.return_value = False
    state.handle_pipeline_event(
        {
            "kind": "final",
            "ts": "00:03",
            "ja": "はい",
            "en": "Yes.",
            "total_ms": 100,
        }
    )
    assert state.state == "paused"


def test_settings_post_persists_and_refreshes_glossary(tmp_path, monkeypatch):
    from mimivox import settings_store
    from mimivox.config import GLOSSARY
    from mimivox.translate import apply_glossary, set_active_glossary

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_store, "_SETTINGS_PATH", settings_path)

    api_state.pipeline_task = None
    api_state.state = "idle"
    api_state.settings = settings_store.default_settings()
    set_active_glossary(dict(GLOSSARY))

    client = _api_client()
    resp = client.post(
        "/api/settings",
        json={
            "latency_quality": "quality",
            "glossary_terms": [{"source": "みみぼっくす", "target": "MimiVox"}],
            "cloud_provider_enabled": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["settings"]["latency_quality"] == "quality"
    assert body["message"] == "Cloud disabled in MimiVox; running local-only"
    assert settings_path.exists()
    assert apply_glossary("みみぼっくす") == "MimiVox"

    # Cleanup
    set_active_glossary(dict(GLOSSARY))
    api_state.settings = settings_store.default_settings()


def test_status_reports_model_path_mismatch():
    from mimivox.settings_store import default_settings

    state = APISessionState()
    state.settings = default_settings()
    state.settings["model_paths"] = {
        "whisper": "other/whisper",
        "cat_translate": state.settings["model_paths"]["cat_translate"],
    }
    payload = state.status_payload()
    assert payload["model_readiness"]["whisper"]["matches_loaded"] is False
    assert any("Restart" in s for s in payload["model_readiness"]["next_steps"])
    assert "effective terms" in payload["model_readiness"]["glossary"]["message"]


def _reset_api_session():
    api_state.pipeline_task = None
    api_state.state = "idle"
    api_state.control = None
    api_state.session_id = None
    api_state.error = None
    api_state.events = []
    api_state._event_index = 0


def test_session_start_system_audio_missing_device_returns_400(monkeypatch):
    from mimivox.settings_store import default_settings

    _reset_api_session()
    api_state.settings = default_settings()
    monkeypatch.setattr(
        "mimivox.api_adapter.resolve_system_audio_device",
        lambda _settings: None,
    )
    client = _api_client()
    resp = client.post("/api/session/start", json={"source": "system-audio"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["session_id"] is None
    assert "BlackHole" in body["message"]
    assert api_state.state == "idle"
    _reset_api_session()


def test_session_start_system_audio_uses_system_device(monkeypatch):
    from mimivox.settings_store import default_settings

    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)

    _reset_api_session()
    api_state.settings = default_settings()
    api_state.settings["system_audio_device"] = "7"
    api_state.settings["microphone_device"] = "1"
    monkeypatch.setattr(
        "mimivox.settings_store.list_input_devices",
        lambda: [(1, "Mic"), (7, "BlackHole 2ch")],
    )
    monkeypatch.setattr("mimivox.api_adapter.run", fake_run)

    client = _api_client()
    resp = client.post("/api/session/start", json={"source": "system-audio"})
    assert resp.status_code == 200
    assert resp.json()["message"] == "started"
    assert captured.get("live") is True
    assert captured.get("audio_device") == 7

    _reset_api_session()


def test_session_start_microphone_uses_mic_device(monkeypatch):
    from mimivox.settings_store import default_settings

    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)

    _reset_api_session()
    api_state.settings = default_settings()
    api_state.settings["system_audio_device"] = "7"
    api_state.settings["microphone_device"] = "3"
    monkeypatch.setattr(
        "mimivox.settings_store.list_input_devices",
        lambda: [(3, "Built-in Mic"), (7, "BlackHole 2ch")],
    )
    monkeypatch.setattr("mimivox.api_adapter.run", fake_run)

    client = _api_client()
    resp = client.post("/api/session/start", json={"source": "microphone"})
    assert resp.status_code == 200
    assert captured.get("live") is True
    assert captured.get("audio_device") == 3

    _reset_api_session()


@pytest.mark.asyncio
async def test_session_stop_requests_graceful_pipeline_drain():
    import asyncio
    import json

    from mimivox.api_adapter import api_session_stop
    from mimivox.session_control import SessionControl

    _reset_api_session()
    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    api_state.pipeline_task = task
    api_state.state = "listening"
    api_state.session_id = "live-session"
    api_state.control = SessionControl()

    response = await api_session_stop()
    body = json.loads(response.body)

    assert body["message"] == "stopping"
    assert api_state.control.stop_requested is True
    assert not task.cancelled()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _reset_api_session()


# ---------------------------------------------------------------------------
# Minor-issue fixes: translate 503, pause/resume context messages
# ---------------------------------------------------------------------------


def test_translate_returns_503_when_cat_model_not_loaded(monkeypatch):
    """Quick Translate without a loaded model returns 503, not 500."""
    import mimivox.config as cfg

    _reset_api_session()
    monkeypatch.setattr(cfg._models, "cat_model", None)
    monkeypatch.setattr(cfg._models, "cat_tokenizer", None)

    client = _api_client()
    resp = client.post("/api/translate", json={"text": "こんにちは"})
    assert resp.status_code == 503
    body = resp.json()
    assert "not loaded" in body["error"].lower()
    assert body["translated_text"] == ""
    _reset_api_session()


def test_translate_english_passthrough_works_without_model(monkeypatch):
    """English code-switching should pass through even if CAT not loaded."""
    import mimivox.config as cfg

    _reset_api_session()
    monkeypatch.setattr(cfg._models, "cat_model", None)
    monkeypatch.setattr(cfg._models, "cat_tokenizer", None)

    client = _api_client()
    resp = client.post("/api/translate", json={"text": "hello world"})
    assert resp.status_code == 200
    assert resp.json()["translated_text"] == "hello world"
    _reset_api_session()


def _fake_cat_loaded(monkeypatch):
    import mimivox.config as cfg

    monkeypatch.setattr(cfg._models, "cat_model", object())
    monkeypatch.setattr(cfg._models, "cat_tokenizer", object())


def test_translate_en_ja_returns_japanese_and_back_translation(monkeypatch):
    import mimivox.translate as tr

    _reset_api_session()
    _fake_cat_loaded(monkeypatch)
    calls = []

    async def fake(text, direction):
        calls.append((text, direction))
        return (
            "改訂版のお見積りを金曜日までにお送りいただけますか。"
            if direction == "en-ja"
            else "Could you send the revised quote by Friday?"
        )

    monkeypatch.setattr(tr, "translate_directed", fake)
    resp = _api_client().post(
        "/api/translate",
        json={"text": "Could you send me the revised quote by Friday?", "direction": "en-ja"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "source_text": "Could you send me the revised quote by Friday?",
        "translated_text": "改訂版のお見積りを金曜日までにお送りいただけますか。",
        "back_translation": "Could you send the revised quote by Friday?",
    }
    assert [d for _, d in calls] == ["en-ja", "ja-en"]
    assert calls[1][0] == "改訂版のお見積りを金曜日までにお送りいただけますか。"
    _reset_api_session()


def test_translate_default_direction_unchanged_shape(monkeypatch):
    """No direction => old ja-en shape, no back_translation key, translate_directed unused."""
    import mimivox.translate as tr

    _reset_api_session()
    _fake_cat_loaded(monkeypatch)

    async def boom(text, direction):
        raise AssertionError("directed path must not run for default direction")

    async def fake_translate(self, ja, cb, **kw):
        return "Hello.", 0.0

    monkeypatch.setattr(tr, "translate_directed", boom)
    monkeypatch.setattr(tr.CatTranslator, "translate", fake_translate)
    tr.reset_translator()
    client = _api_client()
    for payload in ({"text": "こんにちは"}, {"text": "こんにちは", "direction": "ja-en"}):
        resp = client.post("/api/translate", json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"source_text": "こんにちは", "translated_text": "Hello."}
    tr.reset_translator()
    _reset_api_session()


def test_translate_unknown_direction_is_400():
    _reset_api_session()
    resp = _api_client().post("/api/translate", json={"text": "hi", "direction": "fr-en"})
    assert resp.status_code == 400
    assert "direction" in resp.json()["error"]
    _reset_api_session()


def test_translate_en_ja_503_when_model_not_loaded(monkeypatch):
    import mimivox.config as cfg

    _reset_api_session()
    monkeypatch.setattr(cfg._models, "cat_model", None)
    monkeypatch.setattr(cfg._models, "cat_tokenizer", None)
    resp = _api_client().post("/api/translate", json={"text": "hi", "direction": "en-ja"})
    assert resp.status_code == 503
    assert resp.json()["translated_text"] == ""
    _reset_api_session()


def test_pause_after_completed_gives_helpful_message():
    """Pause on a completed session says 'session has ended', not 'no active session'."""
    _reset_api_session()
    api_state.state = "completed"
    api_state.session_id = "abc-123"

    client = _api_client()
    resp = client.post("/api/session/pause")
    assert resp.status_code == 400
    assert "ended" in resp.json()["message"].lower()
    _reset_api_session()


def test_resume_after_failed_gives_error_context():
    """Resume on a failed session surfaces the failure, not a generic message."""
    _reset_api_session()
    api_state.state = "failed"
    api_state.error = "model OOM"
    api_state.session_id = "xyz-789"

    client = _api_client()
    resp = client.post("/api/session/resume")
    assert resp.status_code == 400
    msg = resp.json()["message"].lower()
    assert "failed" in msg
    assert "oom" in msg
    _reset_api_session()


def test_pause_with_no_session_at_all_says_no_active_session():
    """Pause on idle with no session_id gives the original 'no active session'."""
    _reset_api_session()

    client = _api_client()
    resp = client.post("/api/session/pause")
    assert resp.status_code == 400
    assert "no active session" in resp.json()["message"].lower()
    _reset_api_session()


def test_session_start_native_system_audio_skips_device_lookup(monkeypatch):
    from mimivox.audio import PushAudioSource
    from mimivox.settings_store import default_settings

    captured: dict = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)

    def no_lookup(_settings):
        raise AssertionError("native capture must not resolve a loopback device")

    _reset_api_session()
    api_state.settings = default_settings()
    monkeypatch.setattr("mimivox.api_adapter.resolve_system_audio_device", no_lookup)
    monkeypatch.setattr("mimivox.api_adapter.run", fake_run)

    client = _api_client()
    resp = client.post("/api/session/start", json={"source": "system-audio", "capture": "native"})
    assert resp.status_code == 200
    assert resp.json()["message"] == "started"
    assert captured.get("live") is True
    assert captured.get("audio_device") is None
    assert isinstance(captured.get("audio_push"), PushAudioSource)

    _reset_api_session()


def test_session_audio_rejects_without_native_session():
    _reset_api_session()
    api_state.audio_push = None
    client = _api_client()
    resp = client.post(
        "/api/session/audio?session_id=nope",
        content=np.zeros(160, dtype="<f4").tobytes(),
    )
    assert resp.status_code == 409
    _reset_api_session()


def test_session_audio_pushes_samples_to_running_session():
    from mimivox.audio import PushAudioSource

    _reset_api_session()
    received: list = []
    push = PushAudioSource()
    push.attach(received.append)
    task = MagicMock()
    task.done.return_value = False
    api_state.pipeline_task = task
    api_state.session_id = "s1"
    api_state.audio_push = push

    client = _api_client()
    samples = np.linspace(-0.5, 0.5, 1600, dtype="<f4")
    resp = client.post("/api/session/audio?session_id=s1", content=samples.tobytes())
    assert resp.status_code == 204
    assert len(received) == 1
    np.testing.assert_allclose(received[0], samples)

    stale = client.post("/api/session/audio?session_id=old", content=samples.tobytes())
    assert stale.status_code == 409
    odd = client.post("/api/session/audio?session_id=s1", content=b"\x00" * 6)
    assert odd.status_code == 400

    api_state.audio_push = None
    _reset_api_session()
