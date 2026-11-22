"""Glossary kinds, profiles, CSV import, ASR prompt, substitution, session start."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mimivox import asr, pipeline, settings_store
from mimivox import translate as tr
from mimivox.api_adapter import api_state, router
from mimivox.config import GLOSSARY, ASRResult
from mimivox.glossary import build_initial_prompt, is_asr_fix, parse_glossary_text

PROFILE = {
    "name": "Acme",
    "entries": [
        {"source": "部長", "target": "department head"},
        {"source": "ミミボックス", "target": "MimiVox"},
        {"source": "よろしくお願いします", "target": "I look forward to working with you"},
        {"source": "てっこうだ", "target": "結構だ"},
    ],
}


@pytest.fixture(autouse=True)
def _restore():
    yield
    tr.set_active_glossary(dict(GLOSSARY))
    asr.set_asr_prompt("。、")
    api_state.settings = settings_store.default_settings()
    api_state.state = "idle"
    api_state.pipeline_task = None


def test_kind_is_derived_from_target():
    assert is_asr_fix("結構") is True
    assert is_asr_fix("department head") is False
    assert is_asr_fix("MimiVox 社") is True


def test_parse_csv_and_tsv_paste():
    assert parse_glossary_text("部長,department head\n\nミミボックス, MimiVox \nbad\n,x") == [
        {"source": "部長", "target": "department head"},
        {"source": "ミミボックス", "target": "MimiVox"},
    ]
    # A comma inside a TSV target must survive.
    assert parse_glossary_text("部長,department head\n見積もり\tquote, estimate") == [
        {"source": "部長", "target": "department head"},
        {"source": "見積もり", "target": "quote, estimate"},
    ]


def test_initial_prompt_uses_term_sources_only():
    g = settings_store.get_effective_glossary(
        {"glossary_profiles": [PROFILE], "active_glossary_profile": "Acme"}
    )
    assert build_initial_prompt(g) == "。、部長、ミミボックス、よろしくお願いします。"
    assert build_initial_prompt({}) == "。、"
    assert build_initial_prompt(dict(GLOSSARY)) == "。、"  # fixes only


def test_initial_prompt_is_capped():
    g = {f"用語{i:03d}": "term" for i in range(200)}
    assert len(build_initial_prompt(g)) <= 170


def test_refresh_sets_whisper_prompt_and_translation_glossary():
    settings_store.refresh_glossary_from_settings({"glossary_profiles": [PROFILE]}, profile="Acme")
    assert asr.get_asr_prompt().startswith("。、部長")
    assert tr.apply_glossary("部長によろしくお願いします") == (
        "department headにI look forward to working with you"
    )


def test_longest_source_wins():
    assert tr.apply_glossary("お願いします", {"お願いします": "please"}) == "please"
    g = {"お願いします": "please", "よろしくお願いします": "regards"}
    assert tr.apply_glossary("よろしくお願いします", g) == "regards"


def test_profile_selection_precedence():
    s = {"glossary_profiles": [PROFILE], "active_glossary_profile": "Acme"}
    assert "部長" in settings_store.get_effective_glossary(s)
    assert "部長" not in settings_store.get_effective_glossary(s, profile="")
    assert "部長" not in settings_store.get_effective_glossary(s, profile="nope")
    assert "部長" not in settings_store.get_effective_glossary({"glossary_profiles": [PROFILE]})


def test_glossary_file_csv(tmp_path):
    p = tmp_path / "g.csv"
    p.write_text("部長,department head\n", encoding="utf-8")
    assert (
        settings_store.get_effective_glossary({"glossary_path": str(p)})["部長"]
        == "department head"
    )


def test_profiles_persist_with_settings(tmp_path):
    s = settings_store.default_settings()
    s["glossary_profiles"] = [PROFILE]
    s["active_glossary_profile"] = "Acme"
    path = settings_store.save_settings(s, tmp_path / "s.json")
    loaded = settings_store.load_settings(path)
    assert loaded["glossary_profiles"] == [PROFILE]
    assert loaded["active_glossary_profile"] == "Acme"


def test_whisper_receives_active_prompt(monkeypatch):
    seen = {}

    def fake_transcribe(audio, **kw):
        seen.update(kw)
        return {"text": "x"}

    monkeypatch.setattr(asr.mlx_whisper, "transcribe", fake_transcribe)
    asr.set_asr_prompt("。、部長。")
    asr._do_asr_streaming(np.zeros(16000, dtype=np.float32), lambda s: None)
    assert seen["initial_prompt"] == "。、部長。"


def _api_client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _start(monkeypatch, tmp_path, body):
    captured = {}
    # Replay start 400s when the WAV is missing (fresh checkouts have no data/).
    wav = tmp_path / "replay.wav"
    wav.write_bytes(b"")

    async def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("mimivox.api_adapter.run", fake_run)
    api_state.pipeline_task = None
    api_state.state = "idle"
    api_state.settings = settings_store.default_settings()
    api_state.settings["glossary_profiles"] = [PROFILE]
    api_state.settings["replay_audio_path"] = str(wav)
    resp = _api_client().post("/api/session/start", json=body)
    return resp, captured


def test_session_start_applies_requested_profile(monkeypatch, tmp_path):
    resp, _ = _start(monkeypatch, tmp_path, {"source": "replay-file", "glossary_profile": "Acme"})
    assert resp.status_code == 200
    assert tr.apply_glossary("ミミボックス") == "MimiVox"
    assert asr.get_asr_prompt().startswith("。、部長")


def test_session_start_without_profile_uses_none(monkeypatch, tmp_path):
    resp, _ = _start(monkeypatch, tmp_path, {"source": "replay-file"})
    assert resp.status_code == 200
    assert tr.apply_glossary("ミミボックス") == "ミミボックス"
    assert asr.get_asr_prompt() == "。、"


def test_session_start_rejects_unknown_profile(monkeypatch, tmp_path):
    resp, captured = _start(
        monkeypatch, tmp_path, {"source": "replay-file", "glossary_profile": "Nope"}
    )
    assert resp.status_code == 400
    assert captured == {}


async def _translated_inputs(monkeypatch, lines, glossary):
    seen: list[str] = []

    async def fake_translate(self, ja, token_cb, priority="final", context=()):
        seen.append(ja)
        return ("EN", 0.01)

    monkeypatch.setattr(tr.CatTranslator, "translate", fake_translate)
    tr.set_active_glossary(glossary)
    pipeline._TRANSLATION_CACHE.clear()
    q: asyncio.Queue = asyncio.Queue()
    for i, line in enumerate(lines):
        await q.put(
            ASRResult(
                kind="final",
                text=line,
                audio_ts=f"00:{i:02d}",
                wall_time="00:00:00",
                asr_latency=0.1,
            )
        )
    await q.put(None)
    await pipeline.display_worker(q, [], asyncio.get_running_loop(), partial_en_enabled=False)
    return seen


async def test_cat_receives_substituted_text(monkeypatch):
    seen = await _translated_inputs(
        monkeypatch, ["部長に報告しましたか。"], {"部長": "department head"}
    )
    assert seen == ["department headに報告しましたか。"]


async def test_line_that_becomes_mostly_english_still_goes_to_cat(monkeypatch):
    seen = await _translated_inputs(monkeypatch, ["はい、部長。"], {"部長": "department head"})
    assert seen == ["はい、department head。"]


async def test_cache_key_changes_with_glossary(monkeypatch):
    first = await _translated_inputs(monkeypatch, ["部長です。"], {"部長": "department head"})
    second = await _translated_inputs(monkeypatch, ["部長です。"], {})
    assert first == ["department headです。"]
    assert second == ["部長です。"]
