"""mimivox.translate — Local-only JA→EN translation backend.

  * ``CatTranslator`` — local CAT-Translate via mlx_lm (on-device, default).

MimiVox is fully local/offline.
"""

from __future__ import annotations

import asyncio
import collections
import re
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol

from mimivox.confidence import MtSignals, mt_signals_from_logprobs
from mimivox.config import (
    _JAPANESE_RE,
    _LONG_PHRASE_PROMPT,
    _PROMPT_LEAK_MARKERS,
    _PROMPT_SUFFIX_RE,
    _SHORT_PHRASE_PROMPT,
    _ULTRA_SHORT_PROMPT,
    CAT_MODEL_ID,
    CONTEXT_TURNS,
    GLOSSARY,
    _models,
)
from mimivox.number_check import group_digits, unsupported_numbers

# Active glossary map — replaced atomically via set_active_glossary().
# Starts as a copy of the built-in GLOSSARY; settings overlay on top.
_active_glossary: dict[str, str] = dict(GLOSSARY)
_glossary_lock = threading.Lock()


def set_active_glossary(glossary: dict[str, str]) -> None:
    """Replace the active glossary map (thread-safe, atomic swap)."""
    global _active_glossary
    with _glossary_lock:
        _active_glossary = dict(glossary)


def get_active_glossary() -> dict[str, str]:
    with _glossary_lock:
        return dict(_active_glossary)


# ---------------------------------------------------------------------------
# CAT-specific post-processing (kept as module helpers for back-compat
# with tests and any external callers)
# ---------------------------------------------------------------------------
def normalize_punctuation(text: str) -> str:
    text = text.strip()
    if text and text[-1] not in "。！？.!?":
        text += "。"
    return text


def translation_cache_key(text: str) -> str:
    """Space-insensitive JA key so partial joins and finals share a cache entry.

    Whisper segment joins often insert spaces (``A。 B``) while the final
    result is ``A。B`` — without this, a successful partial-EN still forces a
    second full CAT on the final.
    """
    t = (text or "").strip()
    return re.sub(r"[\s\u3000]+", "", t)


def apply_glossary(text: str, glossary: dict[str, str] | None = None) -> str:
    """Apply glossary replacements.

    Uses the optional map if provided; otherwise the active (settings-aware)
    glossary. Never mutates the built-in GLOSSARY constant in place.
    """
    g = glossary if glossary is not None else get_active_glossary()
    # Longest source first so "よろしくお願いします" wins over "お願いします".
    for wrong in sorted(g, key=len, reverse=True):
        text = text.replace(wrong, g[wrong])
    return text


def _is_mostly_english(text: str) -> bool:
    """Return True if ASR text has negligible Japanese content.

    When the speaker code-switches to English mid-meeting, the ASR
    (configured with language="ja") outputs English text labelled as JA.
    Passing this to CAT-Translate produces prompt-leak hallucinations.
    We detect this case and skip CAT, passing the English text through.
    """
    stripped = text.strip()
    if not stripped:
        return False
    ja_chars = len(_JAPANESE_RE.findall(stripped))
    if ja_chars >= 3:
        return False
    total_alpha = sum(1 for c in stripped if c.isalpha())
    if total_alpha == 0:
        return False
    return ja_chars / max(total_alpha, 1) < 0.2


def _clean_cat(raw: str) -> tuple[str, bool, bool]:
    """Clean CAT output; also report (prompt_leak_cleaned, japanese_stripped)."""
    leaked = False
    raw_lower = raw.lower()
    for marker in _PROMPT_LEAK_MARKERS:
        idx = raw_lower.find(marker.lower())
        if idx == -1:
            continue
        if marker.endswith(":") and not raw_lower[:idx].strip():
            raw = raw[idx + len(marker) :].strip()
            raw_lower = raw_lower[idx + len(marker) :].strip()
            continue
        leaked = True
        raw = raw[:idx].strip()
        raw_lower = raw_lower[:idx].strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    result = " ".join(lines)
    if len(result.split()) < 2:
        for sep in (".", "!", "?"):
            idx = raw.find(sep)
            if 0 < idx < 120:
                candidate = raw[: idx + 1].strip()
                if not any(m.lower() in candidate.lower() for m in _PROMPT_LEAK_MARKERS):
                    result = candidate
                    break
    result = _PROMPT_SUFFIX_RE.sub("", result).strip().rstrip(".")
    stripped = bool(_JAPANESE_RE.search(result))
    if stripped:
        result = _JAPANESE_RE.sub(" ", result)
        result = re.sub(r"[。、！？・…]+", " ", result)
        result = re.sub(r"\s+", " ", result).strip().strip(".,;: ")
    return result, leaked, stripped


def clean_cat_output(raw: str, ja_text: str = "") -> str:
    """Strip leaked system-prompt instructions, first-sentence fallback, and Japanese leakage."""
    return _clean_cat(raw)[0]


# ---------------------------------------------------------------------------
# CAT prompt selection
# ---------------------------------------------------------------------------
def _prompt_and_tokens(ja_text: str) -> tuple[str, int]:
    """Pick CAT prompt + max_tokens.

    Caps are tight on purpose: meeting EN is typically 15–40 words. Excess
    max_tokens only burns wall-clock after EOS (mlx_lm still stops early, but
    long prompts with high caps cost more prefill/decode headroom).
    """
    n = len(ja_text.strip())
    if n <= 5:
        return _ULTRA_SHORT_PROMPT.format(ja=ja_text), 16
    if n <= 25:
        return _SHORT_PHRASE_PROMPT.format(ja=ja_text), 32
    if n <= 60:
        return _LONG_PHRASE_PROMPT.format(ja=ja_text), 64
    # Long lines (revision joins reach 120 chars) need about 0.75 tokens per
    # JA char; a flat 80 cut a 125-char sentence off mid-clause.
    return _LONG_PHRASE_PROMPT.format(ja=ja_text), max(80, n + 16)


def _chat_messages(
    ja_text: str, context: Sequence[tuple[str, str]]
) -> tuple[list[dict[str, str]], int]:
    """Build CAT chat turns: each prior pair as user/assistant, then the current line.

    Prior lines go in as real turns (same prompt template) so CAT uses them as
    context without translating them again.
    """
    messages: list[dict[str, str]] = []
    for prev_ja, prev_en in context:
        messages.append({"role": "user", "content": _prompt_and_tokens(prev_ja)[0]})
        messages.append({"role": "assistant", "content": prev_en})
    content, max_tok = _prompt_and_tokens(ja_text)
    messages.append({"role": "user", "content": content})
    return messages, max_tok


_ECHO_MIN_WORDS = 4


def _word_ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _sentences(text: str, terminators: str) -> list[str]:
    parts = re.split(f"(?<=[{terminators}])", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _normalized(sentence: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", sentence.lower()))


def _ja_sentence_keys(ja: str) -> set[str]:
    return {translation_cache_key(s.rstrip("。！？!?")) for s in _sentences(ja, "。！？!?")} - {""}


def _echoes_context(en: str, context: Sequence[tuple[str, str]], ja_text: str = "") -> bool:
    """True when CAT copied from a prior line instead of translating.

    With chat-turn context CAT sometimes repeats the previous answer. Two shapes
    were measured on the meeting fixture: a shared run of words ("よろしく
    お願いします" came back as the prior line's "I will prepare the document"),
    and a prior sentence repeated whole ("Not yet. Let's discuss ..." for a JA
    line with no まだ; "Is that it?" for four different lines in a row, too short
    for the word-run check). A repeated sentence counts unless the prior line
    shares a Japanese sentence with this one, as with a second はい.
    The caller retranslates without context.
    """
    produced = _word_ngrams(en, _ECHO_MIN_WORDS)
    if any(produced & _word_ngrams(prev_en, _ECHO_MIN_WORDS) for _, prev_en in context):
        return True
    said = _ja_sentence_keys(ja_text)
    prior = {
        _normalized(s)
        for prev_ja, prev_en in context
        if not said & _ja_sentence_keys(prev_ja)
        for s in _sentences(prev_en, ".!?")
    }
    prior.discard("")
    return any(_normalized(s) in prior for s in _sentences(en, ".!?"))


_CONTEXT_MIN_CHARS = 6


def _ja_content(ja: str) -> str:
    return re.sub(r"[\s\u3000。、！？!?,.「」…ー]+", "", ja)


def _too_short(en: str, ja: str) -> bool:
    """True when the English is far shorter than any translation of the Japanese.

    With several context turns CAT can collapse a long line into one short
    question ("Is that clear?" for 90 characters of Japanese). Real answers run
    about one English word per 2-3 Japanese characters; this flags under one per 6.
    """
    return len(re.findall(r"[A-Za-z0-9']+", en)) * 6 < len(_ja_content(ja))


class TranslationContext:
    """Rolling window of recent finished (JA, EN) lines for one session."""

    def __init__(self) -> None:
        self._pairs: collections.deque[tuple[str, str]] = collections.deque(maxlen=CONTEXT_TURNS)

    def add(self, ja: str, en: str) -> None:
        """Remember a finished line; empty, fragment and English-passthrough lines are skipped.

        A fragment such as ですかね gives CAT a short answer it copies onto the
        next lines in place of translating them.
        """
        en = en.strip()
        if en and len(_ja_content(ja)) >= _CONTEXT_MIN_CHARS and not _is_mostly_english(ja):
            self._pairs.append((ja, en))

    def pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._pairs)

    def restore(self, pairs: Sequence[tuple[str, str]]) -> None:
        """Roll back to an earlier ``pairs()`` snapshot (drops lines added since)."""
        self._pairs.clear()
        self._pairs.extend(pairs)


# Kana, kanji, CJK punctuation, fullwidth forms, and the byte-fallback tokens
# that start them in UTF-8 (U+3000..U+9FFF lead with E3..E9, fullwidth with EF).
_CJK_PIECE_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
_CJK_BYTE_RE = re.compile(r"<0x(?:E[3-9]|EF)>")


def japanese_token_ids(vocab: dict[str, int]) -> list[int]:
    """Ids of tokens that put Japanese script into the output.

    CAT copies terms it does not know straight into the English ("reported to
    the部長？"), and cleanup can only delete them, so the word is lost. Banning
    these tokens while decoding makes CAT pick its best English rendering.
    """
    return sorted(
        i
        for piece, i in vocab.items()
        if _CJK_PIECE_RE.search(piece) or _CJK_BYTE_RE.fullmatch(piece)
    )


# A reply that labels itself ("Translation: ...") came from a context turn that
# was a sentence fragment; without context CAT answers plainly.
_LABELLED_RE = re.compile(r"\s*(?:translation|english translation)\s*:", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Translator Protocol
# ---------------------------------------------------------------------------
class Translator(Protocol):
    """A JA→EN translation backend.

    ``token_cb`` is called incrementally with each produced token (or SSE
    delta). It may be a no-op. Implementations must be safe to ``await``
    from an asyncio event loop.
    """

    def load(self) -> None: ...
    async def translate(
        self,
        ja_text: str,
        token_cb: Callable[[str], None],
        *,
        context: Sequence[tuple[str, str]] = (),
    ) -> tuple[str, float] | tuple[str, float, MtSignals]: ...


# ---------------------------------------------------------------------------
# CAT backend (default, local)
# ---------------------------------------------------------------------------
# Preview partial-EN: bounded so they release cat_lock quickly if a final is
# waiting (cancel cannot abort mid-MLX generate). 48 is enough for a typical
# meeting clause so a complete preview can be reused as the final (cache hit).
_PREVIEW_MAX_TOKENS = 48


def _stream_scored(
    ja_text: str,
    token_cb: Callable[[str], None],
    max_tokens_cap: int | None,
    context: Sequence[tuple[str, str]],
) -> tuple[str, list[float]]:
    """Run CAT once; returns (raw text, logprob of each sampled token)."""
    from mlx_lm import stream_generate

    messages, max_tok = _chat_messages(ja_text, context)
    if max_tokens_cap is not None:
        max_tok = min(max_tok, max_tokens_cap)
    prompt = _models.cat_tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    banned = _models.cat_banned_ids
    processors = None
    if banned is not None:

        def ban_japanese(_tokens: Any, logits: Any) -> Any:
            logits[:, banned] = -float("inf")
            return logits

        processors = [ban_japanese]
    collected: list[str] = []
    logprobs: list[float] = []
    for chunk in stream_generate(
        _models.cat_model,
        _models.cat_tokenizer,
        prompt=prompt,
        max_tokens=max_tok,
        logits_processors=processors,
    ):
        if chunk.text:
            token_cb(chunk.text)
            collected.append(chunk.text)
            logprobs.append(float(chunk.logprobs[chunk.token]))
    return "".join(collected), logprobs


def _do_translate_streaming(
    ja_text: str,
    token_cb: Callable[[str], None],
    max_tokens_cap: int | None = None,
    context: Sequence[tuple[str, str]] = (),
) -> tuple[str, float, MtSignals]:
    """Translate ja_text, firing token_cb(text) for each generated token.
    Returns (en, seconds, signals). Runs in a thread-pool executor; token_cb
    must be thread-safe."""
    t0 = time.perf_counter()

    def run(ja: str, ctx: Sequence[tuple[str, str]], cb: Callable[[str], None]):
        raw, logprobs = _stream_scored(ja, cb, max_tokens_cap, ctx)
        return raw, logprobs, *_clean_cat(raw)

    raw, logprobs, en, leaked, stripped = run(ja_text, context, token_cb)
    # Context turns can pull code-switched (glossary-substituted) input into a
    # Japanese reply, which cleaning strips to nothing, make CAT repeat a prior
    # line, or make it label its answer: retry without context.
    # The retry is scored like any run; the retry alone is not a quality signal
    # (it fired on clean lines whose translations were fine).
    if context and (
        not en
        or _echoes_context(en, context, ja_text)
        or _too_short(en, ja_text)
        or _LABELLED_RE.match(raw)
    ):
        context = ()
        raw, logprobs, en, leaked, stripped = run(ja_text, (), lambda _t: None)
    # CAT misreads bare numerals ("7000" -> "70,000") but not grouped ones.
    # Retry once with grouped digits and no context; keep whichever answer has
    # fewer numbers the Japanese does not contain, and flag what remains.
    wrong = unsupported_numbers(ja_text, en)
    if wrong and (context or group_digits(ja_text) != ja_text):
        retry = run(group_digits(ja_text), (), lambda _t: None)
        retry_wrong = unsupported_numbers(ja_text, retry[2])
        if len(retry_wrong) < len(wrong):
            (raw, logprobs, en, leaked, stripped), wrong = retry, retry_wrong
    return (
        en,
        time.perf_counter() - t0,
        mt_signals_from_logprobs(
            logprobs,
            prompt_leak_cleaned=leaked,
            japanese_stripped=stripped,
            numbers_mismatch=bool(wrong),
        ),
    )


class CatTranslator:
    """Wraps the existing mlx_lm CAT path. Acquires the shared MLX lock
    and offloads inference to the default executor internally, so callers
    simply ``await translator.translate(...)``.
    """

    def load(self) -> None:
        print(f"⏳ CAT      : {CAT_MODEL_ID}")
        t0 = time.perf_counter()
        from mlx_lm import load

        _models.cat_model, _models.cat_tokenizer = load(CAT_MODEL_ID)
        import mlx.core as mx

        _models.cat_banned_ids = mx.array(japanese_token_ids(_models.cat_tokenizer.get_vocab()))
        print(f"   ✅ {time.perf_counter() - t0:.2f}s")

    async def translate(
        self,
        ja_text: str,
        token_cb: Callable[[str], None],
        *,
        priority: str = "final",
        context: Sequence[tuple[str, str]] = (),
    ) -> tuple[str, float, MtSignals]:
        """Translate JA→EN; returns (en, seconds, signals).

        ``priority="final"`` always takes ``cat_lock`` and marks final-waiting
        so preview work will not start ahead of it.

        ``priority="preview"`` is best-effort: skips if the lock is held or a
        final is waiting, and caps generation length so any in-flight preview
        releases the lock quickly.
        """
        loop = asyncio.get_running_loop()
        # Production assigns the same lock to CAT and ASR to prevent Metal
        # contention.
        lock = _models.cat_lock
        if lock is None:
            # Unit tests / early init without locks.
            cap = _PREVIEW_MAX_TOKENS if priority == "preview" else None
            return await loop.run_in_executor(
                None, _do_translate_streaming, ja_text, token_cb, cap, context
            )

        if priority == "preview":
            if _models.cat_final_waiting > 0 or lock.locked():
                return "", 0.0, MtSignals()
            async with lock:
                if _models.cat_final_waiting > 0:
                    return "", 0.0, MtSignals()
                return await loop.run_in_executor(
                    None,
                    _do_translate_streaming,
                    ja_text,
                    token_cb,
                    _PREVIEW_MAX_TOKENS,
                    context,
                )

        _models.cat_final_waiting += 1
        try:
            async with lock:
                return await loop.run_in_executor(
                    None, _do_translate_streaming, ja_text, token_cb, None, context
                )
        finally:
            _models.cat_final_waiting = max(0, _models.cat_final_waiting - 1)


# ---------------------------------------------------------------------------
# Direction-aware entry point (reply mode, EN→JA and back-translation)
# ---------------------------------------------------------------------------
# CAT-Translate is bidirectional and trained on this exact prompt. The live
# JA→EN path above keeps its own tuned prompts; this entry point is separate.
Direction = Literal["ja-en", "en-ja"]
DIRECTIONS: tuple[str, ...] = ("ja-en", "en-ja")
_DIRECTION_LANGS: dict[str, tuple[str, str]] = {
    "ja-en": ("Japanese", "English"),
    "en-ja": ("English", "Japanese"),
}
_OFFICIAL_PROMPT = "Translate the following {src_lang} text into {tgt_lang}.\n\n {src_text}"


def official_prompt(text: str, direction: Direction) -> str:
    src, tgt = _DIRECTION_LANGS[direction]
    return _OFFICIAL_PROMPT.format(src_lang=src, tgt_lang=tgt, src_text=text)


def _do_translate_directed(text: str, direction: Direction) -> str:
    from mlx_lm import generate

    prompt = _models.cat_tokenizer.apply_chat_template(
        [{"role": "user", "content": official_prompt(text, direction)}],
        tokenize=False,
        add_generation_prompt=True,
    )
    max_tok = max(64, min(512, len(text) * 3))
    raw = generate(
        _models.cat_model, _models.cat_tokenizer, prompt=prompt, max_tokens=max_tok, verbose=False
    )
    return raw.strip()


async def translate_directed(text: str, direction: Direction) -> str:
    """Translate ``text`` in ``direction`` with the official CAT prompt.

    Takes the same shared lock as live inference (and counts as a waiting
    final so previews yield), so it never runs concurrently with it.
    """
    loop = asyncio.get_running_loop()
    lock = _models.cat_lock
    if lock is None:
        return await loop.run_in_executor(None, _do_translate_directed, text, direction)
    _models.cat_final_waiting += 1
    try:
        async with lock:
            return await loop.run_in_executor(None, _do_translate_directed, text, direction)
    finally:
        _models.cat_final_waiting = max(0, _models.cat_final_waiting - 1)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_translator: Translator | None = None


def get_translator() -> Translator:
    """Return the process-wide translator singleton (always local CAT)."""
    global _translator
    if _translator is None:
        _translator = CatTranslator()
    return _translator


def reset_translator() -> None:
    """Test hook: clear the cached singleton so a new selection takes effect."""
    global _translator
    _translator = None
