"""Meeting notes / Ask: parsing, validation, chunking, API contract (fake LLM, no models)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox import meeting_notes as mn
from mimivox import notes_llm, session_archive
from mimivox.api_adapter import api_state, router
from mimivox.export_session import render


def _events(n: int = 4) -> list[dict]:
    evs: list[dict] = []
    for i in range(1, n + 1):
        sp = {"id": "A", "label": "Tanaka"}
        evs.append(
            {
                "type": "AsrCompletedEvent",
                "index": i,
                "text": f"日本語{i}",
                "start": i,
                "end": i + 1,
                "speaker": sp,
            }
        )
        evs.append(
            {
                "type": "TranslationCompletedEvent",
                "index": i,
                "text": f"english {i}",
                "start": i,
                "end": i + 1,
                "source_indexes": [i],
                "speaker": sp,
            }
        )
    return evs


GOOD = json.dumps(
    {
        "title_en": '"Release date."',
        "summary_en": "Ship on the 24th.",
        "summary_ja": "24日にリリース。",
        "decisions": [{"text": "Postpone", "text_ja": "延期", "line_ids": [1, 2]}],
        "action_items": [
            {"owner": "Suzuki", "text": "Send schedule", "due": "Wednesday", "line_ids": [3]}
        ],
    }
)


class Fake:
    """Replies from a queue (or a function of the messages) and records calls."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def __call__(self, messages, *, max_tokens):
        self.calls.append(messages)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return r(messages) if callable(r) else r


def test_build_lines_uses_event_ids_speakers_and_revisions():
    evs = _events(2) + [
        {"type": "TranscriptRevisionEvent", "index": 2, "ja": "改訂", "text": "revised 2"}
    ]
    lines = mn.build_lines(evs)
    assert [ln.id for ln in lines] == [1, 2]
    assert lines[1].ja == "改訂" and lines[1].en == "revised 2"
    assert lines[0].render() == "[1] Tanaka: 日本語1 (EN: english 1)"


def test_parse_notes_valid_with_fences_and_prefill():
    n = mn.parse_notes("```json\n" + GOOD + "\n```", {1, 2, 3})
    assert n.summary_en == "Ship on the 24th."
    assert n.title_en == "Release date"
    assert mn.MeetingNotes.from_dict(n.to_dict()).title_en == "Release date"
    assert n.decisions[0].line_ids == [1, 2] and n.decisions[0].text_ja == "延期"
    assert n.action_items[0].owner == "Suzuki" and n.action_items[0].due == "Wednesday"


def test_parse_notes_drops_uncited_and_invented_ids():
    raw = json.dumps(
        {
            "summary_en": "s",
            "decisions": [
                {"text": "real", "line_ids": ["1", 99]},
                {"text": "invented", "line_ids": [99]},
                {"text": "no cite", "line_ids": []},
            ],
            "action_items": [{"owner": "null", "text": "do it", "due": None, "line_ids": [2]}],
        }
    )
    n = mn.parse_notes(raw, {1, 2})
    assert [d.text for d in n.decisions] == ["real"]
    assert n.decisions[0].line_ids == [1]
    assert n.action_items[0].owner is None and n.action_items[0].due is None


@pytest.mark.parametrize(
    "raw", ["not json", "[1,2]", '{"decisions": []}', '{"summary_en":"x","decisions":"no"}']
)
def test_parse_notes_rejects_unusable(raw):
    with pytest.raises(ValueError):
        mn.parse_notes(raw, {1})


def test_one_retry_on_invalid_json_then_ok():
    gen = Fake("oops", GOOD)
    notes = mn.generate_notes(gen, mn.build_lines(_events(4)))
    assert notes.summary_ja == "24日にリリース。"
    assert len(gen.calls) == 2
    assert gen.calls[1][-1]["role"] == "user" and "invalid" in gen.calls[1][-1]["content"]


def test_two_invalid_replies_raise_clear_error():
    gen = Fake("oops", "still not json")
    with pytest.raises(mn.NotesError, match="invalid JSON twice"):
        mn.generate_notes(gen, mn.build_lines(_events(4)))
    assert len(gen.calls) == 2


def test_empty_transcript_is_an_error():
    with pytest.raises(mn.NotesError):
        mn.generate_notes(Fake(GOOD), [])


def test_chunking_respects_budget_and_keeps_order():
    lines = mn.build_lines(_events(30))
    chunks = mn.chunk_lines(lines, budget=60)
    assert len(chunks) > 3
    assert [ln.id for c in chunks for ln in c] == [ln.id for ln in lines]
    assert all(
        sum(mn.estimate_tokens(ln.render()) + 1 for ln in c) <= 60 or len(c) == 1 for c in chunks
    )


def test_map_reduce_merges_summaries_keeps_cited_items_and_dedupes():
    lines = mn.build_lines(_events(30))

    def reply(messages):
        user = messages[-1]["content"]
        if user.startswith("Part 1"):  # reduce step
            return json.dumps(
                {"title_en": "Merged title", "summary_en": "merged", "summary_ja": "統合"}
            )
        ids = [int(x) for x in __import__("re").findall(r"^\[(\d+)\]", user, __import__("re").M)]
        return json.dumps(
            {
                "summary_en": f"part {ids[0]}",
                "summary_ja": "部分",
                "decisions": [
                    {"text": "Same decision", "line_ids": [ids[0]]},
                    {"text": f"d{ids[0]}", "line_ids": [ids[-1]]},
                ],
                "action_items": [],
            }
        )

    gen = Fake(reply)
    notes = mn.generate_notes(gen, lines, budget=400)
    assert len(mn.chunk_lines(lines, 400)) > 1
    assert (notes.title_en, notes.summary_en, notes.summary_ja) == (
        "Merged title",
        "merged",
        "統合",
    )
    texts = [d.text for d in notes.decisions]
    assert texts.count("Same decision") == 1 and len(texts) > 2
    assert all(i in {ln.id for ln in lines} for d in notes.decisions for i in d.line_ids)


def test_chunk_citations_cannot_point_outside_their_chunk():
    lines = mn.build_lines(_events(30))
    gen2 = Fake(
        lambda m: json.dumps(
            {"summary_en": "s", "summary_ja": "j", "decisions": [{"text": "x", "line_ids": [30]}]}
        )
    )
    notes = mn.generate_notes(gen2, lines, budget=400)
    assert len(mn.chunk_lines(lines, 400)) > 1
    # id 30 is only valid in the last chunk, so earlier chunks drop the decision
    assert len(notes.decisions) == 1


def test_ask_returns_answer_with_validated_citations():
    lines = mn.build_lines(_events(4))
    gen = Fake(json.dumps({"answer": "Friday.", "line_ids": [2, 77]}))
    ans = mn.answer_question(gen, lines, " When? ")
    assert ans.answer == "Friday." and ans.line_ids == [2]
    assert "Question: When?" in gen.calls[0][-1]["content"]


def test_ask_long_meeting_reduces_only_cited_partials():
    lines = mn.build_lines(_events(30))

    def reply(messages):
        user = messages[-1]["content"]
        if user.startswith("Candidate answers"):
            return json.dumps({"answer": "final", "line_ids": [1]})
        return json.dumps({"answer": "p", "line_ids": [1] if "[1]" in user else []})

    ans = mn.answer_question(Fake(reply), lines, "q", budget=400)
    assert ans.answer == "final" and ans.line_ids == [1]


def test_ask_not_discussed_when_no_chunk_cites():
    lines = mn.build_lines(_events(30))
    ans = mn.answer_question(
        Fake(json.dumps({"answer": "n/a", "line_ids": []})), lines, "q", budget=400
    )
    assert ans.line_ids == [] and "not discussed" in ans.answer


def test_unanswered_live_question_comes_back_phrased_in_japanese():
    lines = mn.build_lines(_events(4))
    gen = Fake(
        json.dumps({"answer": "Not discussed.", "line_ids": []}),
        json.dumps({"ja": "来週の会議はいつですか？"}),
    )
    ans = mn.answer_question(gen, lines, "When is next week's meeting?", suggest_phrase=True)
    assert ans.to_dict()["ask_in_japanese"] == {
        "ja": "来週の会議はいつですか？",
        "romaji": "raishuu no kaigi wa itsu desu ka?",
    }
    assert gen.calls[1][-1]["content"] == "When is next week's meeting?"


def test_answered_or_archived_questions_get_no_phrase():
    lines = mn.build_lines(_events(4))
    answered = Fake(json.dumps({"answer": "Friday.", "line_ids": [2]}))
    assert mn.answer_question(answered, lines, "q", suggest_phrase=True).ask_in_japanese is None
    assert len(answered.calls) == 1
    archived = Fake(json.dumps({"answer": "n/a", "line_ids": []}))
    assert "ask_in_japanese" not in mn.answer_question(archived, lines, "q").to_dict()
    assert len(archived.calls) == 1


def test_a_failed_phrase_still_returns_the_answer():
    lines = mn.build_lines(_events(4))
    gen = Fake(
        json.dumps({"answer": "Not discussed.", "line_ids": []}), "{}", "{}", '{"ja": "hello"}'
    )
    ans = mn.answer_question(gen, lines, "q", suggest_phrase=True)
    assert ans.answer == "Not discussed." and ans.ask_in_japanese is None


def test_notes_roundtrip_and_markdown_cites_row_numbers():
    notes = mn.parse_notes(GOOD, {1, 2, 3})
    again = mn.MeetingNotes.from_dict(notes.to_dict())
    assert again == notes
    md = render("markdown", _events(3), notes=notes.to_dict())
    assert md.index("## Summary") < md.index("## Transcript")
    assert "Send schedule (owner: Suzuki, due: Wednesday) _(lines 3)_" in md
    assert "## Summary" not in render("markdown", _events(3))


# --- API contract (notes_llm.generate faked; archive in tmp) -------------------
@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session_archive, "_SESSIONS_DIR", root)
    monkeypatch.setattr(session_archive, "_INDEX_PATH", root / "index.json")
    monkeypatch.setattr(api_state, "events", [])
    monkeypatch.setattr(api_state, "session_id", None)
    monkeypatch.setattr(api_state, "notes_cache", {})
    calls: list[str] = []

    def fake_generate(messages, *, max_tokens, pause=None):
        calls.append(messages[0]["content"][:20])
        if messages[0]["content"].startswith("You answer"):
            return json.dumps({"answer": "Friday.", "line_ids": [2]})
        return GOOD

    monkeypatch.setattr(notes_llm, "generate", fake_generate)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    c.calls = calls  # type: ignore[attr-defined]
    return c


def test_notes_for_archived_session_is_cached_in_archive(client):
    session_archive.archive_session("s1", _events(4), label="x")
    r = client.post("/api/session/notes", json={"session_id": "s1"})
    assert r.status_code == 200
    body = r.json()
    assert body["summary_en"] and body["decisions"][0]["line_ids"] == [1, 2]
    assert body["action_items"][0]["owner"] == "Suzuki"
    assert body["meta"]["cached"] is False and body["meta"]["llm_calls"] == 1
    assert "notes" in session_archive.load_session("s1")

    again = client.post("/api/session/notes", json={"session_id": "s1"}).json()
    assert again["meta"] == {"cached": True} and len(client.calls) == 1
    client.post("/api/session/notes", json={"session_id": "s1", "regenerate": True})
    assert len(client.calls) == 2


def test_notes_for_current_session_cached_until_transcript_grows(client):
    api_state.session_id, api_state.events = "live", _events(4)
    assert client.post("/api/session/notes", json={}).status_code == 200
    assert client.post("/api/session/notes", json={}).json()["meta"]["cached"] is True
    api_state.events = _events(5)
    assert client.post("/api/session/notes", json={}).json()["meta"]["cached"] is False


def test_ask_answers_with_line_ids(client):
    session_archive.archive_session("s1", _events(4), label="x")
    r = client.post("/api/session/ask", json={"session_id": "s1", "question": "When?"})
    assert r.status_code == 200 and r.json()["answer"] == "Friday." and r.json()["line_ids"] == [2]


def test_ask_suggests_japanese_only_during_a_live_meeting(client, monkeypatch):
    def gen(messages, *, max_tokens, pause=None):
        if messages[0]["content"].startswith("You answer"):
            return json.dumps({"answer": "Not discussed.", "line_ids": []})
        return json.dumps({"ja": "予算はいくらですか？"})

    monkeypatch.setattr(notes_llm, "generate", gen)
    monkeypatch.setattr(type(api_state), "is_running", property(lambda self: True))
    api_state.session_id, api_state.events = "live", _events(4)
    body = client.post("/api/session/ask", json={"question": "Budget?"}).json()
    assert body["ask_in_japanese"]["romaji"] == "yosan wa ikura desu ka?"

    session_archive.archive_session("s1", _events(4), label="x")
    body = client.post("/api/session/ask", json={"session_id": "s1", "question": "Budget?"}).json()
    assert "ask_in_japanese" not in body


def test_api_errors(client, monkeypatch):
    assert client.post("/api/session/notes", json={}).status_code == 404
    assert client.post("/api/session/notes", json={"session_id": "nope"}).status_code == 404
    assert client.post("/api/session/ask", json={"question": " "}).status_code == 400
    session_archive.archive_session("empty", [], label="x")
    assert client.post("/api/session/notes", json={"session_id": "empty"}).status_code == 422
    session_archive.archive_session("s1", _events(4), label="x")
    monkeypatch.setattr(notes_llm, "generate", lambda m, *, max_tokens, pause=None: "garbage")
    r = client.post("/api/session/notes", json={"session_id": "s1"})
    assert r.status_code == 502 and "invalid JSON" in r.json()["message"]

    def boom(m, *, max_tokens, pause=None):
        raise RuntimeError("model not downloaded")

    monkeypatch.setattr(notes_llm, "generate", boom)
    r = client.post("/api/session/ask", json={"session_id": "s1", "question": "q"})
    assert r.status_code == 503
    assert r.json()["message"] == "The notes model is unavailable."


def test_export_includes_cached_notes(client, tmp_path, monkeypatch):
    from mimivox import api_adapter

    monkeypatch.setattr(api_adapter, "DATA_DIR", str(tmp_path))
    session_archive.archive_session("s1", _events(4), label="x")
    client.post("/api/session/notes", json={"session_id": "s1"})
    path = client.post("/api/session/export?format=markdown&session_id=s1").json()["path"]
    assert "## Action items" in Path(path).read_text(encoding="utf-8")


def test_models_report_notes_llm_and_unload(client):
    r = client.post("/api/models/verify", json={}).json()["model_readiness"]
    assert r["notes_llm"]["model_id"] and "ready" in r["notes_llm"]
    assert client.post("/api/models/unload-notes", json={}).status_code == 200


def test_notes_llm_loads_lazily_and_unloads(monkeypatch):
    assert not notes_llm.is_loaded()  # nothing loads at import / server startup
    loads: list[str] = []
    import mlx_lm

    monkeypatch.setattr(mlx_lm, "load", lambda mid: (loads.append(mid), ("m", "t"))[1])
    notes_llm.load("some/model")
    notes_llm.load("some/model")
    assert loads == ["some/model"] and notes_llm.loaded_id() == "some/model"
    assert notes_llm.unload() is True and not notes_llm.is_loaded()


def test_notes_llm_calls_wait_for_the_shared_mlx_lock(monkeypatch):
    import asyncio

    from mimivox import notes_service
    from mimivox.config import _models

    monkeypatch.setattr(
        notes_llm,
        "generate",
        lambda m, *, max_tokens, pause=None: json.dumps({"answer": "ok", "line_ids": [1]}),
    )
    monkeypatch.setattr(notes_service, "IDLE_UNLOAD_S", 3600)

    async def go():
        _models.asr_lock = asyncio.Lock()  # what the live pipeline holds during Whisper/CAT
        lines = mn.build_lines(_events(2))
        async with _models.asr_lock:
            task = asyncio.create_task(notes_service.ask(lines, "q"))
            await asyncio.sleep(0.3)
            assert not task.done()  # blocked behind live inference
        ans, meta = await asyncio.wait_for(task, 5)
        assert ans.answer == "ok" and meta["llm_calls"] == 1
        notes_service._idle_task.cancel()

    try:
        asyncio.run(go())
    finally:
        _models.asr_lock = None


# --- Notes written when a saved meeting ends ----------------------------------
def _end_meeting(monkeypatch, *, model_on_disk: bool, n_lines: int = 4) -> list[dict]:
    """Run session_end for a meeting with ``n_lines`` lines; returns pushed events."""
    import asyncio

    from mimivox import models_service

    pushed: list[dict] = []
    monkeypatch.setattr(models_service, "model_present", lambda _id: (model_on_disk, "", None))
    monkeypatch.setattr(api_state, "_push", pushed.append)
    monkeypatch.setattr(api_state, "auto_notes", {})
    monkeypatch.setattr(api_state, "control", None)
    monkeypatch.setattr(api_state, "_archived", False)
    api_state.session_id, api_state.events = "ended", _events(n_lines)

    async def end() -> None:
        api_state.handle_pipeline_event({"kind": "session_end"})
        await asyncio.gather(*api_state.auto_notes.values())

    asyncio.run(end())
    return pushed


def test_ending_a_meeting_writes_its_notes_and_title(client, monkeypatch):
    pushed = _end_meeting(monkeypatch, model_on_disk=True)
    assert session_archive.load_session("ended")["notes"]["summary_en"] == "Ship on the 24th."
    assert [e["message"] for e in pushed if e["type"] == "NotesReadyEvent"] == ["ended"]
    entry = next(e for e in session_archive.list_sessions() if e["id"] == "ended")
    assert entry["headline"]


@pytest.mark.parametrize("model_on_disk, n_lines", [(False, 4), (True, 2)])
def test_no_auto_notes_without_model_or_for_tiny_meetings(
    client, monkeypatch, model_on_disk, n_lines
):
    pushed = _end_meeting(monkeypatch, model_on_disk=model_on_disk, n_lines=n_lines)
    assert "notes" not in session_archive.load_session("ended")
    assert not [e for e in pushed if e["type"] == "NotesReadyEvent"] and client.calls == []
