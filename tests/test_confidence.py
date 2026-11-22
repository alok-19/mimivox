"""Caption confidence: pure scoring, signal extraction, event field, export."""

from __future__ import annotations

import asyncio

import pytest

from mimivox import pipeline
from mimivox.api_adapter import APISessionState
from mimivox.confidence import (
    AsrSignals,
    MtSignals,
    asr_signals_from_segments,
    assess,
    merge_asr,
    mt_signals_from_logprobs,
)
from mimivox.config import ASRResult, reset_translation_cache
from mimivox.export_session import (
    pair_events,
    render_csv,
    render_jsonl,
    render_markdown,
    render_text,
)

GOOD_ASR = AsrSignals(avg_logprob=-0.2, no_speech_prob=0.01, compression_ratio=1.1, duration_s=3)
GOOD_MT = MtSignals(mean_logprob=-0.1)


def test_clean_signals_are_high_with_no_reasons():
    c = assess(GOOD_ASR, GOOD_MT, "Hello there")
    assert c.level == "high" and c.reasons == ()
    assert c.asr == pytest.approx(0.819, abs=1e-3) and c.mt == pytest.approx(0.905, abs=1e-3)


def test_missing_signals_never_lower_confidence():
    assert assess(None, None, "Hello").level == "high"
    assert assess(None, None, "Hello").asr is None


@pytest.mark.parametrize(
    "asr, level",
    [
        (AsrSignals(avg_logprob=-0.5), "medium"),
        (AsrSignals(avg_logprob=-0.9), "low"),
        (AsrSignals(no_speech_prob=0.4), "medium"),
        (AsrSignals(no_speech_prob=0.7), "low"),
        (AsrSignals(compression_ratio=2.1), "medium"),
        (AsrSignals(compression_ratio=3.0), "low"),
        (AsrSignals(repeats_collapsed=True), "medium"),
    ],
)
def test_asr_thresholds(asr, level):
    c = assess(asr, GOOD_MT, "ok")
    assert c.level == level and c.reasons


@pytest.mark.parametrize(
    "mt, level",
    [
        (MtSignals(mean_logprob=-0.5), "medium"),
        (MtSignals(mean_logprob=-1.0), "low"),
        (MtSignals(prompt_leak_cleaned=True), "medium"),
        (MtSignals(japanese_stripped=True), "low"),
    ],
)
def test_mt_thresholds_and_repair_flags(mt, level):
    assert assess(GOOD_ASR, mt, "ok").level == level


def test_empty_translation_is_low():
    c = assess(GOOD_ASR, GOOD_MT, "  ")
    assert c.level == "low" and "No translation produced" in c.reasons


def test_level_is_the_worst_signal_and_reasons_list_worst_first():
    c = assess(AsrSignals(avg_logprob=-0.5), MtSignals(japanese_stripped=True), "x")
    assert c.level == "low"
    assert c.reasons[0] == "Untranslated Japanese was removed"
    assert len(c.reasons) == 2


def test_as_dict_shape():
    d = assess(GOOD_ASR, None, "x").as_dict()
    assert set(d) == {"level", "reasons", "asr", "mt"} and d["mt"] is None


def test_asr_signals_aggregate_segments():
    sig = asr_signals_from_segments(
        [
            {
                "start": 0,
                "end": 3,
                "avg_logprob": -0.2,
                "no_speech_prob": 0.1,
                "compression_ratio": 1.0,
            },
            {
                "start": 3,
                "end": 4,
                "avg_logprob": -1.0,
                "no_speech_prob": 0.5,
                "compression_ratio": 2.2,
            },
        ]
    )
    assert sig.avg_logprob == pytest.approx(-0.4)  # duration weighted
    assert sig.no_speech_prob == 0.5 and sig.compression_ratio == 2.2
    assert asr_signals_from_segments([]) is None and asr_signals_from_segments(None) is None


def test_merge_asr_weights_logprob_and_keeps_worst():
    a = AsrSignals(avg_logprob=-0.2, no_speech_prob=0.1, compression_ratio=1.0, duration_s=3)
    b = AsrSignals(
        avg_logprob=-1.0,
        no_speech_prob=0.5,
        compression_ratio=1.5,
        duration_s=1,
        repeats_collapsed=True,
    )
    m = merge_asr(a, b)
    assert m.avg_logprob == pytest.approx(-0.4) and m.no_speech_prob == 0.5
    assert m.repeats_collapsed and merge_asr(None, b) is b and merge_asr(None, None) is None


def test_mt_signals_mean():
    assert mt_signals_from_logprobs([-0.2, -0.4]).mean_logprob == pytest.approx(-0.3)
    assert mt_signals_from_logprobs([]).mean_logprob is None


def _asr(text, start, dur, signals):
    return ASRResult(
        kind="final",
        text=text,
        audio_ts="00:00",
        wall_time="00:00:00",
        asr_latency=0.1,
        duration_s=dur,
        timeline_s=start,
        asr_signals=signals,
    )


class _Stub:
    async def translate(self, ja, token_cb, *, priority="final", context=()):
        return (f"EN<{ja}>", 0.01, MtSignals(mean_logprob=-0.1))


async def _display(monkeypatch, items):
    reset_translation_cache()
    monkeypatch.setattr(pipeline, "get_translator", lambda: _Stub())
    q: asyncio.Queue = asyncio.Queue()
    for it in items:
        q.put_nowait(it)
    q.put_nowait(None)
    events: list[dict] = []
    rows: list[dict] = []
    await pipeline.display_worker(
        q, rows, asyncio.get_running_loop(), sse_broadcast=events.append, partial_en_enabled=False
    )
    return events, rows


async def test_final_event_carries_confidence(monkeypatch):
    shaky = AsrSignals(avg_logprob=-1.3, duration_s=2)
    events, rows = await _display(
        monkeypatch,
        [
            _asr("田中さんから新しい見積もりが届きました。", 0.0, 2.0, GOOD_ASR),
            _asr("先月より二割高いですね。", 4.0, 2.0, shaky),
            _asr("来週までに返事をしないといけないですね。", 8.0, 2.0, None),
        ],
    )
    finals = [e for e in events if e["kind"] == "final"]
    assert [f["confidence"]["level"] for f in finals] == ["high", "low", "high"]
    assert finals[1]["confidence"]["reasons"] == ["Speech recognition was unsure"]
    assert rows[1]["confidence"] == finals[1]["confidence"]


async def test_revision_recomputes_confidence_from_both_halves(monkeypatch):
    events, rows = await _display(
        monkeypatch,
        [
            _asr("部長には報告しましたが、", 4.0, 2.0, GOOD_ASR),
            _asr("まだ返事がありません。", 6.8, 2.0, AsrSignals(avg_logprob=-2.4, duration_s=2)),
        ],
    )
    final = next(e for e in events if e["kind"] == "final")
    revision = next(e for e in events if e["kind"] == "revision")
    assert final["confidence"]["level"] == "high"
    assert revision["confidence"]["level"] == "low"  # weighted -1.08 logprob
    assert rows[0]["confidence"] == revision["confidence"]


def test_adapter_forwards_confidence_to_swift_events():
    state = APISessionState()
    conf = {"level": "medium", "reasons": ["Translation was unsure"], "asr": 0.8, "mt": 0.4}
    state.handle_pipeline_event(
        {
            "kind": "final",
            "id": 1,
            "ts": 1.0,
            "ja": "はい。",
            "en": "Yes.",
            "duration_s": 1.0,
            "confidence": conf,
        }
    )
    state.handle_pipeline_event(
        {
            "kind": "revision",
            "target_id": 1,
            "merged_ids": [2],
            "ts": 1.0,
            "ja": "はい。ええ。",
            "en": "Yes, yes.",
            "duration_s": 2.0,
            "confidence": {**conf, "level": "low"},
        }
    )
    by_type = {e["type"]: e for e in state.events}
    assert by_type["AsrCompletedEvent"]["confidence"] == conf
    assert by_type["TranslationCompletedEvent"]["confidence"] == conf
    assert by_type["TranscriptRevisionEvent"]["confidence"]["level"] == "low"


def _events(level="low"):
    conf = {"level": level, "reasons": ["Speech recognition was unsure"], "asr": 0.3, "mt": None}
    return [
        {
            "type": "AsrCompletedEvent",
            "index": 1,
            "start": 1.0,
            "end": 2.0,
            "text": "はい。",
            "confidence": conf,
        },
        {
            "type": "TranslationCompletedEvent",
            "index": 1,
            "start": 1.0,
            "end": 2.0,
            "text": "Yes.",
            "source_indexes": [1],
            "confidence": conf,
        },
        {"type": "AsrCompletedEvent", "index": 2, "start": 3.0, "end": 4.0, "text": "いいえ。"},
        {
            "type": "TranslationCompletedEvent",
            "index": 2,
            "start": 3.0,
            "end": 4.0,
            "text": "No.",
            "source_indexes": [2],
        },
    ]


def test_export_rows_and_formats_include_confidence():
    rows = pair_events(_events())
    assert rows[0]["confidence"]["level"] == "low" and "confidence" not in rows[1]
    assert '"confidence": {"level": "low"' in render_jsonl(rows).splitlines()[0]
    assert "confidence" not in render_jsonl(rows).splitlines()[1]
    assert "> ⚠ low confidence: Speech recognition was unsure" in render_markdown(rows)
    assert "[low confidence: Speech recognition was unsure]" in render_text(rows)
    csv_lines = render_csv(rows).splitlines()
    assert csv_lines[0].endswith("confidence,confidence_reasons")
    assert csv_lines[1].endswith("low,Speech recognition was unsure") and csv_lines[2].endswith(
        ",,"
    )


def test_export_high_confidence_is_not_flagged_and_revision_wins():
    assert "⚠" not in render_markdown(pair_events(_events("high")))
    events = _events("high") + [
        {
            "type": "TranscriptRevisionEvent",
            "index": 1,
            "start": 1.0,
            "end": 2.0,
            "text": "Yes.",
            "ja": "はい。",
            "merged_indexes": [],
            "confidence": {"level": "medium", "reasons": ["Translation was unsure"]},
        }
    ]
    assert pair_events(events)[0]["confidence"]["level"] == "medium"


def test_export_without_confidence_is_unchanged():
    plain = [{k: v for k, v in e.items() if k != "confidence"} for e in _events()]
    assert not render_csv(pair_events(plain)).splitlines()[0].endswith("confidence_reasons")
    assert "⚠" not in render_markdown(pair_events(plain))
