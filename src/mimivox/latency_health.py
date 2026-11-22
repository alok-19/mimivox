"""mimivox.latency_health — Is the caption pipeline keeping up with the speaker?

One immutable ``HealthSnapshot`` answers it: per-stage queue depth and latency
for VAD -> ASR -> CAT, the backlog (how many seconds ago the oldest speech not
yet on screen ended, on the audio clock), a
``HealthState`` (ok / behind / overloaded) and the reasons behind a non-ok
state. ``LatencyMonitor`` turns worker callbacks plus a peek at the queues
into that snapshot. Pure Python, no FastAPI, no models.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from mimivox.config import (
    HEALTH_BEHIND_CLEAR_S,
    HEALTH_BEHIND_S,
    HEALTH_OVERLOADED_CLEAR_S,
    HEALTH_OVERLOADED_S,
)

_EWMA_ALPHA = 0.4
_MIN_RTF_SAMPLES = 2


class HealthState(str, enum.Enum):
    OK = "ok"
    BEHIND = "behind"
    OVERLOADED = "overloaded"


class CatchUpMode(str, enum.Enum):
    """What a catch-up does with queued work.

    FAST   keep every queued utterance, but skip its translation (JA stays on
           screen, EN is marked skipped and can be recovered later).
    SKIP   as FAST, and drop queued audio that ASR cannot decode in time: each
           span becomes a visible gap whose audio is archived for recovery.
    """

    FAST = "fast"
    SKIP = "skip"


@dataclasses.dataclass(frozen=True)
class StageHealth:
    stage: str
    queue_depth: int
    queued_audio_s: float
    latency_s: float | None
    realtime_factor: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "queue_depth": self.queue_depth,
            "queued_audio_s": round(self.queued_audio_s, 3),
            "latency_s": None if self.latency_s is None else round(self.latency_s, 3),
            "realtime_factor": (
                None if self.realtime_factor is None else round(self.realtime_factor, 3)
            ),
        }


@dataclasses.dataclass(frozen=True)
class HealthReason:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclasses.dataclass(frozen=True)
class HealthSnapshot:
    state: HealthState
    backlog_s: float
    stages: tuple[StageHealth, ...]
    reasons: tuple[HealthReason, ...]
    catching_up: bool
    recommended_mode: CatchUpMode
    skipped_translations: int = 0
    gaps: int = 0
    gap_audio_s: float = 0.0
    skippable: int = 0

    @property
    def catch_up_available(self) -> bool:
        return self.state is not HealthState.OK and not self.catching_up and self.skippable > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "backlog_s": round(self.backlog_s, 3),
            "stages": [s.as_dict() for s in self.stages],
            "reasons": [r.as_dict() for r in self.reasons],
            "catching_up": self.catching_up,
            "catch_up_available": self.catch_up_available,
            "recommended_mode": self.recommended_mode.value,
            "skipped_translations": self.skipped_translations,
            "gaps": self.gaps,
            "gap_audio_s": round(self.gap_audio_s, 3),
            "skippable": self.skippable,
        }


def next_state(prev: HealthState, backlog_s: float) -> HealthState:
    """Backlog -> state with hysteresis, so a number hovering at a threshold
    does not flicker the indicator."""
    if prev is HealthState.OVERLOADED:
        if backlog_s >= HEALTH_OVERLOADED_CLEAR_S:
            return HealthState.OVERLOADED
        prev = HealthState.BEHIND
    if backlog_s >= HEALTH_OVERLOADED_S:
        return HealthState.OVERLOADED
    if prev is HealthState.BEHIND:
        return HealthState.BEHIND if backlog_s >= HEALTH_BEHIND_CLEAR_S else HealthState.OK
    return HealthState.BEHIND if backlog_s >= HEALTH_BEHIND_S else HealthState.OK


class _Ewma:
    def __init__(self) -> None:
        self.value: float | None = None
        self.count = 0

    def add(self, x: float) -> None:
        self.value = x if self.value is None else self.value + _EWMA_ALPHA * (x - self.value)
        self.count += 1


class LatencyMonitor:
    """Worker callbacks in, ``HealthSnapshot`` out. Thread-safe.

    ``gpu_probe`` says whether another model (the notes LLM) holds the shared
    GPU lock; ``thermal_probe`` returns the CPU speed limit in percent
    (100 = unthrottled) or None when unknown. Both are optional.
    """

    def __init__(
        self,
        *,
        gpu_probe: Callable[[], bool] | None = None,
        thermal_probe: Callable[[], float | None] | None = None,
    ) -> None:
        self.gpu_probe = gpu_probe
        self.thermal_probe = thermal_probe
        self._lock = threading.Lock()
        self._state = HealthState.OK
        self._vad = _Ewma()
        self._asr = _Ewma()
        self._asr_rtf = _Ewma()
        self._cat = _Ewma()
        self._cat_rtf = _Ewma()
        self._asr_inflight: tuple[float, float] | None = None
        self._cat_inflight: tuple[float, float] | None = None

    def vad_wait(self, seconds: float) -> None:
        with self._lock:
            self._vad.add(max(0.0, float(seconds)))

    def asr_begin(self, audio_s: float, end_s: float) -> None:
        with self._lock:
            self._asr_inflight = (max(0.0, float(audio_s)), float(end_s))

    def asr_end(self, audio_s: float, latency_s: float) -> None:
        with self._lock:
            self._asr_inflight = None
            self._asr.add(float(latency_s))
            if audio_s > 0:
                self._asr_rtf.add(float(latency_s) / float(audio_s))

    def cat_begin(self, audio_s: float, end_s: float) -> None:
        with self._lock:
            self._cat_inflight = (max(0.0, float(audio_s)), float(end_s))

    def cat_end(self, audio_s: float, latency_s: float) -> None:
        with self._lock:
            self._cat_inflight = None
            self._cat.add(float(latency_s))
            if audio_s > 0:
                self._cat_rtf.add(float(latency_s) / float(audio_s))

    @property
    def state(self) -> HealthState:
        return self._state

    def snapshot(
        self,
        asr_queue: Iterable[tuple[float, float]] = (),
        cat_queue: Iterable[tuple[float, float]] = (),
        *,
        now_s: float = 0.0,
        producer_lag_s: float = 0.0,
        catching_up: bool = False,
        skipped_translations: int = 0,
        gaps: int = 0,
        gap_audio_s: float = 0.0,
        skippable: int = 0,
    ) -> HealthSnapshot:
        """``asr_queue`` / ``cat_queue`` hold one ``(audio seconds, audio-clock
        end)`` pair per waiting item; ``now_s`` is the audio clock right now.
        The backlog is how long ago the oldest unfinished speech ended: a long
        utterance that is simply being processed does not count as behind."""
        asr_items = [(float(a), float(e)) for a, e in asr_queue]
        cat_items = [(float(a), float(e)) for a, e in cat_queue]
        with self._lock:
            asr_all = asr_items + ([self._asr_inflight] if self._asr_inflight else [])
            cat_all = cat_items + ([self._cat_inflight] if self._cat_inflight else [])
            asr_audio = sum(a for a, _ in asr_all)
            cat_audio = sum(a for a, _ in cat_all)
            ends = [e for _, e in asr_all + cat_all]
            backlog = max(0.0, float(now_s) - min(ends)) if ends else 0.0
            self._state = next_state(self._state, backlog)
            asr_rtf = self._asr_rtf.value if self._asr_rtf.count >= _MIN_RTF_SAMPLES else None
            stages = (
                StageHealth("vad", 0, max(0.0, float(producer_lag_s)), self._vad.value),
                StageHealth("asr", len(asr_items), asr_audio, self._asr.value, asr_rtf),
                StageHealth("cat", len(cat_items), cat_audio, self._cat.value, self._cat_rtf.value),
            )
            state = self._state
        reasons = self._reasons(state, backlog, stages) if state is not HealthState.OK else ()
        asr_bound = asr_rtf is not None and asr_rtf > 1.0
        mode = CatchUpMode.SKIP if asr_bound else CatchUpMode.FAST
        return HealthSnapshot(
            state=state,
            backlog_s=backlog,
            stages=stages,
            reasons=tuple(reasons),
            catching_up=catching_up,
            recommended_mode=mode,
            skipped_translations=skipped_translations,
            gaps=gaps,
            gap_audio_s=gap_audio_s,
            skippable=skippable,
        )

    def _reasons(
        self, state: HealthState, backlog: float, stages: tuple[StageHealth, ...]
    ) -> list[HealthReason]:
        _, asr, cat = stages
        out: list[HealthReason] = []
        if self.gpu_probe is not None and self._probe(self.gpu_probe):
            out.append(
                HealthReason("notes_gpu", "Notes or Ask is running; captions may lag briefly")
            )
        if self.thermal_probe is not None:
            limit = self._probe(self.thermal_probe)
            if isinstance(limit, (int, float)) and limit < 100:
                out.append(
                    HealthReason("thermal", f"CPU thermal limit: running at {limit:.0f}% speed")
                )
        if asr.realtime_factor is not None and asr.realtime_factor > 1.0:
            out.append(
                HealthReason(
                    "asr_slow",
                    f"Speech recognition is slower than real time ({asr.realtime_factor:.1f}x)",
                )
            )
        elif asr.queued_audio_s > cat.queued_audio_s and asr.queued_audio_s > 0:
            out.append(HealthReason("asr_slow", "Speech recognition is falling behind"))
        if cat.queued_audio_s >= asr.queued_audio_s and cat.queued_audio_s > 0:
            avg = f" (about {cat.latency_s:.1f}s per line)" if cat.latency_s else ""
            out.append(HealthReason("translate_slow", f"Translation is the bottleneck{avg}"))
        if not out:
            out.append(HealthReason("backlog", f"Captions are {backlog:.0f}s behind the speaker"))
        return out

    @staticmethod
    def _probe(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception:
            return None


_THERMAL_TTL_S = 15.0
_thermal_lock = threading.Lock()
_thermal: dict[str, Any] = {"at": 0.0, "limit": None, "refreshing": False}


def parse_pmset_therm(output: str) -> float | None:
    """``CPU_Speed_Limit = 80`` from ``pmset -g therm``; None if absent."""
    for line in output.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "CPU_Speed_Limit":
            try:
                return float(value.strip())
            except ValueError:
                return None
    return None


def _refresh_thermal() -> None:
    limit: float | None = None
    try:
        proc = subprocess.run(
            ["pmset", "-g", "therm"], capture_output=True, text=True, timeout=3, check=False
        )
        limit = parse_pmset_therm(proc.stdout)
    except (OSError, subprocess.SubprocessError):
        limit = None
    with _thermal_lock:
        _thermal.update(at=time.monotonic(), limit=limit, refreshing=False)


def thermal_speed_limit() -> float | None:
    """Last known CPU speed limit in percent; never blocks on the subprocess."""
    with _thermal_lock:
        stale = time.monotonic() - _thermal["at"] > _THERMAL_TTL_S
        start = stale and not _thermal["refreshing"]
        if start:
            _thermal["refreshing"] = True
        limit = _thermal["limit"]
    if start:
        threading.Thread(target=_refresh_thermal, daemon=True).start()
    return limit


class DebugDelay:
    """Sleep inside one stage so a replay session can be driven behind.

    ``cat:8:3`` delays the first three translations by 8 s each; omit the
    count to delay every one. ``asr`` delays decoding the same way. Unset or
    malformed means no delay.
    """

    ENV = "MIMIVOX_DEBUG_DELAY"

    def __init__(self, stage: str = "", seconds: float = 0.0, count: int | None = None) -> None:
        self.stage = stage
        self.seconds = max(0.0, seconds)
        self.remaining = count

    @classmethod
    def from_env(cls, env: Any = None) -> DebugDelay:
        raw = (env if env is not None else os.environ).get(cls.ENV, "")
        parts = raw.split(":")
        try:
            stage, seconds = parts[0].strip(), float(parts[1])
            count = int(parts[2]) if len(parts) > 2 else None
        except (IndexError, ValueError):
            return cls()
        return cls(stage, seconds, count) if stage in ("asr", "cat") else cls()

    async def apply(self, stage: str) -> None:
        if stage != self.stage or self.seconds <= 0 or self.remaining == 0:
            return
        if self.remaining is not None:
            self.remaining -= 1
        await asyncio.sleep(self.seconds)
