from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox import catch_up, export_session, gap_recovery, pipeline, session_archive
from mimivox.api_adapter import APISessionState, api_state, router
from mimivox.catch_up import apply_catch_up
from mimivox.config import SAMPLE_RATE, ASRResult
from mimivox.latency_health import (
    CatchUpMode,
    DebugDelay,
    HealthState,
    LatencyMonitor,
    next_state,
    parse_pmset_therm,
)
from mimivox.session_control import SessionControl


def test_state_enters_and_clears_with_hysteresis():
    s = HealthState.OK
    s = next_state(s, 3.9)
    assert s is HealthState.OK
    s = next_state(s, 4.0)
    assert s is HealthState.BEHIND
    s = next_state(s, 2.5)
    assert s is HealthState.BEHIND
    s = next_state(s, 10.0)
    assert s is HealthState.OVERLOADED
    s = next_state(s, 7.0)
    assert s is HealthState.OVERLOADED
    s = next_state(s, 5.0)
    assert s is HealthState.BEHIND
    s = next_state(s, 1.9)
    assert s is HealthState.OK


def test_idle_snapshot_is_ok_with_three_stages():
    snap = LatencyMonitor().snapshot()
    assert snap.state is HealthState.OK
    assert [s.stage for s in snap.stages] == ["vad", "asr", "cat"]
    assert snap.reasons == ()
    assert snap.catch_up_available is False


def test_asr_slower_than_realtime_is_named_and_recommends_skip():
    mon = LatencyMonitor()
    for _ in range(3):
        mon.asr_end(audio_s=2.0, latency_s=3.0)
    snap = mon.snapshot([(5.0, 0.0), (5.0, 5.0)], now_s=12.0, skippable=1)
    assert snap.state is HealthState.OVERLOADED
    assert snap.reasons[0].code == "asr_slow"
    assert "1.5x" in snap.reasons[0].message
    assert snap.recommended_mode is CatchUpMode.SKIP
    assert snap.catch_up_available is True


def test_translation_bottleneck_recommends_fast():
    mon = LatencyMonitor()
    mon.cat_end(audio_s=3.0, latency_s=8.0)
    snap = mon.snapshot([], [(3.0, 0.0), (3.0, 3.0)], now_s=5.0)
    assert snap.state is HealthState.BEHIND
    assert [r.code for r in snap.reasons] == ["translate_slow"]
    assert snap.recommended_mode is CatchUpMode.FAST


def test_gpu_and_thermal_reasons_only_when_not_ok():
    mon = LatencyMonitor(gpu_probe=lambda: True, thermal_probe=lambda: 70.0)
    assert mon.snapshot().reasons == ()
    codes = [r.code for r in mon.snapshot([(6.0, 0.0)], now_s=6.0).reasons]
    assert codes[:2] == ["notes_gpu", "thermal"]


def test_probe_failure_is_not_a_reason():
    def boom() -> bool:
        raise RuntimeError

    snap = LatencyMonitor(gpu_probe=boom, thermal_probe=lambda: None).snapshot(
        [(6.0, 0.0)], now_s=6.0
    )
    assert [r.code for r in snap.reasons] == ["asr_slow"]


def test_backlog_is_the_age_of_the_oldest_unfinished_speech():
    mon = LatencyMonitor()
    mon.asr_begin(audio_s=15.0, end_s=15.0)
    assert mon.snapshot(now_s=17.0).state is HealthState.OK
    snap = mon.snapshot(now_s=20.0)
    assert (snap.backlog_s, snap.state) == (5.0, HealthState.BEHIND)
    assert snap.catch_up_available is False
    mon.asr_end(audio_s=15.0, latency_s=5.0)
    assert mon.snapshot(now_s=21.0).backlog_s == 0.0


def test_only_queued_items_make_catch_up_available():
    mon = LatencyMonitor()
    queued = [(3.0, 3.0)]
    assert mon.snapshot(queued, now_s=9.0, skippable=0).catch_up_available is False
    assert mon.snapshot(queued, now_s=9.0, skippable=1).catch_up_available is True
    assert (
        mon.snapshot(queued, now_s=9.0, catching_up=True, skippable=1).catch_up_available is False
    )


def test_parse_pmset_therm():
    out = "Note: No thermal warning level has been recorded\nCPU_Speed_Limit \t= 80\n"
    assert parse_pmset_therm(out) == 80.0
    assert parse_pmset_therm("nothing here") is None


def test_snapshot_dict_shape():
    d = LatencyMonitor().snapshot([(6.0, 0.0)], now_s=6.0).as_dict()
    assert d["state"] == "behind"
    assert set(d) >= {
        "backlog_s",
        "stages",
        "reasons",
        "catching_up",
        "catch_up_available",
        "recommended_mode",
        "skipped_translations",
        "gaps",
        "gap_audio_s",
        "skippable",
    }
    assert d["stages"][1]["queue_depth"] == 1


async def test_debug_delay_parses_and_counts_down():
    assert DebugDelay.from_env({}).seconds == 0
    assert DebugDelay.from_env({"MIMIVOX_DEBUG_DELAY": "bogus"}).seconds == 0
    d = DebugDelay.from_env({"MIMIVOX_DEBUG_DELAY": "cat:0.01:2"})
    assert (d.stage, d.seconds, d.remaining) == ("cat", 0.01, 2)
    t0 = time.perf_counter()
    await d.apply("asr")
    await d.apply("cat")
    await d.apply("cat")
    await d.apply("cat")
    assert d.remaining == 0
    assert 0.02 <= time.perf_counter() - t0 < 0.5


def _audio_item(seconds: float, end_s: float, skip: bool | None = None):
    audio = np.full(int(seconds * SAMPLE_RATE), 0.1, dtype=np.float32)
    item = (audio, time.perf_counter(), end_s, 0.0)
    return item if skip is None else (*item, skip)


def _control() -> tuple[SessionControl, list[dict]]:
    control = SessionControl()
    control.clock_origin_perf = time.perf_counter() - 30
    control.asr_q = asyncio.Queue(maxsize=4)
    control.display_q = asyncio.Queue(maxsize=16)
    sent: list[dict] = []
    control.broadcast = sent.append
    return control, sent


def _final(text: str, duration_s: float = 3.0, ts: float = 0.0) -> ASRResult:
    return ASRResult(
        kind="final",
        text=text,
        audio_ts="00:00",
        wall_time="00:00:00",
        asr_latency=0.1,
        duration_s=duration_s,
        timeline_s=ts,
    )


async def test_catch_up_is_a_noop_when_ok():
    control, _ = _control()
    assert apply_catch_up(control) is None
    assert control.catch_up_active is False


async def test_fast_catch_up_flags_all_but_newest_final():
    control, _ = _control()
    for i in range(4):
        control.display_q.put_nowait(_final(f"line {i}"))
    result = apply_catch_up(control)
    assert result is not None and result.mode is CatchUpMode.FAST
    assert result.translations_skipped == 3 and result.gaps == 0
    flags = [control.display_q.get_nowait().skip_translation for _ in range(4)]
    assert flags == [True, True, True, False]
    assert control.catch_up_active is True


async def test_newest_final_is_skipped_too_when_newer_audio_exists():
    control, _ = _control()
    control.display_q.put_nowait(_final("a"))
    control.display_q.put_nowait(_final("b", ts=3.0))
    assert control.skip_plan() == (1, 0)
    control.asr_q.put_nowait(_audio_item(3, 9.0))
    assert control.skip_plan() == (2, 0)
    result = apply_catch_up(control)
    assert result.translations_skipped == 2
    assert control.catch_up_active is True


async def test_a_lone_queued_final_is_not_skippable_and_pauses_stop_the_clock():
    control, _ = _control()
    control.display_q.put_nowait(_final("only"))
    snap = control.latency_snapshot()
    assert snap.state is HealthState.OVERLOADED and snap.catch_up_available is False
    control.pause()
    frozen = control.realtime_s()
    await asyncio.sleep(0.05)
    assert control.realtime_s() == pytest.approx(frozen, abs=0.01)
    control.resume()
    assert control.realtime_s() == pytest.approx(frozen, abs=0.05)


async def test_fast_catch_up_flags_queued_audio_and_survives_sentinel():
    control, _ = _control()
    control.asr_q.put_nowait(_audio_item(3, 3.0))
    control.asr_q.put_nowait(_audio_item(3, 6.0))
    control.asr_q.put_nowait(None)
    result = apply_catch_up(control)
    assert result.translations_skipped == 1
    first, second, sentinel = (control.asr_q.get_nowait() for _ in range(3))
    assert first[4] is True and len(second) == 4 and sentinel is None


async def test_live_speech_merged_after_a_catch_up_is_still_translated():
    from mimivox.audio import enqueue_asr

    control, _ = _control()
    control.asr_q.put_nowait(_audio_item(3, 3.0))
    control.asr_q.put_nowait(_audio_item(3, 6.0))
    apply_catch_up(control)
    await enqueue_asr(control.asr_q, _audio_item(2, 8.0), drop_oldest_when_full=True)
    (merged,) = (control.asr_q.get_nowait() for _ in range(control.asr_q.qsize()))
    assert merged[2] == 8.0 and len(merged) == 4


async def test_skip_catch_up_records_every_dropped_span_as_a_gap():
    control, sent = _control()
    saved: dict[int, int] = {}
    control.gap_sink = lambda gid, audio: saved.setdefault(gid, len(audio)) > 0
    for end in (3.0, 6.0, 9.0):
        control.asr_q.put_nowait(_audio_item(3, end))
    control.asr_q.put_nowait(None)
    result = apply_catch_up(control, CatchUpMode.SKIP)
    assert result.gaps == 2 and result.gap_audio_s == pytest.approx(6.0)
    gaps = [e for e in sent if e["kind"] == "gap"]
    assert [(g["ts"], g["end"], g["recoverable"]) for g in gaps] == [
        (0.0, 3.0, True),
        (3.0, 6.0, True),
    ]
    assert len(saved) == 2 and all(n == 3 * SAMPLE_RATE for n in saved.values())
    kept = [control.asr_q.get_nowait() for _ in range(2)]
    assert kept[0][2] == 9.0 and kept[1] is None
    assert control.skip_ahead_requested is True
    snap = control.latency_snapshot()
    assert (snap.gaps, snap.gap_audio_s) == (2, pytest.approx(6.0))


async def test_gap_without_a_sink_is_announced_but_not_recoverable():
    control, sent = _control()
    catch_up.record_gap(control, np.zeros(SAMPLE_RATE, dtype=np.float32), 1.0, 2.0)
    assert sent[0]["recoverable"] is False


async def test_slow_translator_drives_behind_then_catch_up_then_ok(monkeypatch):
    from mimivox import translate as tr

    translated: list[str] = []

    async def _slow(self, ja, token_cb, priority="final", context=()):
        translated.append(ja)
        await asyncio.sleep(0.15)
        return (f"EN {ja}", 0.15)

    monkeypatch.setattr(tr.CatTranslator, "translate", _slow)
    pipeline._TRANSLATION_CACHE.clear()
    control, sent = _control()
    log: list = []
    for i in range(5):
        control.display_q.put_nowait(_final(f"発言{i}", ts=3.0 * i))
    worker = asyncio.create_task(
        pipeline.display_worker(
            control.display_q,
            log,
            asyncio.get_running_loop(),
            sse_broadcast=control.broadcast,
            control=control,
            partial_en_enabled=False,
        )
    )
    await asyncio.sleep(0.05)
    behind = control.latency_snapshot()
    assert behind.state is HealthState.OVERLOADED
    assert behind.reasons[0].code == "translate_slow"

    result = apply_catch_up(control)
    assert result.translations_skipped == 3
    control.display_q.put_nowait(None)
    await asyncio.wait_for(worker, timeout=5)

    assert translated == ["発言0。", "発言4。"]
    finals = [e for e in sent if e["kind"] == "final"]
    assert [bool(e.get("translation_skipped")) for e in finals] == [False, True, True, True, False]
    assert [e["en"] for e in finals][1:4] == ["", "", ""]
    assert [e["ja"] for e in finals] == [f"発言{i}" for i in range(5)]
    assert [r["id"] for r in log] == [1, 2, 3, 4, 5]
    assert control.latency_snapshot().state is HealthState.OK
    assert control.catch_up_active is False


async def test_catching_up_skips_new_finals_that_still_have_backlog(monkeypatch):
    from mimivox import translate as tr

    async def _fast(self, ja, token_cb, priority="final", context=()):
        return ("EN", 0.0)

    monkeypatch.setattr(tr.CatTranslator, "translate", _fast)
    pipeline._TRANSLATION_CACHE.clear()
    control, sent = _control()
    control.catch_up_active = True
    control.display_q.put_nowait(_final("遅れた発言", ts=0.0))
    control.display_q.put_nowait(_final("最新の発言", ts=3.0))
    control.display_q.put_nowait(None)
    await pipeline.display_worker(
        control.display_q,
        [],
        asyncio.get_running_loop(),
        sse_broadcast=control.broadcast,
        control=control,
        partial_en_enabled=False,
    )
    older, newest = [e for e in sent if e["kind"] == "final"]
    assert older["translation_skipped"] == "catch_up" and older["en"] == ""
    assert "translation_skipped" not in newest and newest["en"] == "EN"
    assert control.skipped_translations == 1


def test_gap_and_skipped_events_map_to_stored_meeting_events():
    state = APISessionState()
    state.handle_pipeline_event({"kind": "session_start", "wav": "x.wav"})
    state.handle_pipeline_event(
        {
            "kind": "final",
            "id": 1,
            "ts": 1.0,
            "ja": "遅れた",
            "en": "",
            "duration_s": 2.0,
            "translation_skipped": "catch_up",
        }
    )
    state.handle_pipeline_event(
        {
            "kind": "gap",
            "id": 2,
            "ts": 4.0,
            "end": 9.0,
            "duration_s": 5.0,
            "reason": "catch_up",
            "recoverable": True,
        }
    )
    types = [e["type"] for e in state.events]
    assert types == ["SessionStartedEvent", "AsrCompletedEvent", "CaptionGapEvent"]
    asr, gap = state.events[1], state.events[2]
    assert asr["translation_skipped"] == "catch_up"
    assert (gap["index"], gap["start"], gap["end"], gap["recoverable"]) == (2, 4.0, 9.0, True)
    assert "5s" in gap["message"]


def test_tick_health_emits_and_auto_mode_catches_up(monkeypatch):
    state = APISessionState()
    state.pipeline_task = MagicMock()
    state.pipeline_task.done.return_value = False
    state.control = _control_sync()
    state.settings = {**state.settings, "catch_up_mode": "auto"}
    sent: list[dict] = []
    monkeypatch.setattr(state, "emit", lambda ev, store=True: sent.append(ev))
    state._tick_health()
    assert sent[-1]["type"] == "LatencyHealthEvent"
    assert sent[-1]["health"]["state"] == "overloaded"
    assert sent[-1]["health"]["catching_up"] is True
    assert state.control.skipped_translations == 2


def test_manual_mode_never_auto_catches_up(monkeypatch):
    state = APISessionState()
    state.pipeline_task = MagicMock()
    state.pipeline_task.done.return_value = False
    state.control = _control_sync()
    state.settings = {**state.settings, "catch_up_mode": "manual"}
    monkeypatch.setattr(state, "emit", lambda ev, store=True: None)
    state._tick_health()
    assert state.control.catch_up_active is False


def _control_sync() -> SessionControl:
    control = SessionControl()
    control.clock_origin_perf = time.perf_counter() - 30
    control.asr_q = asyncio.Queue(maxsize=4)
    control.display_q = asyncio.Queue(maxsize=16)
    for i in range(3):
        control.display_q.put_nowait(_final(f"n{i}", duration_s=5.0))
    return control


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _reset_api_state():
    api_state.pipeline_task = None
    api_state.state = "idle"
    api_state.control = None
    api_state.session_id = None


def test_catch_up_endpoint_requires_a_session():
    _reset_api_state()
    resp = _client().post("/api/session/catch-up")
    assert resp.status_code == 400


def test_catch_up_endpoint_reports_result_and_health():
    api_state.pipeline_task = MagicMock()
    api_state.pipeline_task.done.return_value = False
    api_state.state = "listening"
    api_state.session_id = "sid"
    api_state.control = _control_sync()
    try:
        client = _client()
        assert client.post("/api/session/catch-up", json={"mode": "bogus"}).status_code == 400
        resp = client.post("/api/session/catch-up", json={"mode": "fast"})
        body = resp.json()
        assert resp.status_code == 200 and body["message"] == "caught up"
        assert body["translations_skipped"] == 2 and body["mode"] == "fast"
        assert body["health"]["catching_up"] is True
        status = client.get("/api/status").json()
        assert status["health"]["latency"]["state"] in ("behind", "overloaded")
        assert status["health"]["latency"]["skipped_translations"] == 2
    finally:
        _reset_api_state()


def test_status_without_session_has_an_ok_latency_snapshot():
    _reset_api_state()
    latency = _client().get("/api/status").json()["health"]["latency"]
    assert latency["state"] == "ok"


def test_catch_up_mode_setting_defaults_to_manual_and_rejects_junk():
    from mimivox.settings_store import catch_up_mode, default_settings

    assert default_settings()["catch_up_mode"] == "manual"
    assert catch_up_mode({"catch_up_mode": "auto"}) == "auto"
    assert catch_up_mode({"catch_up_mode": "nonsense"}) == "manual"


@pytest.fixture
def archive_root(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session_archive, "_SESSIONS_DIR", root)
    monkeypatch.setattr(session_archive, "_INDEX_PATH", root / "index.json")
    return root


def _skipped_events() -> list[dict]:
    return [
        {"type": "AsrCompletedEvent", "index": 1, "start": 0.0, "end": 2.0, "text": "最初"},
        {
            "type": "TranslationCompletedEvent",
            "index": 1,
            "start": 0.0,
            "end": 2.0,
            "text": "First.",
            "source_indexes": [1],
        },
        {
            "type": "AsrCompletedEvent",
            "index": 2,
            "start": 2.0,
            "end": 4.0,
            "text": "翻訳なし",
            "translation_skipped": "catch_up",
        },
        {
            "type": "CaptionGapEvent",
            "index": 3,
            "start": 4.0,
            "end": 9.0,
            "recoverable": True,
            "reason": "catch_up",
        },
        {
            "type": "CaptionGapEvent",
            "index": 4,
            "start": 9.0,
            "end": 12.0,
            "recoverable": False,
            "reason": "catch_up",
        },
    ]


def test_gap_audio_round_trips_and_is_deleted_with_the_session(archive_root):
    tone = (np.sin(np.linspace(0, 200, SAMPLE_RATE)) * 0.3).astype(np.float32)
    assert session_archive.save_gap_audio("sid", 3, tone) is True
    audio = gap_recovery._load_wav(session_archive.gap_audio_file("sid", 3))
    assert len(audio) == SAMPLE_RATE and np.abs(audio - tone).max() < 1e-3
    session_archive.archive_session("sid", [])
    assert session_archive.delete_session("sid") is True
    assert not session_archive.gaps_dir("sid").exists()


def test_gap_audio_rejects_unsafe_session_ids(archive_root):
    assert session_archive.save_gap_audio("../evil", 1, np.zeros(10, dtype=np.float32)) is False


async def test_recovery_fills_gaps_and_untranslated_rows(archive_root):
    session_archive.archive_session("sid", _skipped_events())
    session_archive.save_gap_audio("sid", 3, np.full(SAMPLE_RATE, 0.1, dtype=np.float32))

    async def transcribe(audio):
        return "回復した発言"

    async def translate(ja):
        return f"EN[{ja}]"

    report = await gap_recovery.recover_session("sid", transcribe, translate)
    assert (report.recovered_gaps, report.retranslated, report.remaining) == (1, 1, 1)
    events = session_archive.load_session("sid")["events"]
    rows = export_session.pair_events(events)
    by_index = {r["index"]: r for r in rows}
    assert by_index[2]["en"] == "EN[翻訳なし]" and "skipped" not in by_index[2]
    assert (by_index[3]["ja"], by_index[3]["en"]) == ("回復した発言", "EN[回復した発言]")
    assert by_index[4]["skipped"] == "gap"
    assert gap_recovery.pending(events) == [e for e in events if e["index"] == 4]
    again = await gap_recovery.recover_session("sid", transcribe, translate)
    assert (again.recovered_gaps, again.retranslated, again.remaining) == (0, 0, 1)


async def test_recovery_of_silent_gap_removes_it(archive_root):
    session_archive.archive_session("sid", _skipped_events()[3:4])
    session_archive.save_gap_audio("sid", 3, np.full(SAMPLE_RATE, 0.1, dtype=np.float32))

    async def silent(_):
        return ""

    async def translate(ja):
        return ja

    report = await gap_recovery.recover_session("sid", silent, translate, indexes={3})
    assert report.recovered_gaps == 1
    assert session_archive.load_session("sid")["events"] == []
    assert await gap_recovery.recover_session("missing", silent, translate) is None


async def test_model_failure_never_discards_a_gap(archive_root):
    session_archive.archive_session("sid", _skipped_events()[3:4])
    session_archive.save_gap_audio("sid", 3, np.full(SAMPLE_RATE, 0.1, dtype=np.float32))

    async def boom(_):
        raise RuntimeError("model down")

    async def ok(ja):
        return "x"

    report = await gap_recovery.recover_session("sid", boom, ok)
    assert (report.recovered_gaps, report.remaining) == (0, 1)
    assert [e["type"] for e in session_archive.load_session("sid")["events"]] == ["CaptionGapEvent"]

    async def heard(_):
        return "聞こえた"

    async def translate_down(_):
        raise RuntimeError("cat down")

    report = await gap_recovery.recover_session("sid", heard, translate_down)
    assert report.recovered_gaps == 1
    (asr,) = session_archive.load_session("sid")["events"]
    assert (asr["text"], asr["translation_skipped"]) == ("聞こえた", "catch_up")
    assert len(gap_recovery.pending([asr])) == 1


def test_recover_endpoint_is_refused_while_a_session_runs(archive_root):
    api_state.pipeline_task = MagicMock()
    api_state.pipeline_task.done.return_value = False
    try:
        assert _client().post("/api/sessions/sid/recover").status_code == 409
    finally:
        _reset_api_state()


def test_recover_endpoint_404_for_unknown_session(archive_root):
    _reset_api_state()
    assert _client().post("/api/sessions/nope/recover").status_code == 404


def test_exports_show_gaps_and_untranslated_rows(archive_root):
    events = _skipped_events()
    rows = export_session.pair_events(events)
    assert [r.get("skipped") for r in rows] == [None, "translation", "gap", "gap"]
    md = export_session.render("markdown", events)
    assert "> ⏭ not translated while catching up" in md
    assert "[Skipped 5s of speech while catching up]" in md
    assert "(not recoverable)" in md
    text = export_session.render("text", events)
    assert "[skipped while catching up]" in text
    assert "[Skipped 3s of speech while catching up]" in export_session.render("srt", events)
    assert '"skipped": "gap"' in export_session.render("jsonl", events)
