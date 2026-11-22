"""Golden-string tests for session export writers."""

from __future__ import annotations

from pathlib import Path

import pytest

from mimivox.export_session import (
    SUPPORTED_FORMATS,
    pair_events,
    render,
    render_csv,
    render_jsonl,
    render_markdown,
    render_srt,
    render_text,
    render_vtt,
    write_export,
)


def _two_events() -> list[dict]:
    return [
        {
            "type": "AsrCompletedEvent",
            "index": 1,
            "start": 6.2,
            "end": 8.9,
            "text": "では始めさせていただきます。",
        },
        {
            "type": "TranslationCompletedEvent",
            "index": 1,
            "start": 6.2,
            "end": 8.9,
            "text": "Let me begin.",
            "source_indexes": [1],
        },
        {
            "type": "AsrCompletedEvent",
            "index": 2,
            "start": 9.0,
            "end": 10.0,
            "text": "次です。",
        },
        {
            "type": "TranslationCompletedEvent",
            "index": 2,
            "start": 9.0,
            "end": 10.0,
            "text": "Next.",
            "source_indexes": [2],
        },
    ]


def test_pair_events_two_rows():
    rows = pair_events(_two_events())
    assert len(rows) == 2
    assert rows[0]["ja"] == "では始めさせていただきます。"
    assert rows[0]["en"] == "Let me begin."
    assert rows[0]["start"] == 6.2
    assert rows[0]["end"] == 8.9
    assert rows[1]["en"] == "Next."


def test_render_srt_golden():
    rows = pair_events(_two_events())
    out = render_srt(rows)
    assert out == (
        "1\n"
        "00:00:06,200 --> 00:00:08,900\n"
        "では始めさせていただきます。\n"
        "Let me begin.\n"
        "\n"
        "2\n"
        "00:00:09,000 --> 00:00:10,000\n"
        "次です。\n"
        "Next.\n"
    )


def test_render_vtt_golden():
    rows = pair_events(_two_events())
    out = render_vtt(rows)
    assert out.startswith("WEBVTT\n")
    assert "00:00:06.200 --> 00:00:08.900" in out
    assert "Let me begin." in out
    assert "00:00:09.000 --> 00:00:10.000" in out
    assert "Next." in out


def test_render_markdown_has_ja_en_blocks():
    out = render_markdown(pair_events(_two_events()))
    assert out.startswith("## Transcript")
    assert "**JA**" in out
    assert "**EN**" in out
    assert "Let me begin." in out


def test_render_csv_header_and_rows():
    out = render_csv(pair_events(_two_events()))
    lines = out.strip().splitlines()
    assert lines[0] == "start,end,ja,en"
    assert "6.200" in lines[1]
    assert "Let me begin." in lines[1]
    assert len(lines) == 3


def test_render_jsonl_two_objects():
    out = render_jsonl(pair_events(_two_events()))
    lines = [ln for ln in out.strip().splitlines() if ln]
    assert len(lines) == 2
    assert '"en": "Let me begin."' in lines[0]
    assert '"en": "Next."' in lines[1]


def test_render_text_plain():
    out = render_text(pair_events(_two_events()))
    assert "JA: では始めさせていただきます。" in out
    assert "EN: Let me begin." in out
    assert "EN: Next." in out


def test_render_unknown_raises():
    with pytest.raises(ValueError, match="Unknown export format"):
        render("docx", _two_events())


def test_write_export_all_formats(tmp_path: Path):
    events = _two_events()
    for fmt in sorted(SUPPORTED_FORMATS):
        path = write_export(
            fmt,
            events,
            session_id="sess-test",
            settings={"latency_quality": "fast"},
            out_dir=tmp_path,
        )
        assert path.exists()
        assert path.stat().st_size > 0
        text = path.read_text(encoding="utf-8")
        if fmt == "srt":
            assert "-->" in text
        if fmt == "vtt":
            assert text.startswith("WEBVTT")
        if fmt == "csv":
            assert text.startswith("start,end,ja,en")


def test_api_export_unknown_format_400():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mimivox.api_adapter import api_state, router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    api_state.events = _two_events()
    api_state.session_id = "export-sid"
    resp = client.post("/api/session/export?format=docx")
    assert resp.status_code == 400
    assert "Unknown format" in resp.json()["message"]


def test_api_export_srt_writes_file(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mimivox import export_session
    from mimivox.api_adapter import api_state, router

    # write_export uses out_dir kw; patch Path used inside write via monkeypatch
    # of the export call site by overriding write_export's default via wrapping.
    original = export_session.write_export

    def _write(*args, **kwargs):
        kwargs["out_dir"] = tmp_path
        return original(*args, **kwargs)

    monkeypatch.setattr("mimivox.export_session.write_export", _write)
    # api imports write_export inside the handler — patch module attr used by import
    monkeypatch.setattr(
        "mimivox.export_session.write_export",
        _write,
    )

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    api_state.events = _two_events()
    api_state.session_id = "export-sid"

    # Handler does `from mimivox.export_session import ... write_export` each call,
    # so patching the module attribute is enough.
    resp = client.post("/api/session/export?format=srt")
    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "exported as srt"
    assert body["session_id"] == "export-sid"
    path = Path(body["path"])
    assert path.exists()
    assert "Let me begin." in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Empty-session edge cases — all formats must produce non-empty valid output
# ---------------------------------------------------------------------------


def test_render_srt_empty_is_valid():
    out = render_srt([])
    assert len(out) > 0
    assert out == "\n"


def test_render_vtt_empty_has_header():
    out = render_vtt([])
    assert "WEBVTT" in out
    assert len(out) > 0


def test_render_jsonl_empty_is_valid():
    out = render_jsonl([])
    assert len(out) > 0
    assert out == "\n"


def test_render_text_empty_has_placeholder():
    out = render_text([])
    assert "no utterances" in out
    assert len(out) > 0


def test_render_csv_empty_has_header():
    out = render_csv([])
    assert "start,end,ja,en" in out


def test_render_markdown_empty_has_note():
    out = render_markdown([])
    assert "No utterances" in out


def test_write_export_empty_all_formats(tmp_path):
    for fmt in SUPPORTED_FORMATS:
        p = write_export(fmt, [], session_id="empty-test", out_dir=tmp_path)
        assert p.exists(), f"{fmt}: file not created"
        assert p.stat().st_size > 0, f"{fmt}: file is empty"


def test_pinned_rows_marked_and_filtered():
    events = _two_events()
    rows = pair_events(events, [2])
    assert [r.get("pinned") for r in rows] == [None, True]
    assert [r["en"] for r in pair_events(events, [2], pinned_only=True)] == ["Next."]
    # No pins: rows are byte-identical to before (no new keys).
    assert all("pinned" not in r and "_asr" not in r for r in pair_events(events))


def test_pinned_markdown_json_and_filename(tmp_path: Path):
    events = _two_events()
    md = render("markdown", events, pinned_ids=[1])
    assert "### 1. [00:06.20 → 00:08.90] ★" in md
    assert "### 2. [00:09.00 → 00:10.00]\n" in md
    js = render("json", events, pinned_ids=[2], pinned_only=True)
    assert '"pinned_ids": [\n    2\n  ]' in js
    path = write_export(
        "markdown",
        events,
        session_id="s1",
        out_dir=tmp_path,
        pinned_ids=[2],
        pinned_only=True,
    )
    assert path.name == "s1.pinned.md"
    assert "Next." in path.read_text() and "Let me begin." not in path.read_text()


def test_pin_matches_asr_index_when_translation_index_differs():
    events = [
        {"type": "AsrCompletedEvent", "index": 3, "start": 9.0, "end": 10.0, "text": "次です"},
        {
            "type": "TranslationCompletedEvent",
            "index": 99,
            "start": 9.0,
            "end": 10.0,
            "text": "Next.",
            "source_indexes": [3],
        },
    ]
    assert pair_events(events, [3])[0]["pinned"] is True
