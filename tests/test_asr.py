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


def test_haas_model_skips_whisper(monkeypatch):
    import numpy as np

    from mimivox.config import HAAS_MODEL, WHISPER_MODEL

    def no_whisper(*a, **kw):
        raise AssertionError("Whisper must not run for Haas")

    monkeypatch.setattr(asr.mlx_whisper, "transcribe", no_whisper)
    monkeypatch.setattr(asr, "_haas_transcribe", lambda audio: "こんにちは")
    segments: list[str] = []
    asr.set_asr_model(HAAS_MODEL)
    try:
        audio = np.full(16000, 0.1, dtype=np.float32)
        text, _, signals = asr._do_asr_streaming(audio, segments.append)
    finally:
        asr.set_asr_model(WHISPER_MODEL)
    assert (text, segments, signals) == ("こんにちは", ["こんにちは"], None)
