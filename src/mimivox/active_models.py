"""mimivox.active_models — name the models a running session uses, for the Live header."""

from __future__ import annotations

from mimivox.config import CAT_MODEL_ID, HAAS_JA_MODEL, NOTES_MODEL_ID, WHISPER_MODEL
from mimivox.languages import LanguagePair

_LABELS = {
    HAAS_JA_MODEL: "Haas-ja",
    WHISPER_MODEL: "Whisper turbo",
    CAT_MODEL_ID: "CAT-Translate",
    "mlx-community/Qwen3-4B-Instruct-2507-4bit": "Qwen3 4B",
}


def _model(model_id: str) -> dict[str, str]:
    return {"id": model_id, "label": _LABELS.get(model_id, model_id.rsplit("/", 1)[-1])}


def active_models(pair: LanguagePair, asr_model_id: str | None) -> dict[str, dict[str, str]]:
    """The recognizer and translator a session runs: the same choice pipeline.run makes."""
    translator = CAT_MODEL_ID if pair.uses_cat else NOTES_MODEL_ID
    return {
        "asr": _model(asr_model_id or pair.src.asr_model),
        "translation": _model(translator),
    }
