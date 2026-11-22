"""Caption revision: trigger, revision event, context replacement, export."""

from __future__ import annotations

import asyncio

import pytest

from mimivox import pipeline
from mimivox.api_adapter import APISessionState
from mimivox.config import REVISION_MAX_GAP_S, ASRResult, reset_translation_cache
from mimivox.export_session import pair_events, render_text
from mimivox.revision import is_split_continuation


@pytest.mark.parametrize(
    "prev, nxt, gap, expected",
    [
        ("部長には報告しましたが、", "まだ返事がありません。", 0.8, True),
        ("部長には報告しましたが", "まだ返事がありません。", 0.8, True),  # no punctuation
        ("報告しましたが。", "まだ返事がありません。", 0.8, True),  # strong conjunction + 。
        ("部長には報告しましたが、", "まだ返事がありません。", REVISION_MAX_GAP_S + 0.1, False),
        ("先月より二割高いですね。", "来週までに返事をしないといけないですね。", 0.5, False),
        ("それで、部長には報告しましたか。", "まだです。", 0.5, False),
        ("本当ですか？", "はい", 0.3, False),
        ("待って。", "これを見てください。", 0.5, False),  # complete reply ending in て
        ("忙しいから。", "また今度。", 0.5, False),
        ("はい", "わかりました。", 0.5, False),  # bare filler is not a clause
        ("", "まだです。", 0.5, False),
        ("あ" * 70 + "、", "い" * 60 + "。", 0.5, False),  # joined length cap
    ],
)
def test_is_split_continuation(prev, nxt, gap, expected):
    assert is_split_continuation(prev, nxt, gap) is expected


def _asr(text: str, start: float, dur: float) -> ASRResult:
    return ASRResult(
        kind="final",
        text=text,
        audio_ts="00:00",
        wall_time="00:00:00",
        asr_latency=0.1,
        duration_s=dur,
        timeline_s=start,
    )


class _StubTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def translate(self, ja, token_cb, *, priority="final", context=()):
        self.calls.append((ja, tuple(context)))
        return (f"EN<{ja}>", 0.01)


async def _run_display(monkeypatch, items: list[ASRResult]):
    reset_translation_cache()
    stub = _StubTranslator()
    monkeypatch.setattr(pipeline, "get_translator", lambda: stub)
    q: asyncio.Queue = asyncio.Queue()
    for it in items:
        q.put_nowait(it)
    q.put_nowait(None)
    events: list[dict] = []
    rows: list[dict] = []
    await pipeline.display_worker(
        q,
        rows,
        asyncio.get_running_loop(),
        sse_broadcast=events.append,
        partial_en_enabled=False,
    )
    return stub, events, rows


async def test_split_sentence_emits_revision_and_merges(monkeypatch):
    items = [
        _asr("先月より二割高いですね。", 0.0, 2.0),
        _asr("部長には報告しましたが、", 4.0, 2.0),
        _asr("まだ返事がありません。", 6.8, 2.0),  # 0.8 s gap
        _asr("そうですか。", 12.0, 1.0),
    ]
    stub, events, rows = await _run_display(monkeypatch, items)

    finals = [e for e in events if e["kind"] == "final"]
    revisions = [e for e in events if e["kind"] == "revision"]
    assert [f["id"] for f in finals] == [1, 2, 4]  # id 3 was merged away
    assert len(revisions) == 1
    rev = revisions[0]
    assert rev["target_id"] == 2
    assert rev["merged_ids"] == [3]
    assert rev["reason"] == "merged_fragment"
    assert rev["ja"] == "部長には報告しましたが、まだ返事がありません。"
    assert rev["en"] == "EN<部長には報告しましたが、まだ返事がありません。>"
    assert rev["duration_s"] == pytest.approx(4.8)
    # The merged row replaces the fragment row in the log.
    assert [r["ja"] for r in rows][1] == rev["ja"]
    assert len(rows) == 3


async def test_merged_translation_uses_context_before_first_half(monkeypatch):
    items = [
        _asr("先月より二割高いですね。", 0.0, 2.0),
        _asr("部長には報告しましたが、", 4.0, 2.0),
        _asr("まだ返事がありません。", 6.8, 2.0),
        _asr("そうですか。", 12.0, 1.0),
    ]
    stub, _, _ = await _run_display(monkeypatch, items)
    by_ja = {ja: ctx for ja, ctx in stub.calls}
    joined = "部長には報告しましたが、まだ返事がありません。"
    # Context is [line 1] only: the fragment is not fed back to CAT.
    assert [ja for ja, _ in by_ja[joined]] == ["先月より二割高いですね。"]
    # The line after the merge sees line 1 + the merged line, no fragments.
    after = by_ja["そうですか。"]
    assert [ja for ja, _ in after] == ["先月より二割高いですね。", joined]


async def test_unsplit_sentences_never_revise(monkeypatch):
    items = [
        _asr("田中さんから新しい見積もりが届きました。", 0.0, 3.0),
        _asr("先月より二割高いですね。", 5.0, 2.0),
        _asr("来週までに返事をしないといけないですね。", 8.0, 3.0),
    ]
    _, events, rows = await _run_display(monkeypatch, items)
    assert not [e for e in events if e["kind"] == "revision"]
    assert len(rows) == 3


def test_context_restore_drops_later_lines():
    from mimivox.translate import TranslationContext

    ctx = TranslationContext()
    ctx.add("一つ目です。", "One.")
    snap = ctx.pairs()
    ctx.add("二つ目です。", "Two.")
    ctx.restore(snap)
    assert ctx.pairs() == snap


def test_adapter_maps_ids_and_revision_event():
    state = APISessionState()
    state.handle_pipeline_event({"kind": "session_start", "wav": "x"})
    state.handle_pipeline_event(
        {
            "kind": "final",
            "id": 2,
            "ts": 4.0,
            "ja": "部長には報告しましたが、",
            "en": "I told the manager, but",
            "duration_s": 2.0,
        }
    )
    state.handle_pipeline_event(
        {
            "kind": "revision",
            "target_id": 2,
            "merged_ids": [3],
            "ts": 4.0,
            "reason": "merged_fragment",
            "duration_s": 4.8,
            "ja": "部長には報告しましたが、まだ返事がありません。",
            "en": "I told the manager, but I have no reply yet.",
        }
    )
    asr = next(e for e in state.events if e["type"] == "AsrCompletedEvent")
    rev = next(e for e in state.events if e["type"] == "TranscriptRevisionEvent")
    assert asr["index"] == 2
    assert rev["index"] == 2
    assert rev["merged_indexes"] == [3]
    assert rev["text"] == "I told the manager, but I have no reply yet."
    assert rev["ja"].endswith("まだ返事がありません。")
    assert rev["end"] == pytest.approx(8.8)
    # Next id-less final does not collide with the pipeline id space.
    state.handle_pipeline_event({"kind": "final", "ts": 12.0, "ja": "はい。", "en": "Yes."})
    assert [e["index"] for e in state.events if e["type"] == "AsrCompletedEvent"] == [2, 3]


def test_export_uses_revised_text_swift_events():
    state = APISessionState()
    state.handle_pipeline_event({"kind": "session_start", "wav": "x"})
    for ev in (
        {
            "kind": "final",
            "id": 1,
            "ts": 0.0,
            "ja": "先月より二割高いですね。",
            "en": "Higher.",
            "duration_s": 2.0,
        },
        {
            "kind": "final",
            "id": 2,
            "ts": 4.0,
            "ja": "部長には報告しましたが、",
            "en": "Told the, but",
            "duration_s": 2.0,
        },
        {
            "kind": "revision",
            "target_id": 2,
            "merged_ids": [3],
            "ts": 4.0,
            "duration_s": 4.8,
            "reason": "merged_fragment",
            "ja": "部長には報告しましたが、まだ返事がありません。",
            "en": "I told the manager, but there is no reply yet.",
        },
    ):
        state.handle_pipeline_event(ev)
    rows = pair_events(state.events)
    assert [r["en"] for r in rows] == ["Higher.", "I told the manager, but there is no reply yet."]
    assert rows[1]["ja"] == "部長には報告しましたが、まだ返事がありません。"
    assert rows[1]["end"] == pytest.approx(8.8)
    text = render_text(rows)
    assert "Told the, but" not in text


def test_export_uses_revised_text_raw_pipeline_events():
    events = [
        {"kind": "final", "id": 1, "ts": 4.0, "ja": "前半、", "en": "First,", "duration_s": 1.0},
        {
            "kind": "revision",
            "target_id": 1,
            "merged_ids": [2],
            "ts": 4.0,
            "duration_s": 3.0,
            "ja": "前半、後半。",
            "en": "First, second.",
        },
    ]
    rows = pair_events(events)
    assert len(rows) == 1
    assert rows[0]["ja"] == "前半、後半。" and rows[0]["en"] == "First, second."
    assert rows[0]["end"] == pytest.approx(7.0)


def test_revised_row_keeps_first_speaker_in_export():
    s1 = {"id": "speaker-1", "label": "Tanaka"}
    s2 = {"id": "speaker-2", "label": "Speaker 2"}
    events = [
        {
            "kind": "final",
            "id": 1,
            "ts": 4.0,
            "ja": "前半、",
            "en": "First,",
            "duration_s": 1.0,
            "speaker": s1,
        },
        {
            "kind": "final",
            "id": 2,
            "ts": 5.0,
            "ja": "後半。",
            "en": "second.",
            "duration_s": 1.0,
            "speaker": s2,
        },
        {
            "kind": "revision",
            "target_id": 1,
            "merged_ids": [2],
            "ts": 4.0,
            "duration_s": 3.0,
            "ja": "前半、後半。",
            "en": "First, second.",
        },
    ]
    rows = pair_events(events)
    assert len(rows) == 1
    assert rows[0]["speaker"] == "Tanaka" and rows[0]["en"] == "First, second."
    assert "Tanaka:" in render_text(rows) or "Tanaka" in str(render_text(rows))
