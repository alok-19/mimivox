"""Unit tests for session archive + Review list/load/delete API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox import session_archive
from mimivox.api_adapter import APISessionState, api_state, router
from mimivox.session_control import SessionControl


def _patch_sessions_root(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    monkeypatch.setattr(session_archive, "_SESSIONS_DIR", root)
    monkeypatch.setattr(session_archive, "_INDEX_PATH", root / "index.json")


def _sample_events() -> list[dict]:
    return [
        {"type": "SessionStartedEvent", "message": "source=replay-file", "timestamp_s": 0.0},
        {
            "type": "AsrCompletedEvent",
            "index": 1,
            "start": 1.0,
            "end": 2.0,
            "text": "はい",
        },
        {
            "type": "TranslationCompletedEvent",
            "index": 1,
            "start": 1.0,
            "end": 2.0,
            "text": "Yes.",
            "source_indexes": [1],
        },
        {"type": "SessionEndedEvent", "message": "session complete", "timestamp_s": 3.0},
    ]


def test_archive_list_load_delete(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    events = _sample_events()
    path = session_archive.archive_session(
        "sid-1",
        events,
        settings={"latency_quality": "fast"},
        performance_summary={"asr_count": 1},
        started_at="2026-07-18T12:00:00+00:00",
        label="replay-file",
    )
    assert path.exists()
    assert path.name == "sid-1.json"

    listed = session_archive.list_sessions()
    assert len(listed) == 1
    assert listed[0]["id"] == "sid-1"
    assert listed[0]["label"] == "replay-file"
    assert listed[0]["event_count"] == 4
    assert listed[0]["started_at"] == "2026-07-18T12:00:00+00:00"

    loaded = session_archive.load_session("sid-1")
    assert loaded is not None
    assert loaded["id"] == "sid-1"
    assert loaded["events"][1]["text"] == "はい"
    assert loaded["settings"]["latency_quality"] == "fast"
    assert loaded["performance_summary"]["asr_count"] == 1

    assert session_archive.load_session("missing") is None
    assert session_archive.delete_session("sid-1") is True
    assert session_archive.load_session("sid-1") is None
    assert session_archive.list_sessions() == []
    assert session_archive.delete_session("sid-1") is False


def test_archive_index_newest_first(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    session_archive.archive_session("a", [], label="first", started_at="2026-01-01T00:00:00+00:00")
    session_archive.archive_session("b", [], label="second", started_at="2026-01-02T00:00:00+00:00")
    ids = [e["id"] for e in session_archive.list_sessions()]
    assert ids == ["b", "a"]


def test_archive_idempotent_update(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    session_archive.archive_session("sid", [{"type": "A"}], label="v1")
    session_archive.archive_session(
        "sid",
        [{"type": "A"}, {"type": "B"}],
        label="v2",
    )
    listed = session_archive.list_sessions()
    assert len(listed) == 1
    assert listed[0]["label"] == "v2"
    assert listed[0]["event_count"] == 2


def _api_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_sessions_list_get_delete(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    session_archive.archive_session(
        "api-sid",
        _sample_events(),
        label="microphone",
        started_at="2026-07-18T10:00:00+00:00",
    )
    client = _api_client()

    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == "api-sid"

    resp = client.get("/api/sessions/api-sid")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "api-sid"
    assert len(body["events"]) == 4
    assert body["events"][2]["text"] == "Yes."

    resp = client.get("/api/sessions/missing")
    assert resp.status_code == 404

    resp = client.delete("/api/sessions/api-sid")
    assert resp.status_code == 200
    assert resp.json()["message"] == "deleted"
    assert client.get("/api/sessions").json()["sessions"] == []


def test_session_end_archives(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    state = APISessionState()
    state.session_id = "end-sid"
    state.session_label = "replay-file"
    state.started_at_iso = "2026-07-18T11:00:00+00:00"
    state.settings = {"latency_quality": "balanced"}
    state.control = SessionControl()
    state.control.record_final(0.5, had_ja=True, had_en=True)
    state.handle_pipeline_event({"kind": "session_start", "wav": "data/fixtures/x.wav"})
    state.handle_pipeline_event(
        {
            "kind": "final",
            "ts": "00:01",
            "ja": "テスト",
            "en": "Test.",
            "total_ms": 500,
            "duration_s": 1.0,
        }
    )
    state.session_id = "end-sid"
    state.session_label = "replay-file"
    state.started_at_iso = "2026-07-18T11:00:00+00:00"
    state._archived = False
    state.handle_pipeline_event({"kind": "session_end"})

    assert state._archived is True
    # In-memory buffer cleared after archive so SSE cannot replay stale rows.
    assert state.events == []
    loaded = session_archive.load_session("end-sid")
    assert loaded is not None
    assert loaded["label"] == "replay-file"
    assert any(e.get("type") == "AsrCompletedEvent" for e in loaded["events"])
    assert loaded["performance_summary"]["asr_count"] >= 1


def test_export_from_archived_session(tmp_path, monkeypatch):
    from mimivox import export_session

    _patch_sessions_root(monkeypatch, tmp_path)
    session_archive.archive_session(
        "export-arch",
        _sample_events(),
        label="replay-file",
    )

    original = export_session.write_export

    def _write(*args, **kwargs):
        kwargs["out_dir"] = tmp_path / "transcripts"
        return original(*args, **kwargs)

    monkeypatch.setattr(export_session, "write_export", _write)

    api_state.events = []
    api_state.session_id = None
    client = _api_client()
    resp = client.post("/api/session/export?format=text&session_id=export-arch")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "export-arch"
    path = Path(body["path"])
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "はい" in text or "Yes." in text


def test_pins_endpoint_live_and_archived(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    monkeypatch.setattr(api_state, "pinned_ids", [])
    monkeypatch.setattr(api_state, "session_id", "live-sid")
    monkeypatch.setattr(api_state, "_archived", False)
    client = _api_client()

    resp = client.post("/api/session/pins", json={"pinned_ids": [2, 1, 2]})
    assert resp.status_code == 200
    assert api_state.pinned_ids == [1, 2]
    assert client.post("/api/session/pins", json={"pinned_ids": ["x"]}).status_code == 400

    # Archiving persists the live pins; later pin changes reach the archive.
    session_archive.archive_session("live-sid", _sample_events(), pinned_ids=api_state.pinned_ids)
    assert session_archive.load_session("live-sid")["pinned_ids"] == [1, 2]
    monkeypatch.setattr(api_state, "_archived", True)
    client.post("/api/session/pins", json={"pinned_ids": [1]})
    assert session_archive.load_session("live-sid")["pinned_ids"] == [1]

    # An archived session can be pinned by id; unknown ids 404.
    session_archive.archive_session("old", _sample_events())
    assert session_archive.load_session("old")["pinned_ids"] == []
    ok = client.post("/api/session/pins", json={"pinned_ids": [1], "session_id": "old"})
    assert ok.status_code == 200
    assert session_archive.load_session("old")["pinned_ids"] == [1]
    assert (
        client.post("/api/session/pins", json={"pinned_ids": [1], "session_id": "nope"}).status_code
        == 404
    )


def test_export_endpoint_pinned_only(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    monkeypatch.setattr("mimivox.api_adapter.DATA_DIR", tmp_path)
    session_archive.archive_session("old", _sample_events(), pinned_ids=[1])
    client = _api_client()
    resp = client.post("/api/session/export?format=text&pinned_only=true&session_id=old")
    assert resp.status_code == 200
    path = Path(resp.json()["path"])
    assert path.name == "old.pinned.txt"
    assert "Yes." in path.read_text()


def test_delete_session_removes_suffixed_exports_only_for_that_id(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    session_archive.archive_session("sid", [], label="a")
    session_archive.archive_session("sid-2", [], label="b")
    exports = tmp_path / "transcripts"
    exports.mkdir()
    mine = ["sid.md", "sid-vocabulary.csv", "sid-vocabulary-anki.txt"]
    theirs = ["sid-2.md", "sid-2-vocabulary.csv", "sidecar.md", "other-sid.md"]
    for name in mine + theirs:
        (exports / name).write_text("x")

    assert session_archive.delete_session("sid") is True

    assert sorted(p.name for p in exports.iterdir()) == sorted(theirs)


def test_index_entries_carry_duration_and_notes_headline(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    session_archive.archive_session("sid", _sample_events(), label="replay-file")
    [entry] = session_archive.list_sessions()
    assert entry["duration_seconds"] == 2.0
    assert entry["headline"] is None

    notes = {"summary_en": "The team agreed to reply to Tanaka by Friday. Prices rose 20 percent."}
    assert session_archive.save_notes("sid", notes) is True
    [entry] = session_archive.list_sessions()
    assert entry["headline"] == "The team agreed to reply to Tanaka by Friday."


def test_headline_is_trimmed_to_a_row(tmp_path, monkeypatch):
    long_summary = "word " * 40
    headline = session_archive._headline({"notes": {"summary_en": long_summary}})
    assert headline is not None
    assert len(headline) <= 80
    assert headline.endswith("\u2026")
    assert session_archive._headline({"notes": {"summary_en": "  "}}) is None
    assert session_archive._headline(None) is None


def test_headline_prefers_user_title_then_generated_title(tmp_path, monkeypatch):
    notes = {"title_en": "Bedroom sunlight", "summary_en": "The group agrees. More."}
    assert session_archive._headline({"notes": notes}) == "Bedroom sunlight"
    assert session_archive._headline({"notes": notes, "title": "Flat viewing"}) == "Flat viewing"


def test_rename_sets_and_clears_the_title(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    session_archive.archive_session("sid", _sample_events(), label="replay-file")
    session_archive.save_notes("sid", {"title_en": "Bedroom sunlight", "summary_en": "S."})

    assert session_archive.rename_session("sid", "  Flat   viewing ") == "Flat viewing"
    [entry] = session_archive.list_sessions()
    assert entry["headline"] == "Flat viewing"

    # Regenerated notes and a re-archive keep the user's title.
    session_archive.save_notes("sid", {"title_en": "Something else", "summary_en": "S."})
    session_archive.archive_session("sid", _sample_events(), label="replay-file")
    [entry] = session_archive.list_sessions()
    assert entry["headline"] == "Flat viewing"
    assert session_archive.search_sessions(q="flat viewing")["results"]

    assert session_archive.rename_session("sid", "") == ""
    assert session_archive.load_session("sid").get("title") is None
    assert session_archive.rename_session("missing", "x") is None
    with pytest.raises(ValueError):
        session_archive.rename_session("sid", "x" * 121)


def test_list_sessions_backfills_entries_indexed_before_duration(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    session_archive.archive_session("old", _sample_events(), label="replay-file")
    session_archive.save_notes("old", {"summary_en": "Short recap."})
    legacy = [
        {
            "id": "old",
            "started_at": "2026-07-18T12:00:00+00:00",
            "label": "replay-file",
            "event_count": 4,
        }
    ]
    session_archive._write_index(legacy)

    [entry] = session_archive.list_sessions()
    assert entry["duration_seconds"] == 2.0
    assert entry["headline"] == "Short recap."
    assert session_archive._read_index()[0]["duration_seconds"] == 2.0


def test_list_sessions_marks_missing_archives_as_checked(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    session_archive._write_index([{"id": "gone", "started_at": "", "label": "x", "event_count": 0}])
    [entry] = session_archive.list_sessions()
    assert entry["duration_seconds"] is None
    assert "duration_seconds" in session_archive._read_index()[0]


def test_api_rename_meeting(tmp_path, monkeypatch):
    _patch_sessions_root(monkeypatch, tmp_path)
    session_archive.archive_session("api-sid", _sample_events(), label="microphone")
    client = _api_client()

    resp = client.post("/api/sessions/api-sid/title", json={"title": "Flat viewing"})
    assert resp.status_code == 200
    assert resp.json() == {"session_id": "api-sid", "headline": "Flat viewing"}
    assert client.get("/api/sessions").json()["sessions"][0]["headline"] == "Flat viewing"

    resp = client.post("/api/sessions/api-sid/title", json={"title": ""})
    assert resp.json()["headline"] is None
    assert client.post("/api/sessions/nope/title", json={"title": "x"}).status_code == 404
    resp = client.post("/api/sessions/api-sid/title", json={"title": "x" * 200})
    assert resp.status_code == 400
