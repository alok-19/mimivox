"""mimivox.session_control — Shared pause/health/metrics for a live session.

No FastAPI dependency. Safe to import from pipeline workers and the API layer.
"""

from __future__ import annotations

import asyncio
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mimivox.config import HEALTH_BEHIND_S, SAMPLE_RATE
from mimivox.latency_health import HealthSnapshot, HealthState, LatencyMonitor


def percentile(values: list[float], p: float) -> float | None:
    """Return the p-th percentile (0..100) of values, or None if empty."""
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    if p <= 0:
        return float(ordered[0])
    if p >= 100:
        return float(ordered[-1])
    k = (len(ordered) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    frac = k - lo
    return float(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)


def _queued(q: Any) -> list[tuple[float, float]]:
    """``(audio seconds, audio-clock end)`` of each utterance waiting on a queue.

    Reads asyncio.Queue's backing deque (there is no public peek); call it on
    the event loop so the deque is not mutated mid-read.
    """
    if q is None:
        return []
    out: list[tuple[float, float]] = []
    for item in list(getattr(q, "_queue", ())):
        if isinstance(item, tuple) and len(item) >= 3:
            out.append((len(item[0]) / SAMPLE_RATE, float(item[2])))
        elif getattr(item, "kind", None) == "final":
            dur = float(item.duration_s or 0.0)
            out.append((dur, float(item.timeline_s) + dur))
    return out


@dataclass
class SessionControl:
    """Per-session control flags and live health counters.

    Pause semantics: ``_running`` is an asyncio.Event.
    - set  → not paused (workers proceed)
    - clear → paused (workers await until resume)
    """

    stop_requested: bool = False
    audio_input_active: bool = False
    asr_busy: bool = False
    translate_busy: bool = False
    active_speech: bool = False
    queue_depth: int = 0
    session_start_perf: float = field(default_factory=time.perf_counter)
    last_audio_level: float = 0.0  # 0..1
    latencies_s: list[float] = field(default_factory=list)
    asr_count: int = 0
    translation_count: int = 0
    empty_asr_count: int = 0
    late_translations: int = 0
    stale_drops: int = 0
    asr_backend: str = "mlx"

    monitor: LatencyMonitor = field(default_factory=LatencyMonitor)
    asr_q: Any = None
    display_q: Any = None
    broadcast: Callable[[dict], None] | None = None
    gap_sink: Callable[[int, Any], bool] | None = None
    producer_lag_s: float = 0.0
    catch_up_active: bool = False
    skip_ahead_requested: bool = False
    skipped_translations: int = 0
    gap_count: int = 0
    gap_audio_s: float = 0.0
    _last_row_id: int = 0
    clock_origin_perf: float | None = None
    _paused_at: float | None = None
    _paused_total_s: float = 0.0

    _running: asyncio.Event = field(default_factory=asyncio.Event)
    _level_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Default: not paused.
        self._running.set()

    # -- pause / resume -----------------------------------------------------

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = time.perf_counter()
        self._running.clear()

    def _end_pause(self) -> None:
        if self._paused_at is not None:
            self._paused_total_s += time.perf_counter() - self._paused_at
            self._paused_at = None

    def resume(self) -> None:
        self._end_pause()
        self._running.set()

    async def wait_while_paused(self) -> None:
        """Block until resume (or return immediately if not paused)."""
        await self._running.wait()

    def request_stop(self) -> None:
        self.stop_requested = True
        self._end_pause()
        # Unblock any waiters so workers can observe stop_requested.
        self._running.set()

    def alloc_row_id(self) -> int:
        """Next id in the session's shared row space (captions and gaps)."""
        self._last_row_id += 1
        return self._last_row_id

    def realtime_s(self) -> float:
        """The audio clock: seconds of real time since the feed began, minus pauses."""
        origin = (
            self.clock_origin_perf
            if self.clock_origin_perf is not None
            else self.session_start_perf
        )
        now = self._paused_at if self._paused_at is not None else time.perf_counter()
        return max(0.0, now - origin - self._paused_total_s)

    def skip_plan(self) -> tuple[int, int]:
        """What a catch-up would skip now: (finals awaiting translation, audio items).

        Queue order is age order: a final in display_q is older than the item
        inside ASR and than anything in asr_q, so the newest final is kept only
        when nothing newer exists.
        """
        finals = len(_queued(self.display_q))
        audio = len(_queued(self.asr_q))
        return (finals if self.has_work_newer_than_finals() else max(0, finals - 1)), max(
            0, audio - 1
        )

    def has_work_newer_than_finals(self) -> bool:
        return self.asr_busy or bool(_queued(self.asr_q))

    def latency_snapshot(self) -> HealthSnapshot:
        """Current health. Also ends catch-up mode once the backlog has cleared."""
        finals, audio = self.skip_plan()
        feed_jump = 1 if self.producer_lag_s >= HEALTH_BEHIND_S else 0
        kwargs: dict[str, Any] = dict(
            now_s=self.realtime_s(),
            producer_lag_s=self.producer_lag_s,
            skipped_translations=self.skipped_translations,
            gaps=self.gap_count,
            gap_audio_s=self.gap_audio_s,
            skippable=finals + audio + feed_jump,
        )
        asr_q, cat_q = _queued(self.asr_q), _queued(self.display_q)
        snap = self.monitor.snapshot(asr_q, cat_q, catching_up=self.catch_up_active, **kwargs)
        if self.catch_up_active and snap.state is HealthState.OK:
            self.catch_up_active = False
            snap = self.monitor.snapshot(asr_q, cat_q, **kwargs)
        return snap

    # -- thread-safe audio meter --------------------------------------------

    def set_audio_level(self, level: float, *, speech: bool | None = None) -> None:
        level = max(0.0, min(1.0, float(level)))
        with self._level_lock:
            self.last_audio_level = level
            if speech is not None:
                self.active_speech = bool(speech)

    def get_audio_level(self) -> float:
        with self._level_lock:
            return self.last_audio_level

    def get_active_speech(self) -> bool:
        with self._level_lock:
            return self.active_speech

    # -- metrics ------------------------------------------------------------

    def record_final(
        self,
        total_latency_s: float,
        *,
        had_ja: bool,
        had_en: bool,
        is_late: bool = False,
    ) -> None:
        if had_ja:
            self.asr_count += 1
            self.latencies_s.append(float(total_latency_s))
        else:
            self.empty_asr_count += 1
        if had_en:
            self.translation_count += 1
        if is_late:
            self.late_translations += 1

    def performance_summary(self) -> dict[str, Any]:
        p50 = percentile(self.latencies_s, 50)
        p90 = percentile(self.latencies_s, 90)
        display = {
            "p50": round(p50, 3) if p50 is not None else None,
            "p90": round(p90, 3) if p90 is not None else None,
        }
        return {
            "asr_count": self.asr_count,
            "translation_count": self.translation_count,
            "empty_asr_count": self.empty_asr_count,
            "stale_drops": self.stale_drops,
            "late_translations": self.late_translations,
            "display_latency_s": display,
            "asr_backend": self.asr_backend,
        }

    def health_payload(
        self,
        *,
        state: str,
        model_ready: bool = True,
    ) -> dict[str, Any]:
        active = state in ("listening", "processing", "paused", "stopping")
        paused = self.paused and active
        level = self.get_audio_level()
        speech = self.get_active_speech()

        # Queue lag proxy: ~0.5s per pending item (coarse but live).
        latency = self.latency_snapshot()
        queue_lag = max(0.0, float(self.queue_depth) * 0.5, latency.backlog_s)
        # Realtime lag: recent median display latency, else 0.
        recent = self.latencies_s[-5:] if self.latencies_s else []
        realtime_lag = float(statistics.median(recent)) if recent else 0.0

        if paused:
            session_label = "Paused"
            audio_label = "Paused"
            asr_label = "Paused"
            translation_label = "Paused"
        elif not active:
            session_label = "Ready"
            audio_label = "Idle"
            asr_label = "Idle"
            translation_label = "Idle"
        else:
            session_label = "Listening" if state != "processing" else "Processing"
            audio_label = "Listening" if self.audio_input_active else "Idle"
            asr_label = "Live" if self.asr_busy else "Idle"
            translation_label = "Live" if self.translate_busy else "Idle"

        if state == "completed":
            session_label = "Stopped"
        elif state == "failed":
            session_label = "Failed"

        return {
            "audio_input_active": bool(self.audio_input_active and active and not paused),
            "asr_worker_active": bool(self.asr_busy and not paused),
            "translation_worker_active": bool(self.translate_busy and not paused),
            "queue_lag_s": round(queue_lag, 3),
            "realtime_lag_s": round(realtime_lag, 3),
            "dropped_stale_chunks": self.stale_drops,
            "dropped_silence_chunks": 0,
            "active_speech": bool(speech and active and not paused),
            "model_ready": model_ready,
            "degraded": state == "failed",
            "session_label": session_label,
            "audio_label": audio_label,
            "asr_label": asr_label,
            "translation_label": translation_label,
            "catching_up": queue_lag > 1.5,
            "latency": latency.as_dict(),
            "stopped": state in ("idle", "completed", "failed") or not active,
            "audio_level": round(level, 3),
        }
