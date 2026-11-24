"""mimivox.asr — ASR loading and streaming transcription (Haas-ja or Whisper)."""

from __future__ import annotations

import io
import re as _re
import sys as _sys
import threading as _threading
import time
from collections.abc import Callable
from typing import Any

import mlx_whisper
from mlx_whisper import load_models

from mimivox.confidence import AsrSignals, asr_signals_from_segments
from mimivox.config import _SEG_RE, HAAS_MODEL, SAMPLE_RATE, WHISPER_MODEL

# ---------------------------------------------------------------------------
# Active ASR model (swappable per session)
# ---------------------------------------------------------------------------
# Defaults to turbo-q8 (file + live). run() may set ASR_MODEL_LIVE override
# for live experiments and preloads non-default models before first chunk.
_asr_model_id: str = WHISPER_MODEL


def set_asr_model(model_id: str) -> None:
    """Set the model id used by _do_asr_streaming. Call at session start."""
    global _asr_model_id
    _asr_model_id = model_id or WHISPER_MODEL


def get_asr_model() -> str:
    return _asr_model_id


# Whisper language for the session's source (see mimivox.languages).
_asr_language: str = "ja"


def set_asr_language(code: str) -> None:
    global _asr_language
    _asr_language = code or "ja"


def get_asr_language() -> str:
    return _asr_language


# Whisper initial_prompt: neutral punctuation plus the glossary's term sources.
# "" means no prompt (languages without a neutral base, e.g. Korean).
_asr_prompt: str = "。、"


def set_asr_prompt(prompt: str | None) -> None:
    global _asr_prompt
    _asr_prompt = "。、" if prompt is None else prompt


def get_asr_prompt() -> str:
    return _asr_prompt


def is_haas_model(model_id: str) -> bool:
    return model_id == HAAS_MODEL


def preload_asr_model(model_id: str) -> None:
    """Eagerly load a model so the first chunk does not stall (call off the loop)."""
    if is_haas_model(model_id):
        _load_haas(model_id)
        return
    from mlx_whisper import load_models

    load_models.load_model(model_id or WHISPER_MODEL)


# ---------------------------------------------------------------------------
# Haas-ja (Parakeet-TDT, Japanese only)
# ---------------------------------------------------------------------------
_haas_lock = _threading.Lock()
_haas: tuple[str, Any, Any] | None = None  # (model_id, HaasModel, SentencePieceProcessor)


def _load_haas(model_id: str) -> tuple[Any, Any]:
    global _haas
    with _haas_lock:
        if _haas is None or _haas[0] != model_id:
            import sentencepiece as spm
            from haas_mlx import HaasModel

            from mimivox.models_service import model_dir

            path = model_dir(model_id)
            sp = spm.SentencePieceProcessor(model_file=str(path / "tokenizer.model"))
            _haas = (model_id, HaasModel.load(path), sp)
        return _haas[1], _haas[2]


def _haas_transcribe(audio: np.ndarray) -> str:
    import mlx.core as mx
    from haas_mlx.longform import transcribe_text

    model, sp = _load_haas(get_asr_model())
    return transcribe_text(model, mx.array(audio.astype(np.float32, copy=False)), sp).strip()


# ---------------------------------------------------------------------------
# Thread-local stdout router
# ---------------------------------------------------------------------------
class _RoutingStdout(io.TextIOBase):
    """Thread-local stdout router installed once at module load.

    Writes are dispatched to a per-thread capture callable when one is
    registered, otherwise forwarded to the real stdout. This isolates
    mlx_whisper's verbose segment output to the executor thread running
    _do_asr_streaming without touching any other thread's stdout.
    """

    def __init__(self, real: io.TextIOBase) -> None:
        self._real = real
        self._local = _threading.local()

    def write(self, s: str) -> int:
        h = getattr(self._local, "capture", None)
        return h(s) if h is not None else self._real.write(s)

    def flush(self) -> None:
        self._real.flush()


_routing_stdout = _RoutingStdout(_sys.stdout)
_sys.stdout = _routing_stdout


# ---------------------------------------------------------------------------
# ASR inference
# ---------------------------------------------------------------------------
def _do_asr_streaming(
    audio: np.ndarray,
    on_segment: Callable[[str], None],
) -> tuple[str, float, AsrSignals | None]:
    """Single-pass large-model transcription. Returns (text, seconds, signals).

    verbose=True makes mlx_whisper emit one line per decoded segment:
        [00:00.000 --> 00:02.340]  テキスト
    Those lines are captured via a thread-local handler on _routing_stdout,
    isolated to this executor thread. Other threads are unaffected.
    """

    def _handle(s: str) -> int:
        m = _SEG_RE.match(s.rstrip())
        if m:
            on_segment(m.group(1).strip())
        return len(s)

    # Trim trailing low-amplitude samples: Whisper is known to loop-hallucinate
    # (e.g. "移植、 master config, " ×25) when fed chunks that end in silence.
    if audio.size:
        above = np.abs(audio) >= 0.01
        if above.any():
            last = int(np.flatnonzero(above)[-1])
            pad = min(SAMPLE_RATE // 50, audio.size - last - 1)  # ~20ms breath
            audio = audio[: min(audio.size, last + pad + 1)]

    if is_haas_model(get_asr_model()):
        # No segments, logprobs or prompt: one final text per chunk; the
        # glossary still applies to the text downstream.
        t0 = time.perf_counter()
        text = _haas_transcribe(audio)
        if text:
            on_segment(text)
        return text, time.perf_counter() - t0, None

    _routing_stdout._local.capture = _handle
    t0 = time.perf_counter()
    try:
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=get_asr_model(),
            language=get_asr_language(),
            task="transcribe",
            verbose=True,
            temperature=0,
            condition_on_previous_text=False,
            no_speech_threshold=0.8,
            # Neutral JA punctuation bias, plus only the user's glossary terms
            # (generic domain prompts caused hallucinations on weak mic chunks).
            initial_prompt=get_asr_prompt() or None,
        )
    finally:
        _routing_stdout._local.capture = None
    signals = asr_signals_from_segments(result.get("segments"))
    return result["text"].strip(), time.perf_counter() - t0, signals


# ---------------------------------------------------------------------------
# Content filters
# ---------------------------------------------------------------------------
# JA/EN punctuation + whitespace only (no letters, kana, or kanji).
_NON_CONTENT_RE = _re.compile(
    r"^[\s\u3000"
    r"。、．，\.！？!?・…‥\-—–~～"
    r"「」『』（）()\[\]【】〈〉《》"
    r"\"'‘’“”`´"
    r"]+$"
)


def is_non_content_asr(text: str) -> bool:
    """True when ASR output is empty or punctuation-only (e.g. ``。`` / ``、``)."""
    t = (text or "").strip()
    if not t:
        return True
    return _NON_CONTENT_RE.match(t) is not None


# ---------------------------------------------------------------------------
# Hallucination deduplication
# ---------------------------------------------------------------------------
def _deduplicate_phrases(text: str) -> str:
    """Collapse repeated-substring loops from Whisper decode hallucinations.

    On borderline-silence audio, Whisper latches onto a single token/phrase
    and repeats it many times (e.g. アクセプトスクリテリアを ×17, or
    "移植、 master config, " ×25 where the loop unit includes punctuation).
    Two passes:
      1. Regex: collapse any 1-15 char unit (optionally followed by a
         punctuation/space separator) repeated 3+ times back to one copy.
         The separator-aware pattern catches loops the original missed.
      2. Token-dominance: if one token is >50% of a ≥8-token output, keep
         only its first 2 occurrences.
    """
    import re

    # Pass 1: separator-aware repeat collapse (handles "X、 " / "X, " loops).
    text = re.sub(r"([^。、\s]{1,12}[、。，\.]?\s*)\1{3,}", r"\1", text)
    # Pass 1b: legacy fallback for plain repeats with no punctuation.
    text = re.sub(r"(.{1,15}?)\1{4,}", r"\1", text)

    tokens = re.split(r"[\s　、。！？]+", text.strip())
    tokens = [t for t in tokens if t]
    if len(tokens) < 8:
        return text

    from collections import Counter

    counts = Counter(tokens)
    most_common_token, most_common_count = counts.most_common(1)[0]

    if most_common_count >= 4 and most_common_count > len(tokens) * 0.5:
        kept = []
        seen = 0
        for t in tokens:
            if t == most_common_token:
                seen += 1
                if seen <= 2:
                    kept.append(t)
            else:
                kept.append(t)
        if len(kept) > len(tokens) * 0.8:
            return " ".join(kept[: max(3, len(tokens) // 4)])
        return " ".join(kept)
    return text


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------
def load_asr_model() -> None:
    print(f"⏳ Whisper large: {WHISPER_MODEL}")
    t0 = time.perf_counter()
    load_models.load_model(WHISPER_MODEL)
    print(f"   ✅ {time.perf_counter() - t0:.2f}s")


import numpy as np  # noqa: E402
