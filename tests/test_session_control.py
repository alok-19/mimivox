"""Unit tests for SessionControl pause gate and percentile helpers."""

from __future__ import annotations

import asyncio

import pytest

from mimivox.session_control import SessionControl, percentile


def test_percentile_empty():
    assert percentile([], 50) is None


def test_percentile_single():
    assert percentile([1.5], 50) == 1.5
    assert percentile([1.5], 90) == 1.5


def test_percentile_known_values():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(vals, 0) == 1.0
    assert percentile(vals, 100) == 5.0
    assert percentile(vals, 50) == 3.0


def test_default_not_paused():
    ctrl = SessionControl()
    assert ctrl.paused is False


def test_pause_resume():
    ctrl = SessionControl()
    ctrl.pause()
    assert ctrl.paused is True
    ctrl.resume()
    assert ctrl.paused is False


@pytest.mark.asyncio
async def test_wait_while_paused_blocks_until_resume():
    ctrl = SessionControl()
    ctrl.pause()
    resumed = False

    async def _waiter() -> None:
        nonlocal resumed
        await ctrl.wait_while_paused()
        resumed = True

    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0.05)
    assert resumed is False
    ctrl.resume()
    await asyncio.wait_for(task, timeout=1.0)
    assert resumed is True


@pytest.mark.asyncio
async def test_wait_while_paused_immediate_when_running():
    ctrl = SessionControl()
    await asyncio.wait_for(ctrl.wait_while_paused(), timeout=0.5)


def test_request_stop_unblocks_pause():
    ctrl = SessionControl()
    ctrl.pause()
    assert ctrl.paused is True
    ctrl.request_stop()
    assert ctrl.stop_requested is True
    assert ctrl.paused is False


def test_set_audio_level_clamped():
    ctrl = SessionControl()
    ctrl.set_audio_level(1.5, speech=True)
    assert ctrl.get_audio_level() == 1.0
    assert ctrl.get_active_speech() is True
    ctrl.set_audio_level(-0.2, speech=False)
    assert ctrl.get_audio_level() == 0.0
    assert ctrl.get_active_speech() is False


def test_record_final_and_performance_summary():
    ctrl = SessionControl()
    ctrl.record_final(0.5, had_ja=True, had_en=True, is_late=False)
    ctrl.record_final(2.5, had_ja=True, had_en=True, is_late=True)
    ctrl.record_final(0.1, had_ja=False, had_en=False)

    summary = ctrl.performance_summary()
    assert summary["asr_count"] == 2
    assert summary["translation_count"] == 2
    assert summary["empty_asr_count"] == 1
    assert summary["late_translations"] == 1
    assert summary["display_latency_s"]["p50"] is not None
    assert summary["display_latency_s"]["p90"] is not None
    assert summary["asr_backend"] == "mlx"


def test_health_payload_paused():
    ctrl = SessionControl()
    ctrl.audio_input_active = True
    ctrl.asr_busy = True
    ctrl.pause()
    health = ctrl.health_payload(state="paused")
    assert health["session_label"] == "Paused"
    assert health["audio_label"] == "Paused"
    assert health["asr_worker_active"] is False
    assert health["audio_input_active"] is False


def test_health_payload_listening():
    ctrl = SessionControl()
    ctrl.audio_input_active = True
    ctrl.asr_busy = True
    ctrl.set_audio_level(0.4, speech=True)
    health = ctrl.health_payload(state="listening")
    assert health["session_label"] == "Listening"
    assert health["audio_input_active"] is True
    assert health["asr_worker_active"] is True
    assert health["active_speech"] is True
    assert health["audio_level"] == 0.4
