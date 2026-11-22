import asyncio

from mimivox import config


def test_translation_cache_seeded():
    assert "ありがとう。" in config._TRANSLATION_CACHE
    assert config._TRANSLATION_CACHE["ありがとう。"] == "Thank you."


def test_glossary_entries():
    assert "てっこう" in config.GLOSSARY


def test_data_dir_constant():
    assert config.DATA_DIR == "data"


def test_latency_presets_include_fast_balanced_quality():
    assert set(config.LATENCY_PRESETS) >= {"fast", "balanced", "quality"}
    # fast matches the historical production defaults snapshot keys
    for key in ("MIN_SPEECH_S", "MAX_CHUNK_S", "VAD_THRESHOLD", "VAD_MIN_SILENCE_MS", "MAX_EMIT_S"):
        assert key in config.LATENCY_PRESETS["fast"]


def test_live_asr_defaults_to_file_turbo_q8():
    assert config.ASR_MODEL_LIVE == config.ASR_MODEL_FILE == config.WHISPER_MODEL
    assert "turbo" in config.ASR_MODEL_LIVE.lower() or "large-v3" in config.ASR_MODEL_LIVE


def test_asr_queue_policy_constants():
    assert config.ASR_Q_MAXSIZE >= 2
    assert 1 <= config.ASR_Q_LIVE_KEEP <= config.ASR_Q_MAXSIZE
    assert 0.3 <= config.ASR_Q_STALE_WAIT_S <= 2.0


def test_live_overrides_cap_emit_and_silence():
    config.apply_latency_quality("balanced")
    assert config.LATENCY_PRESETS["balanced"]["MAX_EMIT_S"] == config.MAX_EMIT_S
    config.apply_live_vad_overrides()
    assert config.VAD_MIN_SILENCE_MS == config.LIVE_VAD_MIN_SILENCE_MS
    assert config.VAD_SPEECH_PAD_MS == config.LIVE_VAD_SPEECH_PAD_MS
    assert config.MAX_EMIT_S == config.LIVE_MAX_EMIT_S
    # Whisper has substantial fixed per-call overhead. Keep the emergency cap
    # clause-sized so continuous speech does not become many tiny decodes.
    assert 12.0 <= config.LIVE_MAX_EMIT_S <= 18.0
    # Restore file-path defaults for other tests.
    config.apply_latency_quality("fast")


def test_data_dir_follows_env_across_modules(tmp_path):
    """MIMIVOX_DATA_DIR relocates every data path; unset keeps cwd-relative 'data'."""
    import subprocess
    import sys

    code = (
        "from mimivox import config, session_archive, settings_store, models_service, export_session\n"
        "from pathlib import Path\n"
        "print(config.DATA_DIR)\n"
        "print(session_archive.sessions_dir())\n"
        "print(settings_store._SETTINGS_PATH)\n"
        "print(settings_store._DEFAULT_REPLAY)\n"
        "print(models_service.MIMIVOX_MODELS_DIR)\n"
        "print(export_session.write_export.__defaults__ or export_session.write_export.__kwdefaults__)\n"
    )
    import os

    env = {**os.environ, "PYTHONPATH": "src"}
    env.pop("MIMIVOX_DATA_DIR", None)
    default = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert default[0] == "data"
    assert default[1] == "data/sessions"

    env["MIMIVOX_DATA_DIR"] = str(tmp_path)
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert out[0] == str(tmp_path)
    assert out[1] == str(tmp_path / "sessions")
    assert out[2] == str(tmp_path / "settings.json")
    assert out[3] == str(tmp_path / "fixtures" / "meeting-recording.wav")
    assert out[4] == str(tmp_path / "models")
    assert str(tmp_path / "transcripts") in out[5]


async def test_shared_mlx_lock_gives_asr_and_cat_one_lock(monkeypatch):
    from mimivox import config

    monkeypatch.setattr(config._models, "asr_lock", None)
    monkeypatch.setattr(config._models, "cat_lock", None)

    lock = config.shared_mlx_lock()

    assert config._models.asr_lock is lock
    assert config._models.cat_lock is lock


def test_shared_mlx_lock_is_per_event_loop(monkeypatch):
    """Each CLI run has its own loop; a lock from a finished loop must not be reused."""
    from mimivox import config

    monkeypatch.setattr(config._models, "asr_lock", None)
    monkeypatch.setattr(config._models, "cat_lock", None)

    async def get_twice():
        return config.shared_mlx_lock(), config.shared_mlx_lock()

    first_a, first_b = asyncio.run(get_twice())
    second, _ = asyncio.run(get_twice())

    assert first_a is first_b
    assert second is not first_a
