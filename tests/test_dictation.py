"""Dictation for the Translate tab: clip checks and the API contract (Whisper faked)."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox import api_adapter, dictation
from mimivox.api_adapter import api_state, router


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_adapter, "models_not_ready", lambda: None)
    monkeypatch.setattr(api_state, "pipeline_task", None)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _clip(seconds: float = 1.0) -> bytes:
    return np.zeros(int(16000 * seconds), dtype="<f4").tobytes()


def test_dictate_returns_text_and_detected_language(client, monkeypatch):
    calls = []

    def fake(audio, language):
        calls.append(language)
        return "Let's meet on Friday.", "en"

    monkeypatch.setattr(dictation, "_transcribe", fake)
    r = client.post("/api/dictate?hint=ja", content=_clip())
    assert r.status_code == 200
    assert r.json() == {"text": "Let's meet on Friday.", "language": "en"}
    assert calls == [None]


def test_unexpected_language_is_redone_with_the_hint(client, monkeypatch):
    replies = iter([("Ni hao", "zh"), ("こんにちは", "ja")])
    calls = []

    def fake(audio, language):
        calls.append(language)
        return next(replies)

    monkeypatch.setattr(dictation, "_transcribe", fake)
    assert client.post("/api/dictate?hint=ja", content=_clip()).json() == {
        "text": "こんにちは",
        "language": "ja",
    }
    assert calls == [None, "ja"]


def test_punctuation_only_comes_back_empty(client, monkeypatch):
    monkeypatch.setattr(dictation, "_transcribe", lambda a, lang: ("。", "ja"))
    assert client.post("/api/dictate", content=_clip()).json()["text"] == ""


@pytest.mark.parametrize("body", [b"", b"abc", _clip(0.1), _clip(61)])
def test_bad_clips_are_refused(client, body):
    assert client.post("/api/dictate", content=body).status_code == 400


def test_refused_while_a_meeting_is_captioned(client, monkeypatch):
    monkeypatch.setattr(type(api_state), "is_running", property(lambda self: True))
    assert client.post("/api/dictate", content=_clip()).status_code == 409
