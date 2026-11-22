"""mimivox.pair_translate — route a LanguagePair to its translator.

``ja-en`` keeps the CAT-Translate singleton from ``translate`` untouched. Every
other pair uses ``LlmTranslator``: the on-device Qwen instruct model that
``notes_llm`` already owns (no extra model to download or hold in memory).

JA-specific pieces that do not carry over (each is skipped for other pairs):
the English-passthrough heuristic, split-sentence revision, partial-EN preview
and the glossary's ASR-fix entries (their JA sources never match other text).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Sequence

from mimivox import notes_llm
from mimivox.confidence import MtSignals, mt_signals_from_logprobs
from mimivox.config import NOTES_MODEL_ID, _models
from mimivox.languages import LanguagePair
from mimivox.translate import (
    TranslationContext,
    _echoes_context,
    _is_mostly_english,
    get_translator,
    normalize_punctuation,
)

_SENTENCE_END = "。！？.!?"


def normalize_for(pair: LanguagePair, text: str) -> str:
    """Append the source language's sentence mark to an unpunctuated final."""
    if pair.source == "ja":
        return normalize_punctuation(text)
    text = text.strip()
    if text and text[-1] not in _SENTENCE_END:
        text += pair.src.terminal
    return text


def is_passthrough(pair: LanguagePair, text: str) -> bool:
    """JA-only: Whisper (language=ja) emitting English means a code-switch."""
    return pair.source == "ja" and _is_mostly_english(text)


def supports_revision(pair: LanguagePair) -> bool:
    return pair.source == "ja"


def supports_preview(pair: LanguagePair) -> bool:
    return pair.uses_cat


class PairContext(TranslationContext):
    """Rolling context for non-JA sources (no English-passthrough skip)."""

    def add(self, ja: str, en: str) -> None:
        en = en.strip()
        if en and ja.strip():
            self._pairs.append((ja, en))


def context_for(pair: LanguagePair) -> TranslationContext:
    return TranslationContext() if pair.source == "ja" else PairContext()


# ---------------------------------------------------------------------------
# Qwen translator
# ---------------------------------------------------------------------------
_SYSTEM = (
    "You are a simultaneous interpreter for live business meetings. Translate the "
    "user's {src} speech transcript into natural, fluent {tgt}.{style} Keep every "
    "number, name and technical term. Do not add, explain or omit anything. "
    "Reply with the {tgt} translation only."
)
_STYLE = {"ja": " Use polite business Japanese (です/ます)."}


def _system_prompt(pair: LanguagePair) -> str:
    return _SYSTEM.format(src=pair.src.name, tgt=pair.tgt.name, style=_STYLE.get(pair.target, ""))


def build_messages(
    pair: LanguagePair, text: str, context: Sequence[tuple[str, str]]
) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": _system_prompt(pair)}]
    for prev_src, prev_tgt in context:
        messages.append({"role": "user", "content": prev_src})
        messages.append({"role": "assistant", "content": prev_tgt})
    messages.append({"role": "user", "content": text})
    return messages


def max_tokens_for(text: str) -> int:
    return max(48, min(320, len(text.strip()) * 3))


def clean_output(raw: str) -> str:
    out = " ".join(ln.strip() for ln in raw.strip().splitlines() if ln.strip())
    out = re.sub(r"^(translation|english|japanese)\s*[:：]\s*", "", out, flags=re.I)
    return out.strip()


def _run_once(
    pair: LanguagePair,
    text: str,
    token_cb: Callable[[str], None],
    context: Sequence[tuple[str, str]],
) -> tuple[str, list[float]]:
    parts: list[str] = []
    logprobs: list[float] = []
    for piece, lp in notes_llm.chat_stream(
        build_messages(pair, text, context), max_tokens=max_tokens_for(text)
    ):
        token_cb(piece)
        parts.append(piece)
        logprobs.append(lp)
    return clean_output("".join(parts)), logprobs


def _translate_sync(
    pair: LanguagePair,
    text: str,
    token_cb: Callable[[str], None],
    context: Sequence[tuple[str, str]],
) -> tuple[str, float, MtSignals]:
    t0 = time.perf_counter()
    out, logprobs = _run_once(pair, text, token_cb, context)
    if context and (not out or _echoes_context(out, context, text)):
        out, logprobs = _run_once(pair, text, lambda _t: None, ())
    return out, time.perf_counter() - t0, mt_signals_from_logprobs(logprobs)


class LlmTranslator:
    """Final-only translator on the shared MLX lock (no preview path)."""

    def __init__(self, pair: LanguagePair) -> None:
        self.pair = pair
        self._pinned = False

    def load(self) -> None:
        """Load the model and keep it resident until ``close``."""
        notes_llm.load()
        if not self._pinned:
            notes_llm.pin()
            self._pinned = True

    def close(self) -> None:
        if self._pinned:
            notes_llm.unpin()
            self._pinned = False

    async def translate(
        self,
        text: str,
        token_cb: Callable[[str], None],
        *,
        priority: str = "final",
        context: Sequence[tuple[str, str]] = (),
    ) -> tuple[str, float, MtSignals]:
        if priority == "preview":
            return "", 0.0, MtSignals()
        loop = asyncio.get_running_loop()
        run = lambda: _translate_sync(self.pair, text, token_cb, context)  # noqa: E731
        lock = _models.cat_lock
        if lock is None:
            return await loop.run_in_executor(None, run)
        _models.cat_final_waiting += 1
        try:
            async with lock:
                return await loop.run_in_executor(None, run)
        finally:
            _models.cat_final_waiting = max(0, _models.cat_final_waiting - 1)


_llm: dict[str, LlmTranslator] = {}


def translator_for(pair: LanguagePair):
    """CAT singleton for ja-en; a per-pair Qwen translator otherwise."""
    if pair.uses_cat:
        return get_translator()
    if pair.code not in _llm:
        _llm[pair.code] = LlmTranslator(pair)
    return _llm[pair.code]


def unready_reason(pair: LanguagePair) -> str | None:
    """Why this pair cannot start (translation model not downloaded), else None."""
    if pair.uses_cat:
        return None
    from mimivox.models_service import model_present

    if model_present(NOTES_MODEL_ID)[0]:
        return None
    return (
        f"{pair.label} needs the on-device translation model "
        f"({NOTES_MODEL_ID}). Download it from Settings → Models first."
    )
