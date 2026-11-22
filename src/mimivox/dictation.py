"""mimivox.dictation — Turn a short spoken clip into text for the Translate tab.

Uses the Whisper model the captions already use, so dictation needs no extra
download. The clip is mono float32 at 16 kHz. Whisper detects the language;
only Japanese and English are expected, so any other guess is redone with the
caller's hint.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from mimivox.config import SAMPLE_RATE

MAX_SECONDS = 60
LANGUAGES = ("ja", "en")


@dataclass(frozen=True)
class Dictation:
    text: str
    language: str


def _transcribe(audio: np.ndarray, language: str | None) -> tuple[str, str]:
    import mlx_whisper

    from mimivox.asr import get_asr_model

    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=get_asr_model(),
        language=language,
        task="transcribe",
        temperature=0,
        condition_on_previous_text=False,
        no_speech_threshold=0.8,
        initial_prompt="。、" if language == "ja" else None,
    )
    return str(result.get("text") or "").strip(), str(result.get("language") or language or "")


def transcribe(audio: np.ndarray, hint: str) -> Dictation:
    """Text of the clip, with its language. ``hint`` is "ja" or "en"."""
    from mimivox.asr import _deduplicate_phrases, is_non_content_asr

    text, language = _transcribe(audio, None)
    if language not in LANGUAGES:
        text, language = _transcribe(audio, hint)
    text = "" if is_non_content_asr(text) else _deduplicate_phrases(text)
    return Dictation(text=text, language=language)


def decode_clip(raw: bytes) -> np.ndarray | str:
    """The clip as samples, or why it cannot be used."""
    if not raw or len(raw) % 4:
        return "Expected mono float32 audio at 16 kHz."
    audio = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    if audio.size > MAX_SECONDS * SAMPLE_RATE:
        return f"Dictation is limited to {MAX_SECONDS} seconds."
    if audio.size < SAMPLE_RATE // 4:
        return "The recording was too short."
    return audio
