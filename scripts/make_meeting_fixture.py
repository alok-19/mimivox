"""Synthesize a two-speaker Japanese meeting WAV with a ground-truth script.

Uses the macOS ``say`` voices so the fixture is reproducible on any Mac with
no downloads. Writes 16 kHz mono PCM plus a sidecar JSON listing each line's
speaker, Japanese text, and start/end seconds.

    python scripts/make_meeting_fixture.py            # data/fixtures/meeting-recording.wav
    python scripts/make_meeting_fixture.py out.wav
    python scripts/make_meeting_fixture.py --split    # data/fixtures/meeting-recording-split.wav

``--split`` is opt-in: line 4 becomes one sentence spoken as two halves with a
mid-sentence pause (SPLIT_PAUSE_S), which the VAD splits into two utterances.
The default output is unchanged.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

SAMPLE_RATE = 16_000
GAP_S = 1.2

# Dropped subjects, a pronoun that needs the previous line, and glossary
# terms (部長, 見積もり, ミミボックス) the base model tends to mangle.
SCRIPT: list[tuple[str, str]] = [
    ("A", "田中さんから新しい見積もりが届きました。"),
    ("B", "先月より二割高いですね。"),
    ("A", "来週までに返事をしないといけないですね。"),
    ("B", "それで、部長には報告しましたか。"),
    ("A", "まだです。明日の会議で話すつもりです。"),
    ("B", "ミミボックスの導入の件も一緒に相談しましょう。"),
    ("A", "わかりました。資料は私が準備します。"),
    ("B", "ありがとうございます。よろしくお願いします。"),
]
# Opt-in scenario: speaker B's sentence is spoken in two halves with a pause
# longer than the VAD silence close (700 ms), so each half becomes a fragment.
SPLIT_LINE_INDEX = 3
SPLIT_PARTS = ("部長には報告しましたが、", "まだ返事がありません。")
SPLIT_PAUSE_S = 0.8

# Kyoko is the only Japanese voice macOS ships preinstalled; the others render
# silence until downloaded. Speaker B is Kyoko resampled down, which lowers
# pitch and formants together so it reads as a different voice.
VOICE = "Kyoko"
PITCH = {"A": 1.0, "B": 0.78}
MIN_LINE_S = 0.5


def _render(speaker: str, text: str, workdir: Path, idx: int) -> bytes:
    aiff = workdir / f"{idx}.aiff"
    wav = workdir / f"{idx}.wav"
    subprocess.run(["say", "-v", VOICE, "-o", str(aiff), text], check=True)
    p = PITCH[speaker]
    shift = f"asetrate=22050*{p},aresample=22050,atempo={1 / p:.4f},"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(aiff),
            "-af",
            f"{shift}aresample={SAMPLE_RATE}",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-sample_fmt",
            "s16",
            str(wav),
        ],
        check=True,
    )
    with wave.open(str(wav), "rb") as w:
        frames = w.readframes(w.getnframes())
    if len(frames) / 2 / SAMPLE_RATE < MIN_LINE_S:
        raise SystemExit(f"voice {VOICE!r} rendered silence for line {idx}; is it installed?")
    return frames


def build(out: Path, split: bool = False, limit: int | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    silence = b"\x00\x00" * int(GAP_S * SAMPLE_RATE)
    pause = b"\x00\x00" * int(SPLIT_PAUSE_S * SAMPLE_RATE)
    pcm = bytearray(silence)
    lines = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (speaker, ja) in enumerate(SCRIPT[:limit]):
            start = len(pcm) / 2 / SAMPLE_RATE
            if split and i == SPLIT_LINE_INDEX:
                speaker = "B"
                first, second = SPLIT_PARTS
                pcm += _render(speaker, first, Path(tmp), i)
                pcm += pause
                pcm += _render(speaker, second, Path(tmp), 100 + i)
                ja = "".join(SPLIT_PARTS)
            else:
                pcm += _render(speaker, ja, Path(tmp), i)
            end = len(pcm) / 2 / SAMPLE_RATE
            pcm += silence
            line = {
                "speaker": speaker,
                "ja": ja,
                "start_s": round(start, 2),
                "end_s": round(end, 2),
            }
            if split and i == SPLIT_LINE_INDEX:
                line["split_parts"] = list(SPLIT_PARTS)
                line["split_pause_s"] = SPLIT_PAUSE_S
            lines.append(line)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(pcm))
    truth = out.with_suffix(".json")
    truth.write_text(
        json.dumps({"voice": VOICE, "pitch": PITCH, "lines": lines}, ensure_ascii=False, indent=2)
    )
    return truth


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    split = "--split" in sys.argv[1:]
    default = "meeting-recording-split.wav" if split else "meeting-recording.wav"
    target = Path(args[0]) if args else Path("data/fixtures") / default
    print(build(target, split=split))
