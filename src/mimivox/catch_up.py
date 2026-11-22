"""mimivox.catch_up — Skip ahead when captions fall behind, never silently.

``apply_catch_up`` runs on the event loop (no awaits, so no worker can
interleave) and acts on whatever is queued right now:

* FAST: queued utterances keep their JA but skip translation, except the
  newest, which is translated normally so the viewer lands on the present.
* SKIP: additionally, queued audio older than the newest utterance is dropped
  and each span becomes a gap event. The gap's audio is handed to
  ``control.gap_sink`` (the archive) so Review can recover it later.

Catch-up mode stays on (``control.catch_up_active``) until health returns to
ok; while it is on, the display worker skips translation for any caption that
still has backlog behind it.
"""

from __future__ import annotations

import asyncio
import dataclasses

import numpy as np

from mimivox.config import SAMPLE_RATE
from mimivox.latency_health import CatchUpMode, HealthState
from mimivox.session_control import SessionControl

SKIP_REASON = "catch_up"


@dataclasses.dataclass(frozen=True)
class CatchUpResult:
    mode: CatchUpMode
    translations_skipped: int
    gaps: int
    gap_audio_s: float

    def as_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "translations_skipped": self.translations_skipped,
            "gaps": self.gaps,
            "gap_audio_s": round(self.gap_audio_s, 3),
        }


def record_gap(control: SessionControl, audio: np.ndarray, start_s: float, end_s: float) -> int:
    gap_id = control.alloc_row_id()
    recoverable = control.gap_sink is not None and control.gap_sink(gap_id, audio)
    duration_s = len(audio) / SAMPLE_RATE
    control.gap_count += 1
    control.gap_audio_s += duration_s
    if control.broadcast is not None:
        control.broadcast(
            {
                "kind": "gap",
                "id": gap_id,
                "ts": round(start_s, 3),
                "end": round(end_s, 3),
                "duration_s": round(duration_s, 3),
                "reason": SKIP_REASON,
                "recoverable": recoverable,
            }
        )
    return gap_id


def _drain(q: asyncio.Queue) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return items
        q.task_done()


def _skip_display_translations(q: asyncio.Queue, keep_newest: bool) -> int:
    """Flag queued finals to skip translation; the newest is kept unless newer
    work (in ASR or asr_q) will anchor the viewer on the present instead."""
    items = _drain(q)
    last_final = max(
        (i for i, it in enumerate(items) if getattr(it, "kind", None) == "final"),
        default=-1,
    )
    skip_through = last_final if keep_newest else last_final + 1
    skipped = 0
    for i, it in enumerate(items):
        if getattr(it, "kind", None) == "partial" and i < last_final:
            continue
        if getattr(it, "kind", None) == "final" and i < skip_through and not it.skip_translation:
            it = dataclasses.replace(it, skip_translation=True)
            skipped += 1
        q.put_nowait(it)
    return skipped


def _audio_items(items: list) -> list[int]:
    return [i for i, it in enumerate(items) if isinstance(it, tuple)]


def _skip_audio_translations(q: asyncio.Queue) -> int:
    items = _drain(q)
    idx = _audio_items(items)
    skipped = 0
    for i, it in enumerate(items):
        if i in idx[:-1] and not (len(it) >= 5 and it[4]):
            it = (*it[:4], True)
            skipped += 1
        q.put_nowait(it)
    return skipped


def _drop_audio(control: SessionControl, q: asyncio.Queue) -> tuple[int, float]:
    items = _drain(q)
    idx = _audio_items(items)
    dropped = set(idx[:-1])
    gaps, gap_s = 0, 0.0
    for i, it in enumerate(items):
        if i in dropped:
            audio = np.asarray(it[0], dtype=np.float32)
            end_s = float(it[2])
            dur = len(audio) / SAMPLE_RATE
            record_gap(control, audio, max(0.0, end_s - dur), end_s)
            gaps += 1
            gap_s += dur
        else:
            q.put_nowait(it)
    return gaps, gap_s


def apply_catch_up(
    control: SessionControl, mode: CatchUpMode | None = None
) -> CatchUpResult | None:
    snap = control.latency_snapshot()
    if snap.state is HealthState.OK:
        return None
    mode = mode or snap.recommended_mode
    control.catch_up_active = True
    skipped = 0
    gaps, gap_s = 0, 0.0
    if control.display_q is not None:
        skipped += _skip_display_translations(
            control.display_q, keep_newest=not control.has_work_newer_than_finals()
        )
    if control.asr_q is not None:
        if mode is CatchUpMode.SKIP:
            gaps, gap_s = _drop_audio(control, control.asr_q)
        else:
            skipped += _skip_audio_translations(control.asr_q)
    if mode is CatchUpMode.SKIP:
        control.skip_ahead_requested = True
    control.skipped_translations += skipped
    return CatchUpResult(mode, skipped, gaps, gap_s)
