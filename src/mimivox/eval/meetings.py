"""Synthesize ground-truth "meetings" from FLEURS clips."""

from __future__ import annotations

import json
import random
import wave
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from mimivox.eval.fleurs import Clip

SAMPLE_RATE = 16_000
GAP_RANGE_S = (0.6, 1.5)
TARGET_RANGE_S = (115.0, 165.0)
LEVEL_DBFS = -23.0
SPEAKER_BY_GENDER = {"MALE": "A", "FEMALE": "B"}


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16 kHz mono int16")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def normalize_rms(pcm: np.ndarray, target_dbfs: float = LEVEL_DBFS) -> np.ndarray:
    """Scale int16 PCM to a target RMS level, never clipping.

    FLEURS levels vary by 40 dB between speakers (the ja_jp dev female clips sit
    near -60 dBFS). Levelling keeps the eval about speech quality rather than
    recording gain; --raw-levels measures the quiet-speaker path instead.
    """
    x = pcm.astype(np.float64)
    rms = float(np.sqrt((x**2).mean())) if len(x) else 0.0
    if rms == 0.0:
        return pcm
    gain = 32768 * 10 ** (target_dbfs / 20) / rms
    peak = float(np.abs(x).max())
    gain = min(gain, 32767 / peak)
    return np.clip(np.round(x * gain), -32768, 32767).astype(np.int16)


def plan_meeting(
    clips: Sequence[Clip],
    rng: random.Random,
    use_counts: dict[Path, int] | None = None,
    target_s: tuple[float, float] = TARGET_RANGE_S,
) -> list[Clip]:
    """Alternate male/female clips until the target duration is reached.

    Within a meeting every sentence id is distinct. Across meetings, clips that
    were used least so far are preferred (the female pool is small).
    """
    use_counts = use_counts if use_counts is not None else {}
    pools = {g: [c for c in clips if c.gender == g] for g in SPEAKER_BY_GENDER}
    if not all(pools.values()):
        raise ValueError("need at least one male and one female clip")
    target = rng.uniform(*target_s)
    plan: list[Clip] = []
    seen_sids: set[str] = set()
    total, turn = 0.0, 0
    while total < target:
        gender = "MALE" if turn % 2 == 0 else "FEMALE"
        options = [c for c in pools[gender] if c.sid not in seen_sids]
        if not options:
            break
        least = min(use_counts.get(c.wav, 0) for c in options)
        clip = rng.choice([c for c in options if use_counts.get(c.wav, 0) == least])
        plan.append(clip)
        seen_sids.add(clip.sid)
        use_counts[clip.wav] = use_counts.get(clip.wav, 0) + 1
        total += clip.duration_s + GAP_RANGE_S[1] / 2
        turn += 1
    return plan


def render_meeting(
    plan: Sequence[Clip], rng: random.Random, level_dbfs: float | None = LEVEL_DBFS
) -> tuple[np.ndarray, dict]:
    """Concatenate clips with random gaps; return PCM and the ground-truth sidecar.

    Each clip is levelled to ``level_dbfs`` RMS (None keeps the raw levels).
    """
    parts: list[np.ndarray] = []
    lines: list[dict] = []
    pos = 0

    def gap() -> np.ndarray:
        return np.zeros(int(rng.uniform(*GAP_RANGE_S) * SAMPLE_RATE), dtype=np.int16)

    lead = gap()
    parts.append(lead)
    pos += len(lead)
    for clip in plan:
        pcm = read_wav(clip.wav)
        if level_dbfs is not None:
            pcm = normalize_rms(pcm, level_dbfs)
        parts.append(pcm)
        lines.append(
            {
                "speaker": SPEAKER_BY_GENDER[clip.gender],
                "ja": clip.ja_raw,
                "en": clip.en,
                "start_s": round(pos / SAMPLE_RATE, 2),
                "end_s": round((pos + len(pcm)) / SAMPLE_RATE, 2),
                "sid": clip.sid,
                "gender": clip.gender,
                "clip": clip.wav.name,
            }
        )
        pos += len(pcm)
        g = gap()
        parts.append(g)
        pos += len(g)
    return np.concatenate(parts), {
        "source": "fleurs-ja_jp-dev",
        "level_dbfs": level_dbfs,
        "lines": lines,
    }


def write_wav(out: Path, pcm: np.ndarray) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def write_meeting(out: Path, pcm: np.ndarray, truth: dict) -> Path:
    write_wav(out, pcm)
    sidecar = out.with_suffix(".json")
    sidecar.write_text(json.dumps(truth, ensure_ascii=False, indent=2))
    return sidecar
