"""mimivox.gap_recovery — Win back what a catch-up skipped, from the archive.

An archived session can hold two kinds of catch-up leftovers:

* a ``CaptionGapEvent``: speech dropped unheard. Its audio sits in
  ``data/sessions/<id>.gaps/<gap id>.wav`` (when the session was saved), so it
  is transcribed and translated and the gap event is replaced, in place, by an
  ``AsrCompletedEvent`` + ``TranslationCompletedEvent`` pair under the same index.
* an ``AsrCompletedEvent`` flagged ``translation_skipped``: the JA was shown but
  not translated. It gains the missing ``TranslationCompletedEvent``.

The model calls are injected (``transcribe``, ``translate``) so the event surgery
is testable without models; ``default_transcribe`` / ``default_translate`` run
them under the shared MLX lock.
"""

from __future__ import annotations

import asyncio
import dataclasses
import wave
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np

from mimivox import session_archive
from mimivox.config import SAMPLE_RATE, shared_mlx_lock

GAP_EVENT = "CaptionGapEvent"

Transcribe = Callable[[np.ndarray], Awaitable[str]]
Translate = Callable[[str], Awaitable[str]]


@dataclasses.dataclass(frozen=True)
class RecoveryReport:
    recovered_gaps: int
    retranslated: int
    remaining: int

    def as_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


def pending(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        ev
        for ev in events
        if ev.get("type") == GAP_EVENT
        or (ev.get("type") == "AsrCompletedEvent" and ev.get("translation_skipped"))
    ]


def _load_wav(path: Any) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != SAMPLE_RATE or wf.getsampwidth() != 2 or wf.getnchannels() != 1:
            raise ValueError(f"{path}: expected 16 kHz mono 16-bit")
        data = wf.readframes(wf.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


async def recover_session(
    session_id: str,
    transcribe: Transcribe,
    translate: Translate,
    indexes: set[int] | None = None,
) -> RecoveryReport | None:
    """Recover skipped captions of one archived session. None if it is unknown.

    ``indexes`` limits recovery to those event indexes (default: everything).
    """
    data = session_archive.load_session(session_id)
    if data is None:
        return None
    events: list[dict[str, Any]] = list(data.get("events") or [])
    out: list[dict[str, Any]] = []
    recovered = retranslated = remaining = 0
    for ev in events:
        idx = ev.get("index")
        wanted = indexes is None or idx in indexes
        if ev.get("type") == GAP_EVENT and wanted:
            replacement = await _recover_gap(session_id, ev, transcribe, translate)
            if replacement is None:
                remaining += 1
                out.append(ev)
            else:
                recovered += 1
                out.extend(replacement)
        elif ev.get("type") == "AsrCompletedEvent" and ev.get("translation_skipped") and wanted:
            en = await _safe(translate, ev.get("text") or "")
            if not en:
                remaining += 1
                out.append(ev)
                continue
            retranslated += 1
            out.append({k: v for k, v in ev.items() if k != "translation_skipped"})
            out.append(_translation_event(ev, en))
        else:
            if ev.get("type") == GAP_EVENT or ev.get("translation_skipped"):
                remaining += 1
            out.append(ev)
    if recovered or retranslated:
        session_archive.replace_events(session_id, out)
    return RecoveryReport(recovered, retranslated, remaining)


async def _safe(fn: Callable[[Any], Awaitable[str]], arg: Any) -> str | None:
    try:
        return (await fn(arg) or "").strip()
    except Exception:
        return None


async def _recover_gap(
    session_id: str, gap: dict[str, Any], transcribe: Transcribe, translate: Translate
) -> list[dict[str, Any]] | None:
    path = session_archive.gap_audio_file(session_id, gap.get("index") or 0)
    if not gap.get("recoverable") or not path.exists():
        return None
    try:
        audio = _load_wav(path)
    except (OSError, ValueError, EOFError):
        return None
    ja = await _safe(transcribe, audio)
    if ja is None:
        return None
    start = float(gap.get("start") or 0.0)
    end = float(gap.get("end") or start)
    if not ja:
        return []
    asr = {
        "type": "AsrCompletedEvent",
        "index": gap.get("index"),
        "start": start,
        "end": end,
        "text": ja,
        "timestamp_s": start,
        "recovered": True,
    }
    en = await _safe(translate, ja)
    if not en:
        return [{**asr, "translation_skipped": "catch_up"}]
    return [asr, _translation_event(asr, en)]


def _translation_event(asr: dict[str, Any], en: str) -> dict[str, Any]:
    start = float(asr.get("start") or 0.0)
    ev: dict[str, Any] = {
        "type": "TranslationCompletedEvent",
        "index": asr.get("index"),
        "start": start,
        "end": float(asr.get("end") or start),
        "text": en,
        "source_indexes": [asr.get("index")],
        "timestamp_s": start,
        "recovered": True,
    }
    if isinstance(asr.get("speaker"), dict):
        ev["speaker"] = asr["speaker"]
    return ev


async def default_transcribe(audio: np.ndarray) -> str:
    from mimivox.asr import _deduplicate_phrases, _do_asr_streaming, is_non_content_asr

    loop = asyncio.get_running_loop()
    async with shared_mlx_lock():
        res = await loop.run_in_executor(None, _do_asr_streaming, audio, lambda _s: None)
    text = _deduplicate_phrases(res[0] or "")
    return "" if is_non_content_asr(text) else text


async def default_translate(ja: str) -> str:
    from mimivox.translate import apply_glossary, get_translator, normalize_punctuation

    res = await get_translator().translate(
        apply_glossary(normalize_punctuation(ja)), lambda _t: None, priority="final", context=()
    )
    return res[0]
