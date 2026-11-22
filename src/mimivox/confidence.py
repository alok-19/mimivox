"""mimivox.confidence — how far to trust one caption.

Whisper segments and CAT tokens carry their own probabilities, and the
pipeline already knows when it had to repair a line (prompt leak cleaned,
retry without context, untranslated Japanese stripped). ``assess`` folds those
signals into one ``Confidence`` that rides on ``final`` and ``revision``
events, so a reader can tell when to ask the speaker again.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from mimivox import config

Level = Literal["high", "medium", "low"]
_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


@dataclasses.dataclass(frozen=True)
class AsrSignals:
    """Whisper evidence for one utterance (worst case across its segments)."""

    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None
    duration_s: float = 0.0
    repeats_collapsed: bool = False  # _deduplicate_phrases rewrote the text


@dataclasses.dataclass(frozen=True)
class MtSignals:
    """CAT evidence for one translation. ``mean_logprob`` is None when unknown
    (cache seed, English passthrough)."""

    mean_logprob: float | None = None
    prompt_leak_cleaned: bool = False
    japanese_stripped: bool = False
    numbers_mismatch: bool = False  # an EN number has no JA counterpart


@dataclasses.dataclass(frozen=True)
class Confidence:
    level: Level
    reasons: tuple[str, ...] = ()
    asr: float | None = None  # geometric-mean token probability, 0..1
    mt: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "reasons": list(self.reasons),
            "asr": self.asr,
            "mt": self.mt,
        }


def asr_signals_from_segments(
    segments: Sequence[dict[str, Any]] | None,
) -> AsrSignals | None:
    """Aggregate mlx_whisper ``result["segments"]``; None when it has none."""
    weighted = total = 0.0
    no_speech: list[float] = []
    compression: list[float] = []
    for seg in segments or ():
        dur = max(float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)), 0.0)
        if seg.get("avg_logprob") is not None:
            weighted += float(seg["avg_logprob"]) * (dur or 1e-3)
            total += dur or 1e-3
        if seg.get("no_speech_prob") is not None:
            no_speech.append(float(seg["no_speech_prob"]))
        if seg.get("compression_ratio") is not None:
            compression.append(float(seg["compression_ratio"]))
    if not total:
        return None
    return AsrSignals(
        avg_logprob=weighted / total,
        no_speech_prob=max(no_speech) if no_speech else None,
        compression_ratio=max(compression) if compression else None,
        duration_s=total,
    )


def merge_asr(a: AsrSignals | None, b: AsrSignals | None) -> AsrSignals | None:
    """Signals for two utterances read as one (a revision): weighted logprob,
    worst of the rest."""
    if a is None or b is None:
        return a or b

    def worst(x: float | None, y: float | None) -> float | None:
        return y if x is None else x if y is None else max(x, y)

    total = a.duration_s + b.duration_s
    if a.avg_logprob is None or b.avg_logprob is None or not total:
        logprob = a.avg_logprob if b.avg_logprob is None else b.avg_logprob
    else:
        logprob = (a.avg_logprob * a.duration_s + b.avg_logprob * b.duration_s) / total
    return AsrSignals(
        avg_logprob=logprob,
        no_speech_prob=worst(a.no_speech_prob, b.no_speech_prob),
        compression_ratio=worst(a.compression_ratio, b.compression_ratio),
        duration_s=total,
        repeats_collapsed=a.repeats_collapsed or b.repeats_collapsed,
    )


def mt_signals_from_logprobs(logprobs: Iterable[float], **flags: bool) -> MtSignals:
    lps = list(logprobs)
    return MtSignals(mean_logprob=sum(lps) / len(lps) if lps else None, **flags)


def _probability(logprob: float | None) -> float | None:
    return None if logprob is None else round(math.exp(min(logprob, 0.0)), 3)


def assess(asr: AsrSignals | None, mt: MtSignals | None, en: str) -> Confidence:
    """Pure: fold ASR and MT signals into a Confidence. Level is the worst
    signal; missing signals never lower it."""
    found: list[tuple[str, str]] = []  # (level, reason)

    def below(value: float | None, limits: tuple[float, float], reason: str) -> None:
        if value is None:
            return
        if value < limits[1]:
            found.append(("low", reason))
        elif value < limits[0]:
            found.append(("medium", reason))

    def above(value: float | None, limits: tuple[float, float], reason: str) -> None:
        if value is None:
            return
        if value > limits[1]:
            found.append(("low", reason))
        elif value > limits[0]:
            found.append(("medium", reason))

    if asr is not None:
        below(asr.avg_logprob, config.CONF_ASR_LOGPROB, "Speech recognition was unsure")
        above(asr.no_speech_prob, config.CONF_NO_SPEECH, "May be noise rather than speech")
        above(asr.compression_ratio, config.CONF_COMPRESSION, "Transcript looks repetitive")
        if asr.repeats_collapsed:
            found.append(("medium", "Repeated phrases were collapsed"))
    if mt is not None:
        below(mt.mean_logprob, config.CONF_MT_LOGPROB, "Translation was unsure")
        if mt.prompt_leak_cleaned:
            found.append(("medium", "Translation needed cleanup"))
        if mt.japanese_stripped:
            found.append(("low", "Untranslated Japanese was removed"))
        if mt.numbers_mismatch:
            found.append(("low", "Numbers may not match the Japanese"))
    if not en.strip():
        found.append(("low", "No translation produced"))

    level: Level = "high"
    for lvl, _ in found:
        if _RANK[lvl] > _RANK[level]:
            level = lvl  # type: ignore[assignment]
    reasons = tuple(dict.fromkeys(r for _, r in sorted(found, key=lambda f: -_RANK[f[0]])))
    return Confidence(
        level=level,
        reasons=reasons,
        asr=_probability(asr.avg_logprob) if asr else None,
        mt=_probability(mt.mean_logprob) if mt else None,
    )
