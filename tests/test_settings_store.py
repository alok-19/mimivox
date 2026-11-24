"""Unit tests for settings persistence, glossary overlay, and latency presets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimivox import config
from mimivox.config import GLOSSARY, apply_latency_quality, latency_tuning_snapshot
from mimivox.settings_store import (
    default_settings,
    find_system_audio_input_device,
    get_effective_glossary,
    load_settings,
    merge_settings,
    model_path_status,
    partial_en_enabled,
    refresh_glossary_from_settings,
    resolve_asr_model_live,
    resolve_microphone_device,
    resolve_system_audio_device,
    save_settings,
)
from mimivox.translate import apply_glossary, get_active_glossary, set_active_glossary


@pytest.fixture(autouse=True)
def _restore_glossary_and_latency():
    """Keep global glossary/latency state isolated across tests."""
    set_active_glossary(dict(GLOSSARY))
    apply_latency_quality("fast")
    yield
    set_active_glossary(dict(GLOSSARY))
    apply_latency_quality("fast")


def test_default_settings_shape():
    s = default_settings()
    assert s["privacy_mode"] == "local-only"
    assert s["latency_quality"] == "fast"
    assert s["cloud_provider_enabled"] is False
    assert "whisper" in s["model_paths"]
    assert isinstance(s["glossary_terms"], list)
    # Preview CAT is intentionally off: it competes with final live inference.
    assert s["partial_en_enabled"] is False
    assert s["asr_model_live"] == ""


def test_resolve_asr_model_live_default_and_override():
    assert resolve_asr_model_live({}) is None
    assert resolve_asr_model_live({"asr_model_live": "  "}) is None
    assert (
        resolve_asr_model_live({"asr_model_live": "mlx-community/whisper-tiny-mlx-q4"})
        == "mlx-community/whisper-tiny-mlx-q4"
    )


def test_partial_en_enabled_flag():
    assert partial_en_enabled({}) is True
    assert partial_en_enabled({"partial_en_enabled": True}) is True
    assert partial_en_enabled({"partial_en_enabled": False}) is False
    assert partial_en_enabled({"partial_en_enabled": "false"}) is False
    assert partial_en_enabled({"partial_en_enabled": "0"}) is False
    assert partial_en_enabled({"partial_en_enabled": "yes"}) is True


def test_merge_settings_never_crashes_on_partial():
    base = default_settings()
    merged = merge_settings(base, {"latency_quality": "quality", "unknown_key": 1})
    assert merged["latency_quality"] == "quality"
    assert merged["unknown_key"] == 1
    assert merged["privacy_mode"] == "local-only"


def test_merge_model_paths_partial():
    base = default_settings()
    merged = merge_settings(base, {"model_paths": {"whisper": "custom/whisper"}})
    assert merged["model_paths"]["whisper"] == "custom/whisper"
    assert merged["model_paths"]["cat_translate"] == base["model_paths"]["cat_translate"]


def test_settings_round_trip_file(tmp_path: Path):
    path = tmp_path / "settings.json"
    original = default_settings()
    original["latency_quality"] = "balanced"
    original["glossary_terms"] = [{"source": "foo", "target": "bar"}]
    save_settings(original, path)

    loaded = load_settings(path)
    assert loaded["latency_quality"] == "balanced"
    assert loaded["glossary_terms"] == [{"source": "foo", "target": "bar"}]
    assert loaded["privacy_mode"] == "local-only"


def test_load_settings_missing_file_returns_defaults(tmp_path: Path):
    loaded = load_settings(tmp_path / "missing.json")
    assert loaded == default_settings()


def test_load_settings_corrupt_json_returns_defaults(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    loaded = load_settings(path)
    assert loaded == default_settings()


def test_glossary_overlay_terms():
    settings = default_settings()
    settings["glossary_terms"] = [
        {"source": "てっこうです", "target": "結構です"},  # same as built-in
        {"source": "みみぼっくす", "target": "MimiVox"},
    ]
    g = get_effective_glossary(settings)
    assert g["てっこうです"] == "結構です"
    assert g["みみぼっくす"] == "MimiVox"
    assert "てっこう" in g  # built-in preserved


def test_glossary_overlay_file_dict(tmp_path: Path):
    path = tmp_path / "glossary.json"
    path.write_text(json.dumps({"カスタム": "custom"}), encoding="utf-8")
    settings = default_settings()
    settings["glossary_path"] = str(path)
    g = get_effective_glossary(settings)
    assert g["カスタム"] == "custom"


def test_glossary_overlay_file_list(tmp_path: Path):
    path = tmp_path / "glossary.json"
    path.write_text(
        json.dumps([{"source": "エービー", "target": "AB"}]),
        encoding="utf-8",
    )
    settings = default_settings()
    settings["glossary_path"] = str(path)
    settings["glossary_terms"] = [{"source": "みみ", "target": "Mimi"}]
    g = get_effective_glossary(settings)
    assert g["みみ"] == "Mimi"
    assert g["エービー"] == "AB"


def test_refresh_glossary_affects_apply_glossary():
    settings = default_settings()
    settings["glossary_terms"] = [{"source": "XYZ用語", "target": "XYZ term"}]
    refresh_glossary_from_settings(settings)
    assert "XYZ用語" in get_active_glossary()
    assert apply_glossary("これはXYZ用語です") == "これはXYZ termです"


def test_apply_glossary_optional_map_overrides_active():
    set_active_glossary({"a": "A"})
    assert apply_glossary("a", {"a": "B"}) == "B"
    assert apply_glossary("a") == "A"


def test_latency_mode_changes_known_constants():
    apply_latency_quality("fast")
    fast = latency_tuning_snapshot()
    assert fast["VAD_MIN_SILENCE_MS"] == config.LATENCY_PRESETS["fast"]["VAD_MIN_SILENCE_MS"]

    apply_latency_quality("balanced")
    bal = latency_tuning_snapshot()
    assert bal["VAD_MIN_SILENCE_MS"] == config.LATENCY_PRESETS["balanced"]["VAD_MIN_SILENCE_MS"]
    assert bal["MAX_EMIT_S"] == config.LATENCY_PRESETS["balanced"]["MAX_EMIT_S"]

    apply_latency_quality("quality")
    qual = latency_tuning_snapshot()
    assert qual["VAD_MIN_SILENCE_MS"] == config.LATENCY_PRESETS["quality"]["VAD_MIN_SILENCE_MS"]
    assert qual["MIN_SPEECH_S"] == config.LATENCY_PRESETS["quality"]["MIN_SPEECH_S"]

    # Unknown falls back to fast
    assert apply_latency_quality("nope") == "fast"
    assert (
        latency_tuning_snapshot()["VAD_MIN_SILENCE_MS"]
        == config.LATENCY_PRESETS["fast"]["VAD_MIN_SILENCE_MS"]
    )


def test_resolve_microphone_device(monkeypatch):
    monkeypatch.setattr(
        "mimivox.settings_store.list_input_devices",
        lambda: [(3, "Built-in"), (4, "USB Mic")],
    )
    assert resolve_microphone_device({"microphone_device": ""}) is None
    assert resolve_microphone_device({"microphone_device": "3"}) == 3
    assert resolve_microphone_device({"microphone_device": "Built-in"}) == "Built-in"
    # Stale index is not a current input → default device.
    assert resolve_microphone_device({"microphone_device": "0"}) is None


def test_resolve_system_audio_device_explicit(monkeypatch):
    monkeypatch.setattr(
        "mimivox.settings_store.list_input_devices",
        lambda: [(2, "BlackHole 2ch"), (5, "Loopback Audio")],
    )
    assert resolve_system_audio_device({"system_audio_device": "5"}) == 5
    assert resolve_system_audio_device({"system_audio_device": "BlackHole 2ch"}) == "BlackHole 2ch"
    assert resolve_system_audio_device({"system_audio_device": 2}) == 2


def test_resolve_system_audio_auto_pick(monkeypatch):
    monkeypatch.setattr(
        "mimivox.settings_store.list_input_devices",
        lambda: [
            (0, "MacBook Pro Microphone"),
            (2, "BlackHole 2ch"),
            (4, "External Mic"),
        ],
    )
    assert resolve_system_audio_device({"system_audio_device": ""}) == 2
    assert find_system_audio_input_device() == 2


def test_resolve_system_audio_auto_pick_loopback(monkeypatch):
    monkeypatch.setattr(
        "mimivox.settings_store.list_input_devices",
        lambda: [(1, "Loopback Audio"), (3, "Built-in")],
    )
    assert resolve_system_audio_device({}) == 1


def test_resolve_system_audio_auto_pick_multi_output(monkeypatch):
    monkeypatch.setattr(
        "mimivox.settings_store.list_input_devices",
        lambda: [(0, "Multi-Output Device"), (1, "Mic")],
    )
    assert resolve_system_audio_device({"system_audio_device": None}) == 0


def test_resolve_system_audio_missing(monkeypatch):
    monkeypatch.setattr(
        "mimivox.settings_store.list_input_devices",
        lambda: [(0, "MacBook Pro Microphone"), (1, "USB Mic")],
    )
    assert resolve_system_audio_device({"system_audio_device": ""}) is None
    assert find_system_audio_input_device() is None


def test_resolve_system_audio_stale_index_falls_back_to_auto(monkeypatch):
    """Output-only / removed device ids must not win over BlackHole auto-pick."""
    monkeypatch.setattr(
        "mimivox.settings_store.list_input_devices",
        lambda: [(1, "BlackHole 2ch"), (2, "MacBook Air Microphone")],
    )
    assert resolve_system_audio_device({"system_audio_device": "0"}) == 1
    assert resolve_system_audio_device({"system_audio_device": "9"}) == 1


def test_resolve_system_audio_explicit_wins_over_auto(monkeypatch):
    monkeypatch.setattr(
        "mimivox.settings_store.list_input_devices",
        lambda: [(2, "BlackHole 2ch"), (9, "Loopback Audio")],
    )
    assert resolve_system_audio_device({"system_audio_device": "9"}) == 9


def test_resolve_replay_duration_s():
    from mimivox.settings_store import resolve_replay_duration_s

    assert resolve_replay_duration_s({"replay_duration_seconds": 20}) == 20.0
    assert resolve_replay_duration_s({"replay_duration_seconds": "10"}) == 10.0
    assert resolve_replay_duration_s({"replay_duration_seconds": 0}) is None
    assert resolve_replay_duration_s({"replay_duration_seconds": -1}) is None
    assert resolve_replay_duration_s({"replay_duration_seconds": ""}) is None
    assert resolve_replay_duration_s({}) is None


def test_model_path_status_mismatch():
    settings = default_settings()
    settings["model_paths"] = {
        "whisper": "some/other/whisper",
        "cat_translate": settings["model_paths"]["cat_translate"],
    }
    status = model_path_status(settings)
    assert status["matches_loaded"] is False
    assert status["whisper"]["matches_loaded"] is False
    assert status["cat_translate"]["matches_loaded"] is True
    assert any("Restart" in s for s in status["next_steps"])
