from unittest.mock import MagicMock

from mimivox.active_models import active_models
from mimivox.api_adapter import APISessionState
from mimivox.config import CAT_MODEL_ID, HAAS_JA_MODEL, NOTES_MODEL_ID, WHISPER_MODEL
from mimivox.languages import LanguagePair


def test_japanese_to_english_uses_haas_and_cat():
    assert active_models(LanguagePair("ja", "en"), None) == {
        "asr": {"id": HAAS_JA_MODEL, "label": "Haas-ja"},
        "translation": {"id": CAT_MODEL_ID, "label": "CAT-Translate"},
    }


def test_other_pairs_use_whisper_and_the_notes_model():
    models = active_models(LanguagePair("en", "ja"), None)
    assert models["asr"] == {"id": WHISPER_MODEL, "label": "Whisper turbo"}
    assert models["translation"]["id"] == NOTES_MODEL_ID


def test_a_live_override_names_the_override_and_unknown_ids_show_the_repo_name():
    models = active_models(LanguagePair("ja", "en"), "someone/Custom-ASR-v2")
    assert models["asr"] == {"id": "someone/Custom-ASR-v2", "label": "Custom-ASR-v2"}


def test_status_lists_active_models_only_while_a_session_runs():
    state = APISessionState()
    state.active_models = active_models(LanguagePair("ja", "en"), None)
    assert "active_models" not in state.status_payload()

    state.pipeline_task = MagicMock(done=MagicMock(return_value=False))
    payload = state.status_payload()
    assert payload["active_models"]["asr"]["label"] == "Haas-ja"
    assert payload["active_models"]["translation"]["label"] == "CAT-Translate"
