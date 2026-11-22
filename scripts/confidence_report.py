"""Calibrate caption confidence: how often each level fires on clean vs degraded audio.

Builds two bases (the synthetic meeting fixture, and N cached FLEURS ja_jp dev
clips joined with gaps), degrades each (noise at a target SNR, 2x speed, every
line cut 60% in), runs the real pipeline on every variant, and prints the level
counts. Load models once; run it under the shared model lock:

    lockf -k /tmp/claude-501/mimivox-models.lock \\
        env PYTHONPATH=src .venv/bin/python scripts/confidence_report.py --out <dir>

FLEURS audio is read from the eval cache (never downloaded here).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 16_000
GAP_S = 1.2
FIXTURE = Path("data/fixtures/meeting-recording.wav")
FLEURS = Path.home() / "Library/Caches/mimivox/eval/fleurs/ja_jp"


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == SR and w.getsampwidth() == 2
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            data = data.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)
    return data.astype(np.float32) / 32768.0


def write_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def speech_rms(audio: np.ndarray) -> float:
    frames = audio[: len(audio) // 320 * 320].reshape(-1, 320)
    rms = np.sqrt((frames**2).mean(axis=1))
    return float(np.sqrt((rms[rms > 0.01] ** 2).mean()))


def fleurs_base(n: int) -> tuple[np.ndarray, list[tuple[float, float]]]:
    seen, clips = set(), []
    for line in (FLEURS / "dev.tsv").read_text(encoding="utf-8").splitlines():
        cols = line.split("\t")
        wav, text = FLEURS / "dev" / cols[1], cols[2]
        if text in seen or not wav.exists():
            continue
        audio = read_wav(wav)
        if len(audio) / SR > 13:
            continue
        seen.add(text)
        clips.append(audio)
        if len(clips) == n:
            break
    gap = np.zeros(int(GAP_S * SR), np.float32)
    parts, spans, t = [gap], [], GAP_S
    for clip in clips:
        spans.append((t, t + len(clip) / SR))
        parts += [clip, gap]
        t += len(clip) / SR + GAP_S
    return np.concatenate(parts), spans


def _mean(v: list[float]) -> str:
    return f"{sum(v) / len(v):.2f}" if v else "  - "


def fixture_base() -> tuple[np.ndarray, list[tuple[float, float]]]:
    lines = json.loads(FIXTURE.with_suffix(".json").read_text())["lines"]
    return read_wav(FIXTURE), [(line["start_s"], line["end_s"]) for line in lines]


def add_noise(audio: np.ndarray, snr_db: float, seed: int = 0) -> np.ndarray:
    noise = np.random.default_rng(seed).standard_normal(len(audio)).astype(np.float32)
    noise *= speech_rms(audio) / (10 ** (snr_db / 20)) / noise.std()
    return audio + noise


def cut_mid_word(audio: np.ndarray, spans: list[tuple[float, float]]) -> np.ndarray:
    out = np.zeros_like(audio)
    for start, end in spans:
        a, b = int(start * SR), int((start + 0.6 * (end - start)) * SR)
        out[a:b] = audio[a:b]
    return out


def speed_up(src: Path, dst: Path, tempo: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(src),
            "-filter:a",
            f"atempo={tempo}",
            "-ar",
            str(SR),
            "-ac",
            "1",
            str(dst),
        ],
        check=True,
    )


def variants(out: Path, n: int) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    made: dict[str, Path] = {}
    for name, (audio, spans) in {"fixture": fixture_base(), "fleurs": fleurs_base(n)}.items():
        for tag, sig in {
            "clean": audio,
            "snr10": add_noise(audio, 10),
            "snr0": add_noise(audio, 0),
            "cut60": cut_mid_word(audio, spans),
        }.items():
            made[f"{name}/{tag}"] = out / f"{name}-{tag}.wav"
            write_wav(made[f"{name}/{tag}"], sig)
        made[f"{name}/fast2x"] = out / f"{name}-fast2x.wav"
        speed_up(made[f"{name}/clean"], made[f"{name}/fast2x"], 2.0)
    return made


async def run_one(wav: Path) -> list[dict]:
    from mimivox.pipeline import run

    events: list[dict] = []
    await run(str(wav), live=False, asr_only=False, sse_broadcast=events.append)
    rows: dict[int, dict] = {}
    for ev in events:
        if ev["kind"] == "final":
            rows[ev["id"]] = ev
        elif ev["kind"] == "revision":
            rows[ev["target_id"]].update(ev)
            for gone in ev["merged_ids"]:
                rows.pop(gone, None)
    return list(rows.values())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n", type=int, default=10, help="FLEURS clips in the real-speech base")
    args = ap.parse_args()

    from mimivox.pipeline import load_all_models
    from mimivox.settings_store import apply_settings_for_session, load_settings

    apply_settings_for_session(load_settings())
    load_all_models()
    report: dict[str, list[dict]] = {}
    for key, wav in variants(args.out, args.n).items():
        report[key] = asyncio.run(run_one(wav))
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))

    print("\nCONFIDENCE REPORT  (lines per level)")
    print(f"{'variant':16}{'lines':>6}{'high':>6}{'medium':>8}{'low':>5}   mean asr  mean mt")
    for key, rows in report.items():
        levels = collections.Counter(r["confidence"]["level"] for r in rows)
        asr = [r["confidence"]["asr"] for r in rows if r["confidence"]["asr"] is not None]
        mt = [r["confidence"]["mt"] for r in rows if r["confidence"]["mt"] is not None]
        print(
            f"{key:16}{len(rows):>6}{levels['high']:>6}{levels['medium']:>8}{levels['low']:>5}"
            f"   {_mean(asr):>8}  {_mean(mt):>7}"
        )
    for label, keep in (
        ("clean", lambda k: k.endswith("/clean")),
        ("degraded", lambda k: not k.endswith("/clean")),
    ):
        levels = collections.Counter(
            r["confidence"]["level"] for k, rows in report.items() if keep(k) for r in rows
        )
        total = sum(levels.values())
        print(
            f"{label:16}{total:>6}"
            + "".join(
                f"  {name} {levels[name]} ({100 * levels[name] / total:.0f}%)"
                for name in ("high", "medium", "low")
            )
        )
    print()
    for key, rows in report.items():
        for r in rows:
            c = r["confidence"]
            print(
                f"{key:16}{c['level']:7} asr={c['asr']} mt={c['mt']} {c['reasons']} | {r['ja'][:28]} -> {r['en'][:40]}"
            )


if __name__ == "__main__":
    main()
