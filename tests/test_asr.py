import pytest

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
    from mimivox.config import HAAS_JA_MODEL, WHISPER_MODEL

    try:
        asr.set_asr_model(HAAS_JA_MODEL)
        assert asr.get_asr_model() == HAAS_JA_MODEL
        asr.set_asr_model("")
        assert asr.get_asr_model() == WHISPER_MODEL
    finally:
        asr.set_asr_model(WHISPER_MODEL)


def test_haas_model_routes_to_haas_engine(monkeypatch):
    import sys
    import types

    import numpy as np

    from mimivox.config import HAAS_JA_MODEL

    loads = []
    monkeypatch.setattr(asr, "_load_haas", lambda model_id: loads.append(model_id) or ("be", "tok"))
    seen = []

    def fake_transcribe(backend, tokenizer, audio):
        seen.append((backend, tokenizer))
        return types.SimpleNamespace(text=" こんにちは ")

    monkeypatch.setitem(
        sys.modules, "haas_ja_local", types.SimpleNamespace(transcribe=fake_transcribe)
    )
    monkeypatch.setattr(
        asr.mlx_whisper, "transcribe", lambda *a, **k: pytest.fail("Whisper must not run")
    )
    segments = []
    asr.set_asr_model(HAAS_JA_MODEL)
    try:
        text, seconds, signals = asr._do_asr_streaming(
            np.full(16000, 0.1, dtype=np.float32), segments.append
        )
    finally:
        asr.set_asr_model(asr.WHISPER_MODEL)
    assert (text, signals) == ("こんにちは", None)
    assert seconds >= 0
    assert segments == ["こんにちは"]
    assert loads == [HAAS_JA_MODEL] and seen == [("be", "tok")]


def test_haas_silence_emits_no_segment(monkeypatch):
    import sys
    import types

    import numpy as np

    from mimivox.config import HAAS_JA_MODEL

    monkeypatch.setattr(asr, "_load_haas", lambda model_id: ("be", "tok"))
    monkeypatch.setitem(
        sys.modules,
        "haas_ja_local",
        types.SimpleNamespace(transcribe=lambda *a: types.SimpleNamespace(text="")),
    )
    segments = []
    asr.set_asr_model(HAAS_JA_MODEL)
    try:
        text, _, _ = asr._do_asr_streaming(np.zeros(1600, dtype=np.float32), segments.append)
    finally:
        asr.set_asr_model(asr.WHISPER_MODEL)
    assert text == "" and segments == []


def test_other_model_ids_stay_on_whisper(monkeypatch):
    import numpy as np

    calls = []

    def fake_whisper(audio, **kwargs):
        calls.append(kwargs["path_or_hf_repo"])
        return {"text": " hi ", "segments": []}

    monkeypatch.setattr(asr.mlx_whisper, "transcribe", fake_whisper)
    asr.set_asr_model("mlx-community/whisper-tiny-mlx-q4")
    try:
        text, _, _ = asr._do_asr_streaming(np.full(1600, 0.1, dtype=np.float32), lambda s: None)
    finally:
        asr.set_asr_model(asr.WHISPER_MODEL)
    assert text == "hi" and calls == ["mlx-community/whisper-tiny-mlx-q4"]


def test_preload_haas_uses_the_cached_loader(monkeypatch):
    from mimivox.config import HAAS_JA_MODEL

    loads = []
    monkeypatch.setattr(asr, "_load_haas", lambda model_id: loads.append(model_id))
    asr.preload_asr_model(HAAS_JA_MODEL)
    assert loads == [HAAS_JA_MODEL]
