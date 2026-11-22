"""Language pairs: data shape, settings, Whisper wiring, routing, non-JA translation."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox import asr, notes_llm, pair_translate, pipeline
from mimivox.api_adapter import api_state, router
from mimivox.config import ASRResult
from mimivox.glossary import build_initial_prompt
from mimivox.languages import (
    DEFAULT_PAIR,
    LanguagePair,
    UnsupportedPair,
    pair_from_settings,
    parse_pair,
    resolve_pair,
)
from mimivox.settings_store import apply_settings_for_session, default_settings

ZH, KO, EN_JA = LanguagePair("zh", "en"), LanguagePair("ko", "en"), LanguagePair("en", "ja")


# -- data shape ---------------------------------------------------------------
def test_supported_pairs_and_defaults():
    assert DEFAULT_PAIR.code == "ja-en" and DEFAULT_PAIR.uses_cat
    assert [ZH.uses_cat, KO.uses_cat, EN_JA.uses_cat] == [False, False, False]
    assert EN_JA.whisper_language == "en" and ZH.whisper_language == "zh"
    assert default_settings()["source_lang"] == "ja" and default_settings()["target_lang"] == "en"


def test_unsupported_pair_rejected():
    with pytest.raises(UnsupportedPair):
        parse_pair("fr", "en")
    with pytest.raises(UnsupportedPair):
        parse_pair("en", "zh")


def test_settings_fall_back_to_ja_en_on_garbage():
    assert pair_from_settings({"source_lang": "xx", "target_lang": "en"}) == DEFAULT_PAIR
    assert pair_from_settings({}) == DEFAULT_PAIR
    assert pair_from_settings({"source_lang": "ko", "target_lang": "en"}) == KO


def test_request_overrides_settings():
    assert resolve_pair({"source_lang": "ko", "target_lang": "en"}, {"source_lang": "zh"}) == ZH
    assert resolve_pair({"source_lang": "ko", "target_lang": "en"}, {}) == KO
    with pytest.raises(UnsupportedPair):
        resolve_pair({}, {"source_lang": "fr"})


# -- Whisper wiring -----------------------------------------------------------
def test_session_settings_set_whisper_language_and_prompt():
    s = {**default_settings(), "source_lang": "zh", "target_lang": "en"}
    apply_settings_for_session(s)
    assert asr.get_asr_language() == "zh" and asr.get_asr_prompt() == ""
    apply_settings_for_session(default_settings())
    assert asr.get_asr_language() == "ja" and asr.get_asr_prompt() == "。、"


def test_initial_prompt_per_language_keeps_ja_unchanged():
    g = {"部長": "department head", "OKR": "OKR"}
    assert build_initial_prompt(g) == build_initial_prompt(g, DEFAULT_PAIR) == "。、部長、OKR。"
    assert build_initial_prompt({}, KO) == ""
    assert build_initial_prompt({"예산": "budget"}, KO) == "예산."


# -- pair helpers ---------------------------------------------------------------
def test_normalize_uses_source_terminal():
    assert pair_translate.normalize_for(DEFAULT_PAIR, "はい") == "はい。"
    assert pair_translate.normalize_for(ZH, "你好") == "你好。"
    assert pair_translate.normalize_for(KO, "안녕하세요") == "안녕하세요."
    assert pair_translate.normalize_for(EN_JA, "Hello there?") == "Hello there?"


def test_ja_only_heuristics_do_not_apply_elsewhere():
    assert pair_translate.is_passthrough(DEFAULT_PAIR, "Let's start the meeting now")
    assert not pair_translate.is_passthrough(EN_JA, "Let's start the meeting now")
    assert [pair_translate.supports_revision(p) for p in (DEFAULT_PAIR, ZH, KO, EN_JA)] == [
        True,
        False,
        False,
        False,
    ]
    assert [pair_translate.supports_preview(p) for p in (DEFAULT_PAIR, ZH, EN_JA)] == [
        True,
        False,
        False,
    ]


def test_english_source_context_is_kept():
    ctx = pair_translate.context_for(EN_JA)
    ctx.add("Let's begin.", "始めましょう。")
    assert ctx.pairs() == (("Let's begin.", "始めましょう。"),)
    ja = pair_translate.context_for(DEFAULT_PAIR)
    ja.add("Okay let's begin now", "Okay let's begin now")  # JA code-switch passthrough
    assert ja.pairs() == ()


def test_messages_carry_languages_context_and_style():
    msgs = pair_translate.build_messages(EN_JA, "Send it.", [("Hi.", "こんにちは。")])
    assert "English" in msgs[0]["content"] and "Japanese" in msgs[0]["content"]
    assert "です/ます" in msgs[0]["content"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert "です/ます" not in pair_translate.build_messages(ZH, "你好", [])[0]["content"]


def test_clean_output_strips_label():
    assert pair_translate.clean_output("Translation: Hello.\n") == "Hello."


# -- LlmTranslator --------------------------------------------------------------
def _stream(chunks):
    def fake(messages, *, max_tokens):
        yield from chunks

    return fake


async def test_llm_translator_streams_and_scores(monkeypatch):
    monkeypatch.setattr(notes_llm, "chat_stream", _stream([("Hel", -0.1), ("lo.", -0.3)]))
    seen: list[str] = []
    out, secs, sig = await pair_translate.LlmTranslator(ZH).translate("你好。", seen.append)
    assert out == "Hello." and seen == ["Hel", "lo."]
    assert sig.mean_logprob == pytest.approx(-0.2)


async def test_llm_translator_retries_without_context_on_echo(monkeypatch):
    calls: list[int] = []

    def fake(messages, *, max_tokens):
        calls.append(len(messages))
        yield (
            "I will prepare the document now." if len(messages) > 2 else "See you tomorrow.",
            -0.1,
        )

    monkeypatch.setattr(notes_llm, "chat_stream", fake)
    ctx = (("准备文件。", "I will prepare the document now."),)
    out, _, _ = await pair_translate.LlmTranslator(ZH).translate(
        "明天见。", lambda _t: None, context=ctx
    )
    assert out == "See you tomorrow." and calls == [4, 2]


async def test_llm_translator_has_no_preview():
    assert (
        await pair_translate.LlmTranslator(ZH).translate(
            "你好。", lambda _t: None, priority="preview"
        )
    )[0] == ""


def test_pinned_model_survives_idle_unload(monkeypatch):
    monkeypatch.setattr(notes_llm, "_model", object())
    monkeypatch.setattr(notes_llm, "load", lambda *a, **k: None)
    t = pair_translate.LlmTranslator(ZH)
    t.load()
    assert notes_llm.unload() is False and notes_llm.is_loaded()
    t.close()
    assert notes_llm.unload() is True


def test_translator_for_routes_by_pair():
    from mimivox.translate import CatTranslator

    assert isinstance(pair_translate.translator_for(DEFAULT_PAIR), CatTranslator)
    assert isinstance(pair_translate.translator_for(KO), pair_translate.LlmTranslator)
    assert pair_translate.translator_for(KO) is pair_translate.translator_for(KO)


def test_unready_reason_when_model_missing(monkeypatch):
    monkeypatch.setattr("mimivox.models_service.model_present", lambda _id: (False, "x", None))
    assert "Settings" in pair_translate.unready_reason(ZH)
    assert pair_translate.unready_reason(DEFAULT_PAIR) is None
    monkeypatch.setattr("mimivox.models_service.model_present", lambda _id: (True, "", "/p"))
    assert pair_translate.unready_reason(ZH) is None


# -- display_worker with a non-JA pair ------------------------------------------
class _Stub:
    def __init__(self):
        self.seen: list[tuple[str, tuple]] = []

    async def translate(self, text, token_cb, *, priority="final", context=()):
        self.seen.append((text, tuple(context)))
        return (f"T<{text}>", 0.01, pair_translate.MtSignals(mean_logprob=-0.1))


async def _run(monkeypatch, pair, texts):
    stub = _Stub()
    monkeypatch.setattr(pair_translate, "translator_for", lambda _p: stub)
    pipeline.reset_translation_cache()
    q: asyncio.Queue = asyncio.Queue()
    for i, t in enumerate(texts):
        q.put_nowait(
            ASRResult(
                kind="final",
                text=t,
                audio_ts="00:00",
                wall_time="00:00:00",
                asr_latency=0.1,
                duration_s=2.0,
                timeline_s=i * 10.0,
            )
        )
    q.put_nowait(None)
    events: list[dict] = []
    await pipeline.display_worker(
        q,
        [],
        asyncio.get_running_loop(),
        sse_broadcast=events.append,
        partial_en_enabled=True,
        pair=pair,
    )
    return stub, events


async def test_english_source_translates_with_context_and_no_preview(monkeypatch):
    stub, events = await _run(monkeypatch, EN_JA, ["Let's begin", "Send me the report"])
    assert [t for t, _ in stub.seen] == ["Let's begin.", "Send me the report."]
    assert stub.seen[1][1] == (("Let's begin.", "T<Let's begin.>"),)
    assert not any(e["kind"].startswith("partial_en") for e in events)
    assert [e["en"] for e in events if e["kind"] == "final"] == [
        "T<Let's begin.>",
        "T<Send me the report.>",
    ]


async def test_split_sentence_is_not_revised_outside_ja(monkeypatch):
    # A trailing comma would merge for ja-en; for zh it stays two captions.
    _, events = await _run(monkeypatch, ZH, ["我们先看预算，", "然后讨论时间表。"])
    assert [e["kind"] for e in events if e["kind"] in ("final", "revision")] == ["final", "final"]


async def test_cache_is_cleared_when_pair_changes(monkeypatch):
    pipeline._TRANSLATION_CACHE["大丈夫。"] = "stale"
    monkeypatch.setattr(pipeline, "_cache_pair", DEFAULT_PAIR)

    async def fake_feed(*a, **k):
        return None

    async def fake_asr(asr_q, display_q, *a, **k):
        await display_q.put(None)

    async def fake_display(*a, **k):
        return None

    monkeypatch.setattr(pipeline, "feed_wav", fake_feed)
    monkeypatch.setattr(pipeline, "asr_worker", fake_asr)
    monkeypatch.setattr(pipeline, "display_worker", fake_display)
    monkeypatch.setattr(pair_translate.LlmTranslator, "load", lambda self: None)
    await pipeline.run("x.wav", live=False, asr_only=False, save_log=False, pair=ZH)
    assert "大丈夫。" not in pipeline._TRANSLATION_CACHE
    assert asr.get_asr_language() == "zh"
    asr.set_asr_language("ja")


# -- API ------------------------------------------------------------------------
def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _reset():
    api_state.pipeline_task = None
    api_state.state = "idle"
    api_state.control = None
    api_state.session_id = None
    api_state.events = []


def test_session_start_rejects_unsupported_pair():
    _reset()
    api_state.settings = default_settings()
    r = _client().post("/api/session/start", json={"source": "replay-file", "source_lang": "fr"})
    assert r.status_code == 400 and "fr->en" in r.json()["message"]


def test_session_start_blocks_until_translation_model_downloaded(monkeypatch):
    _reset()
    api_state.settings = default_settings()
    monkeypatch.setattr("mimivox.models_service.model_present", lambda _id: (False, "", None))
    r = _client().post("/api/session/start", json={"source": "replay-file", "source_lang": "ko"})
    assert r.status_code == 400 and "Download" in r.json()["message"]


def test_session_start_passes_pair_and_announces_it(monkeypatch, tmp_path):
    _reset()
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    api_state.settings = {**default_settings(), "replay_audio_path": str(wav)}
    monkeypatch.setattr("mimivox.models_service.model_present", lambda _id: (True, "", "/p"))
    got: dict = {}

    async def fake_run(**kw):
        got.update(kw)

    monkeypatch.setattr("mimivox.api_adapter.run", fake_run)
    r = _client().post(
        "/api/session/start",
        json={"source": "replay-file", "source_lang": "en", "target_lang": "ja"},
    )
    assert r.status_code == 200 and got["pair"] == EN_JA
    assert asr.get_asr_language() == "en"
    assert api_state.session_pair == EN_JA
    _reset()
    asr.set_asr_language("ja")


def test_languages_endpoint_lists_pairs_with_readiness(monkeypatch):
    api_state.settings = default_settings()
    monkeypatch.setattr("mimivox.models_service.model_present", lambda _id: (False, "", None))
    body = _client().get("/api/languages").json()
    ready = {p["code"]: p["ready"] for p in body["pairs"]}
    assert ready == {"ja-en": True, "zh-en": False, "ko-en": False, "en-ja": False}
    assert body["selected"] == {"source": "ja", "target": "en"}
