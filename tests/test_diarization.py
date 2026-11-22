"""Speaker labels: clusterer, tracker, rename, degradation, event plumbing."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import numpy as np
import pytest

from mimivox import diarization, pipeline
from mimivox.api_adapter import APISessionState
from mimivox.config import SAMPLE_RATE
from mimivox.diarization import (
    SpeakerClusterer,
    SpeakerTracker,
    SpeakerUnavailable,
    fbank,
)
from mimivox.export_session import pair_events, render


def _unit(*xs: float) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def _noisy(base: np.ndarray, rng: np.random.Generator, scale: float = 0.1) -> np.ndarray:
    return base + rng.normal(0, scale, base.shape).astype(np.float32)


# -- clusterer --------------------------------------------------------------
def test_clusterer_separates_and_reuses_speakers():
    rng = np.random.default_rng(0)
    a, b, c = np.eye(16, dtype=np.float32)[:3]
    clusterer = SpeakerClusterer()
    got = [clusterer.assign(_noisy(v, rng))[0] for v in (a, b, a, c, b, a, c)]
    assert got == [0, 1, 0, 2, 1, 0, 2]
    assert len(clusterer) == 3


def test_clusterer_ambiguous_joins_nearest_without_moving_centroid():
    clusterer = SpeakerClusterer(match=0.8, new=0.2)
    clusterer.assign(_unit(1, 0))
    before = clusterer._centroids[0].copy()
    idx, score = clusterer.assign(_unit(1, 1))  # cosine ~0.71: between thresholds
    assert idx == 0 and 0.2 < score < 0.8
    assert len(clusterer) == 1
    np.testing.assert_allclose(clusterer._centroids[0], before)


def test_clusterer_caps_speakers():
    clusterer = SpeakerClusterer(max_speakers=2)
    eye = np.eye(8, dtype=np.float32)
    ids = [clusterer.assign(eye[i])[0] for i in range(4)]
    assert len(clusterer) == 2
    assert max(ids) == 1


def test_clusterer_rejects_malformed_and_bad_thresholds():
    with pytest.raises(SpeakerUnavailable):
        SpeakerClusterer().assign(np.zeros(8))
    with pytest.raises(SpeakerUnavailable):
        SpeakerClusterer().assign(np.array([np.nan, 1.0]))
    with pytest.raises(ValueError):
        SpeakerClusterer(match=0.2, new=0.5)


# -- tracker ----------------------------------------------------------------
class _FakeEmbedder:
    """Embeds a clip as the one-hot vector named by its first sample."""

    def __init__(self) -> None:
        self.fail: Exception | None = None

    def embed(self, audio: np.ndarray) -> np.ndarray:
        if self.fail:
            raise self.fail
        if len(audio) < int(diarization.MIN_EMBED_S * SAMPLE_RATE):
            raise SpeakerUnavailable("clip too short")
        return np.eye(8, dtype=np.float32)[int(audio[0])]


def _clip(speaker: int, seconds: float = 1.0) -> np.ndarray:
    return np.full(int(seconds * SAMPLE_RATE), speaker, dtype=np.float32)


def test_tracker_labels_and_stable_ids():
    tracker = SpeakerTracker(_FakeEmbedder())
    got = [tracker.identify(_clip(s)) for s in (0, 1, 0, 1)]
    assert [g.id for g in got] == ["speaker-1", "speaker-2", "speaker-1", "speaker-2"]
    assert got[0].label == "Speaker 1"
    assert got[1].as_dict() == {"id": "speaker-2", "label": "Speaker 2"}


def test_tracker_rename_applies_to_later_lines():
    tracker = SpeakerTracker(_FakeEmbedder())
    tracker.identify(_clip(0))
    tracker.identify(_clip(1))
    assert tracker.rename("speaker-2", "  Tanaka ") is True
    assert tracker.identify(_clip(1)).label == "Tanaka"
    assert tracker.identify(_clip(0)).label == "Speaker 1"


def test_tracker_rename_rejects_unknown_id_and_blank_label():
    tracker = SpeakerTracker(_FakeEmbedder())
    tracker.identify(_clip(0))
    assert tracker.rename("speaker-9", "X") is False
    assert tracker.rename("speaker-1", "   ") is False


def test_tracker_short_clip_inherits_previous_speaker():
    tracker = SpeakerTracker(_FakeEmbedder())
    assert tracker.identify(_clip(0, 0.2)) is None  # nothing to inherit yet
    first = tracker.identify(_clip(1))
    assert tracker.identify(_clip(0, 0.2)) == first
    assert tracker.disabled_reason is None


def test_tracker_disables_itself_on_embedding_failure():
    embedder = _FakeEmbedder()
    tracker = SpeakerTracker(embedder)
    assert tracker.identify(_clip(0)) is not None
    embedder.fail = SpeakerUnavailable("inference failed")
    assert tracker.identify(_clip(0)) is None
    embedder.fail = None
    assert tracker.identify(_clip(0)) is None  # stays off for the session
    assert tracker.disabled_reason == "inference failed"


def test_tracker_swallows_unexpected_errors():
    embedder = _FakeEmbedder()
    embedder.fail = RuntimeError("boom")
    tracker = SpeakerTracker(embedder)
    assert tracker.identify(_clip(0)) is None
    assert "boom" in (tracker.disabled_reason or "")


def test_create_returns_none_without_model(tmp_path):
    assert SpeakerTracker.create(tmp_path / "missing.onnx") is None


def test_create_returns_none_without_onnxruntime(tmp_path, monkeypatch):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")
    monkeypatch.setitem(__import__("sys").modules, "onnxruntime", None)
    assert SpeakerTracker.create(model) is None


def test_corrupt_model_degrades_instead_of_raising(tmp_path):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"not an onnx file")
    tracker = SpeakerTracker.create(model)
    if tracker is None:  # onnxruntime not installed in this env
        return
    assert tracker.identify(_clip(0, 2.0)) is None
    assert tracker.disabled_reason


def test_fbank_shape():
    feats = fbank(np.zeros(SAMPLE_RATE, dtype=np.float32))
    assert feats.shape == (1, 98, 80) and feats.dtype == np.float32


# -- pipeline: speaker rides the final event; absent when unavailable -------
class _NullCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return False


class _LockProxy:
    asr_lock = _NullCM()


async def _run_worker(monkeypatch, speakers):
    monkeypatch.setattr(pipeline, "_do_asr_streaming", lambda audio, cb: ("テスト", 0.01))
    monkeypatch.setattr(pipeline, "_models", _LockProxy())
    asr_q: asyncio.Queue = asyncio.Queue()
    display_q: asyncio.Queue = asyncio.Queue()
    for s in (0, 1):
        await asr_q.put((_clip(s), time.perf_counter(), 2.0, 0.1))
    await asr_q.put(None)
    await pipeline.asr_worker(
        asr_q,
        display_q,
        time.perf_counter(),
        asyncio.get_running_loop(),
        False,
        speakers=speakers,
    )
    out = []
    while not display_q.empty():
        item = display_q.get_nowait()
        if item is not None and item.kind == "final":
            out.append(item)
    return out


async def test_asr_worker_attaches_speaker(monkeypatch):
    finals = await _run_worker(monkeypatch, SpeakerTracker(_FakeEmbedder()))
    assert [f.speaker["id"] for f in finals] == ["speaker-1", "speaker-2"]


async def test_asr_worker_without_tracker_has_no_speaker(monkeypatch):
    finals = await _run_worker(monkeypatch, None)
    assert len(finals) == 2 and all(f.speaker is None for f in finals)


# -- adapter: events, rename, archive, export --------------------------------
def _final(speaker=None, ts=1.0, ja="はい。"):
    ev = {"kind": "final", "ts": ts, "ja": ja, "en": "Yes.", "duration_s": 1.0}
    if speaker:
        ev["speaker"] = speaker
    return ev


def _state_with_two_speakers() -> APISessionState:
    state = APISessionState()
    state.speakers = SpeakerTracker(_FakeEmbedder())
    state.speakers.identify(_clip(0))
    state.speakers.identify(_clip(1))
    state.handle_pipeline_event({"kind": "session_start", "wav": "x"})
    state.handle_pipeline_event(_final({"id": "speaker-1", "label": "Speaker 1"}, 1.0))
    state.handle_pipeline_event(_final({"id": "speaker-2", "label": "Speaker 2"}, 3.0))
    state.handle_pipeline_event(_final({"id": "speaker-1", "label": "Speaker 1"}, 5.0))
    return state


def test_final_speaker_maps_onto_swift_events_additively():
    state = _state_with_two_speakers()
    speakers = [
        e["speaker"]["id"]
        for e in state.events
        if e["type"] in ("AsrCompletedEvent", "TranslationCompletedEvent")
    ]
    assert speakers == [
        "speaker-1",
        "speaker-1",
        "speaker-2",
        "speaker-2",
        "speaker-1",
        "speaker-1",
    ]


def test_final_without_speaker_has_no_speaker_key():
    state = APISessionState()
    state.handle_pipeline_event(_final())
    assert all("speaker" not in e for e in state.events)


def test_rename_updates_past_rows_and_emits_event():
    state = _state_with_two_speakers()
    queue: asyncio.Queue = asyncio.Queue()
    state.add_client(queue)
    assert state.rename_speaker("speaker-1", "Kyoko") is True
    labels = {e["speaker"]["id"]: e["speaker"]["label"] for e in state.events if "speaker" in e}
    assert labels == {"speaker-1": "Kyoko", "speaker-2": "Speaker 2"}
    sent = json.loads(queue.get_nowait())
    assert sent == {
        "type": "SpeakerRenamedEvent",
        "message": "speaker-1=Kyoko",
        "speaker": {"id": "speaker-1", "label": "Kyoko"},
        "timestamp_s": sent["timestamp_s"],
    }
    assert state.speakers.identify(_clip(0)).label == "Kyoko"  # later rows too


def test_rename_unknown_or_without_tracker_fails():
    state = _state_with_two_speakers()
    assert state.rename_speaker("speaker-7", "Nobody") is False
    state.speakers = None
    assert state.rename_speaker("speaker-1", "Nobody") is False


def test_rename_route(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mimivox import api_adapter

    state = _state_with_two_speakers()
    monkeypatch.setattr(api_adapter, "api_state", state)
    app = FastAPI()
    app.include_router(api_adapter.router)
    client = TestClient(app)
    ok = client.post("/api/session/speakers/rename", json={"id": "speaker-2", "label": "Sato"})
    assert ok.status_code == 200 and ok.json()["speaker"]["label"] == "Sato"
    assert (
        client.post(
            "/api/session/speakers/rename", json={"id": "speaker-9", "label": "X"}
        ).status_code
        == 404
    )
    assert client.post("/api/session/speakers/rename", json={"id": "speaker-1"}).status_code == 400


def test_rename_archived_speaker_updates_saved_transcript_and_search(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mimivox import api_adapter, session_archive

    monkeypatch.setattr(session_archive, "_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(session_archive, "_INDEX_PATH", tmp_path / "sessions" / "index.json")
    monkeypatch.setattr(api_adapter, "DATA_DIR", str(tmp_path))
    session_archive.archive_session(
        "archived-speaker",
        [
            {
                "type": "AsrCompletedEvent",
                "index": 1,
                "start": 0.0,
                "end": 1.0,
                "text": "こんにちは",
                "speaker": {"id": "speaker-1", "label": "Speaker 1"},
            }
        ],
        label="sample",
    )
    session_archive.save_notes("archived-speaker", {"summary_en": "Keep this note"})
    app = FastAPI()
    app.include_router(api_adapter.router)
    client = TestClient(app)

    response = client.post(
        "/api/sessions/archived-speaker/speakers/rename",
        json={"id": "speaker-1", "label": "Sato"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["speaker"] == {"id": "speaker-1", "label": "Sato"}
    saved = session_archive.load_session("archived-speaker")
    assert saved["events"][0]["speaker"]["label"] == "Sato"
    assert saved["notes"]["summary_en"] == "Keep this note"
    assert session_archive.search_sessions(speaker="Sato")["results"][0]["id"] == "archived-speaker"

    export = client.post("/api/session/export?format=markdown&session_id=archived-speaker")
    assert export.status_code == 200
    assert "Sato" in Path(export.json()["path"]).read_text("utf-8")

    missing = client.post(
        "/api/sessions/archived-speaker/speakers/rename",
        json={"id": "speaker-9", "label": "Nobody"},
    )
    assert missing.status_code == 404


def test_rename_after_end_updates_archive(monkeypatch, tmp_path):
    from mimivox import session_archive

    monkeypatch.setattr(session_archive, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(session_archive, "_INDEX_PATH", tmp_path / "index.json")
    state = _state_with_two_speakers()
    state.session_id = "sid-1"
    state.handle_pipeline_event({"kind": "session_end"})
    assert state.events == []
    assert state.rename_speaker("speaker-2", "Sato") is True
    archived = session_archive.load_session("sid-1")
    labels = {
        e["speaker"]["id"]: e["speaker"]["label"] for e in archived["events"] if "speaker" in e
    }
    assert labels["speaker-2"] == "Sato"


def test_export_includes_speaker():
    state = _state_with_two_speakers()
    rows = pair_events(state.events)
    assert [r["speaker"] for r in rows] == ["Speaker 1", "Speaker 2", "Speaker 1"]
    assert "Speaker 2" in render("markdown", state.events)
    assert "Speaker 2: はい。" in render("text", state.events)
    assert "Speaker 2: " in render("srt", state.events)
    assert render("csv", state.events).splitlines()[0] == "start,end,ja,en,speaker"
    assert json.loads(render("jsonl", state.events).splitlines()[1])["speaker"] == "Speaker 2"


def test_export_without_speaker_is_unchanged():
    state = APISessionState()
    state.handle_pipeline_event(_final())
    assert render("csv", state.events).splitlines()[0] == "start,end,ja,en"
    assert "speaker" not in render("jsonl", state.events)
