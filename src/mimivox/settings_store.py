"""mimivox.settings_store — Persist and apply MeetingSettings.

Path: data/settings.json (under the gitignored data/ tree).
Schema matches Swift MeetingSettings snake_case keys.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from mimivox.config import (
    ASR_MODEL_FILE,
    ASR_MODEL_LIVE,
    CAT_MODEL_ID,
    DATA_DIR,
    GLOSSARY,
    HAAS_MODEL,
    WHISPER_MODEL,
    apply_latency_quality,
)
from mimivox.glossary import build_initial_prompt, entries_to_map, find_profile, parse_glossary_text
from mimivox.languages import LanguagePair, pair_from_settings

_DEFAULT_REPLAY = str(Path(DATA_DIR) / "fixtures" / "meeting-recording.wav")
_SETTINGS_PATH = Path(DATA_DIR) / "settings.json"
_io_lock = threading.Lock()


def default_settings() -> dict[str, Any]:
    return {
        "microphone_device": "",
        "system_audio_device": "",
        "glossary_path": "",
        "glossary_terms": [
            {"source": wrong, "target": correct} for wrong, correct in GLOSSARY.items()
        ],
        # Named per-meeting glossaries: [{name, entries: [{source, target}]}].
        "glossary_profiles": [],
        "active_glossary_profile": "",
        # Language pair (see mimivox.languages): what Whisper hears -> caption language.
        "source_lang": "ja",
        "target_lang": "en",
        "privacy_mode": "local-only",
        # forever | 30d | 7d | 1d | none (see mimivox.privacy)
        "retention_policy": "forever",
        "latency_quality": "fast",
        "catch_up_mode": "manual",
        "cloud_provider_enabled": False,
        "cloud_provider_consent": False,
        "replay_audio_path": _DEFAULT_REPLAY,
        "replay_duration_seconds": 20,
        # "haas" (Japanese source only; other languages fall back) or "whisper".
        "asr_engine": "haas",
        # Whisper only. Empty -> default ASR_MODEL_LIVE (turbo-q8). Set to a model id
        # (e.g. "mlx-community/whisper-medium-mlx") to override the live path.
        "asr_model_live": "",
        # Stream a partial EN preview during ASR so EN appears before the final.
        # Off by default: A/B on meeting_merged showed concurrent preview CAT
        # contends for GPU + holds cat_lock so final CAT waits (perc p50 +1.5s).
        # Re-enable for perceived-preview experiments once preview cannot block final.
        "partial_en_enabled": False,
        # Furigana, word glosses and a vocabulary list for people studying
        # Japanese (see mimivox.learning). Off: captions carry no extra work.
        "learning_mode": False,
        "model_paths": {
            "whisper": WHISPER_MODEL,
            "cat_translate": CAT_MODEL_ID,
        },
    }


def merge_settings(base: dict[str, Any], overlay: dict[str, Any] | None) -> dict[str, Any]:
    """Deep-merge overlay onto a copy of base. Never crash on missing keys."""
    out = dict(base)
    if not isinstance(overlay, dict):
        return out
    for key, value in overlay.items():
        if key in ("message", "status", "detail"):
            continue
        if (
            key == "model_paths"
            and isinstance(value, dict)
            and isinstance(out.get("model_paths"), dict)
        ):
            merged = dict(out["model_paths"])
            merged.update({k: v for k, v in value.items() if v is not None})
            out["model_paths"] = merged
        else:
            out[key] = value
    return out


def load_settings(path: Path | str | None = None) -> dict[str, Any]:
    """Load settings from disk and merge with defaults."""
    target = Path(path) if path is not None else _SETTINGS_PATH
    base = default_settings()
    if not target.exists():
        return base
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(raw, dict):
        return base
    return merge_settings(base, raw)


def save_settings(settings: dict[str, Any], path: Path | str | None = None) -> Path:
    """Atomically write settings JSON. Returns the path written."""
    target = Path(path) if path is not None else _SETTINGS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = merge_settings(default_settings(), settings)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_suffix(target.suffix + ".tmp")
    with _io_lock:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
    return target


def _load_glossary_file(path: str) -> dict[str, str]:
    """Load glossary JSON ({src: tgt} or [{source, target}, ...]) or CSV/TSV."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    if p.suffix.lower() in (".csv", ".tsv", ".txt"):
        return entries_to_map(parse_glossary_text(text))
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k is None:
                continue
            out[str(k)] = "" if v is None else str(v)
        return out
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            src = item.get("source")
            if src is None or src == "":
                continue
            tgt = item.get("target")
            out[str(src)] = "" if tgt is None else str(tgt)
    return out


def get_effective_glossary(
    settings: dict[str, Any] | None = None, profile: str | None = None
) -> dict[str, str]:
    """Built-in GLOSSARY ← glossary_terms ← profile ← glossary_path file.

    ``profile`` overrides settings["active_glossary_profile"] (None = use it,
    "" = no profile).
    """
    settings = settings if settings is not None else default_settings()
    effective = dict(GLOSSARY)

    terms = settings.get("glossary_terms") or []
    if isinstance(terms, dict):
        for src, tgt in terms.items():
            if src is not None and str(src):
                effective[str(src)] = "" if tgt is None else str(tgt)
    elif isinstance(terms, list):
        for item in terms:
            if not isinstance(item, dict):
                continue
            src = item.get("source")
            if src is None or src == "":
                continue
            tgt = item.get("target")
            effective[str(src)] = "" if tgt is None else str(tgt)

    name = settings.get("active_glossary_profile") if profile is None else profile
    chosen = find_profile(settings, name)
    if chosen:
        effective.update(entries_to_map(chosen.get("entries")))

    path = settings.get("glossary_path") or ""
    if isinstance(path, str) and path.strip():
        effective.update(_load_glossary_file(path.strip()))

    return effective


def refresh_glossary_from_settings(
    settings: dict[str, Any] | None = None, profile: str | None = None
) -> dict[str, str]:
    """Atomically replace the glossary used by apply_glossary() and Whisper's prompt."""
    from mimivox.asr import set_asr_prompt
    from mimivox.translate import set_active_glossary

    effective = get_effective_glossary(settings, profile)
    set_active_glossary(effective)
    set_asr_prompt(build_initial_prompt(effective, pair_from_settings(settings)))
    return effective


def apply_settings_for_session(
    settings: dict[str, Any], glossary_profile: str | None = None
) -> None:
    """Apply session-start-only knobs (latency VAD/chunk presets + glossary)."""
    mode = settings.get("latency_quality") or "fast"
    if not isinstance(mode, str):
        mode = "fast"
    apply_latency_quality(mode)
    refresh_glossary_from_settings(settings, glossary_profile)
    from mimivox.asr import set_asr_language

    set_asr_language(pair_from_settings(settings).whisper_language)


ASR_ENGINES = ("haas", "whisper")


def asr_engine(settings: dict[str, Any]) -> str:
    raw = settings.get("asr_engine")
    return raw if raw in ASR_ENGINES else "haas"


def resolve_asr_model(settings: dict[str, Any], pair: LanguagePair, *, live: bool) -> str:
    """Model id for a session: Haas for Japanese unless Whisper is chosen.

    Whisper sessions use asr_model_live (live only) or the default model.
    """
    if asr_engine(settings) == "haas" and pair.source == "ja":
        return HAAS_MODEL
    if not live:
        return ASR_MODEL_FILE
    raw = settings.get("asr_model_live")
    if not isinstance(raw, str) or not raw.strip():
        return ASR_MODEL_LIVE
    return raw.strip()


CATCH_UP_MODES = ("off", "manual", "auto")


def catch_up_mode(settings: dict[str, Any]) -> str:
    raw = settings.get("catch_up_mode")
    return raw if raw in CATCH_UP_MODES else "manual"


def partial_en_enabled(settings: dict[str, Any]) -> bool:
    """Whether to stream a partial EN preview during ASR (default True)."""
    raw = settings.get("partial_en_enabled")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "off", "no")
    return True


_SYSTEM_AUDIO_NAME_HINTS = ("blackhole", "loopback", "multi-output")

SYSTEM_AUDIO_MISSING_MESSAGE = (
    "System audio device not found. Install BlackHole (or Loopback), "
    "route system output through it, and select it in Settings → System Audio."
)


def _parse_device_setting(raw: Any) -> int | str | None:
    """Empty → None; numeric string → int; else pass through name."""
    if raw == "" or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return str(raw)


def list_input_devices() -> list[tuple[int, str]]:
    """Return (index, name) for sounddevice inputs. Empty if sounddevice missing."""
    try:
        import sounddevice as sd
    except ImportError:
        return []
    devices: list[tuple[int, str]] = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) <= 0:
            continue
        name = d.get("name") or f"Device {i}"
        devices.append((i, str(name)))
    return devices


def is_usable_input_device(device: int | str | None) -> bool:
    """True when device is a current PortAudio input (by index or name)."""
    if device is None:
        return False
    inputs = list_input_devices()
    if not inputs:
        # sounddevice missing / no probe — keep explicit value and let open fail loud.
        return True
    if isinstance(device, int):
        return any(index == device for index, _ in inputs)
    needle = str(device).strip().lower()
    if not needle:
        return False
    return any(needle == name.lower() or needle in name.lower() for _, name in inputs)


def resolve_microphone_device(settings: dict[str, Any]) -> int | str | None:
    """Empty → default device; stale/invalid index → default; else explicit."""
    explicit = _parse_device_setting(settings.get("microphone_device") or "")
    if explicit is None:
        return None
    if is_usable_input_device(explicit):
        return explicit
    return None


def find_system_audio_input_device() -> int | None:
    """First input whose name matches BlackHole / Loopback / Multi-Output."""
    for index, name in list_input_devices():
        lowered = name.lower()
        if any(hint in lowered for hint in _SYSTEM_AUDIO_NAME_HINTS):
            return index
    return None


def resolve_system_audio_device(settings: dict[str, Any]) -> int | str | None:
    """Usable settings device if set; else auto-pick virtual loopback; else None.

    Stale indexes (e.g. an old output-only device id) fall back to auto-detect
    so Live System Audio does not open, fail, and end the session immediately.
    """
    explicit = _parse_device_setting(settings.get("system_audio_device") or "")
    if explicit is not None and is_usable_input_device(explicit):
        return explicit
    return find_system_audio_input_device()


def resolve_replay_duration_s(settings: dict[str, Any]) -> float | None:
    """Return max replay duration in seconds, or None to play the full file.

    Values <= 0 mean unlimited (full file).
    """
    raw = settings.get("replay_duration_seconds")
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def model_path_status(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Report whether settings model paths match models loaded at startup.

    Hot-reload is intentionally not supported (v1 safe path).
    """
    settings = settings if settings is not None else default_settings()
    paths = settings.get("model_paths") or {}
    if not isinstance(paths, dict):
        paths = {}
    whisper = str(paths.get("whisper") or WHISPER_MODEL)
    cat = str(paths.get("cat_translate") or CAT_MODEL_ID)
    whisper_match = whisper == WHISPER_MODEL
    cat_match = cat == CAT_MODEL_ID
    next_steps: list[str] = []
    if not whisper_match or not cat_match:
        next_steps.append("Restart server to apply model path changes")
    return {
        "whisper": {
            "settings_path": whisper,
            "loaded_path": WHISPER_MODEL,
            "matches_loaded": whisper_match,
        },
        "cat_translate": {
            "settings_path": cat,
            "loaded_path": CAT_MODEL_ID,
            "matches_loaded": cat_match,
        },
        "matches_loaded": whisper_match and cat_match,
        "next_steps": next_steps,
    }


def cloud_status_message(settings: dict[str, Any] | None = None) -> str | None:
    """MimiVox is local-only; surface a note if cloud toggles are enabled."""
    settings = settings if settings is not None else default_settings()
    if settings.get("cloud_provider_enabled"):
        return "Cloud disabled in MimiVox; running local-only"
    return None
