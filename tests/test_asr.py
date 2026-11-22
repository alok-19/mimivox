import inspect

from mimivox import asr


def test_is_non_content_asr():
    assert asr.is_non_content_asr("") is True
    assert asr.is_non_content_asr("   ") is True
    assert asr.is_non_content_asr("。") is True
    assert asr.is_non_content_asr("、") is True
    assert asr.is_non_content_asr("...！！") is True
    assert asr.is_non_content_asr("「」") is True
    assert asr.is_non_content_asr("はい。") is False
    assert asr.is_non_content_asr("Eのフロント") is False


def test_deduplicate_phrases_noop():
    text = "これは普通の文章です。"
    assert asr._deduplicate_phrases(text) == text


def test_deduplicate_phrases_collapses_repeats():
    text = "テスト " * 10
    result = asr._deduplicate_phrases(text)
    assert result != text
    assert "テスト" in result


def test_asr_initial_prompt_defaults_to_neutral_punctuation():
    assert asr.get_asr_prompt() == "。、"


def test_asr_model_is_swappable_per_session():
    from mimivox.config import ASR_MODEL_FILE, ASR_MODEL_LIVE, WHISPER_MODEL

    try:
        asr.set_asr_model(ASR_MODEL_LIVE)
        assert asr.get_asr_model() == ASR_MODEL_LIVE
        asr.set_asr_model(ASR_MODEL_FILE)
        assert asr.get_asr_model() == ASR_MODEL_FILE
        # Empty -> default file model.
        asr.set_asr_model("")
        assert asr.get_asr_model() == WHISPER_MODEL
    finally:
        asr.set_asr_model(WHISPER_MODEL)


def test_asr_streaming_uses_active_model_id():
    src = inspect.getsource(asr._do_asr_streaming)
    assert "get_asr_model()" in src
