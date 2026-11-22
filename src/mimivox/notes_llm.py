"""mimivox.notes_llm — On-device instruct LLM for meeting notes / Ask.

Loaded lazily on first use (never at server startup) and evictable. Callers run
``generate`` under the shared MLX lock (see notes_service) so it never overlaps
Whisper or CAT inference.
"""

from __future__ import annotations

import gc
import threading
from collections.abc import Callable
from typing import Any

from mimivox.config import NOTES_MODEL_ID

_lock = threading.Lock()
_model: Any = None
_tokenizer: Any = None
_loaded_id: str | None = None
PAUSE_EVERY = 4  # tokens between generate()'s pause() calls
_pins = 0  # live-translation sessions holding the model; unload() refuses while > 0


def is_loaded() -> bool:
    return _model is not None


def loaded_id() -> str | None:
    return _loaded_id


def load(model_id: str = NOTES_MODEL_ID) -> None:
    global _model, _tokenizer, _loaded_id
    with _lock:
        if _model is not None and _loaded_id == model_id:
            return
        from mlx_lm import load as mlx_load

        _model, _tokenizer = mlx_load(model_id)
        _loaded_id = model_id


def pin() -> None:
    """Keep the model resident (a session translates with it); pair with ``unpin``."""
    global _pins
    with _lock:
        _pins += 1


def unpin() -> None:
    global _pins
    with _lock:
        _pins = max(0, _pins - 1)


def unload() -> bool:
    """Drop the model and return its memory to the OS. True if one was unloaded.

    A pinned model stays: the notes idle timer must not evict it mid-session.
    """
    global _model, _tokenizer, _loaded_id
    with _lock:
        if _pins:
            return False
        had = _model is not None
        _model = _tokenizer = _loaded_id = None
    if had:
        gc.collect()
        try:
            import mlx.core as mx

            mx.clear_cache()
        except Exception:
            pass
    return had


def generate(
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    pause: Callable[[], None] | None = None,
) -> str:
    """Greedy chat completion. The reply is prefilled with ``{`` to force JSON.

    ``pause`` runs every ``PAUSE_EVERY`` tokens, so a caller can hand the GPU
    to live captions mid-reply.
    """
    load()
    from mlx_lm import stream_generate

    prompt = _tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    parts = ["{"]
    chunks = stream_generate(_model, _tokenizer, prompt=prompt + "{", max_tokens=max_tokens)
    for n, chunk in enumerate(chunks, start=1):
        parts.append(chunk.text)
        if pause is not None and n % PAUSE_EVERY == 0:
            pause()
    return "".join(parts)


def chat_stream(messages: list[dict[str, str]], *, max_tokens: int):
    """Greedy plain-text chat completion; yields ``(text, logprob)`` per token."""
    load()
    from mlx_lm import stream_generate

    prompt = _tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    for chunk in stream_generate(_model, _tokenizer, prompt=prompt, max_tokens=max_tokens):
        if chunk.text:
            yield chunk.text, float(chunk.logprobs[chunk.token])
