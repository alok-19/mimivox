"""Google FLEURS (CC-BY-4.0) dev-split access: cache layout, TSV parsing, pairing."""

from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

DEFAULT_CACHE = Path.home() / "Library" / "Caches" / "mimivox" / "eval" / "fleurs"
HF_BASE = "https://huggingface.co/datasets/google/fleurs/resolve/main/data"
SPLIT = "dev"
# FLEURS directory per MimiVox language code (Mandarin is cmn_hans_cn, not zh_cn).
FLEURS_CODE = {"ja": "ja_jp", "zh": "cmn_hans_cn", "ko": "ko_kr", "en": "en_us"}
ALIASES = {"zh_cn": "cmn_hans_cn"}


@dataclasses.dataclass(frozen=True)
class Clip:
    """One spoken sentence. ``en`` is the shared-id English reference translation."""

    sid: str
    wav: Path
    ja_raw: str
    ja_norm: str
    en: str
    gender: str
    num_samples: int

    @property
    def duration_s(self) -> float:
        return self.num_samples / 16_000


def tsv_path(cache: Path, lang: str) -> Path:
    return cache / lang / f"{SPLIT}.tsv"


def audio_dir(cache: Path, lang: str) -> Path:
    return cache / lang / SPLIT


def parse_tsv(path: Path) -> list[dict]:
    """FLEURS TSV columns: id, wav filename, raw, normalized, phonemes, num_samples, gender."""
    rows = []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            if len(r) < 7:
                continue
            rows.append(
                {
                    "sid": r[0],
                    "wav": r[1],
                    "raw": r[2],
                    "norm": r[3],
                    "num_samples": int(r[5]),
                    "gender": r[6].strip().upper(),
                }
            )
    return rows


def load_clips(
    cache: Path = DEFAULT_CACHE, lang: str = "ja_jp", ref_lang: str = "en_us"
) -> list[Clip]:
    """Clips (``lang`` audio, any language; ``ja_*`` fields hold the source text) that
    have audio on disk and a ``ref_lang`` reference translation with the same id
    (stored in ``en``)."""
    refs: dict[str, str] = {}
    for r in parse_tsv(tsv_path(cache, ref_lang)):
        refs.setdefault(r["sid"], r["raw"])
    clips = []
    for r in parse_tsv(tsv_path(cache, lang)):
        wav = audio_dir(cache, lang) / r["wav"]
        if r["sid"] in refs and wav.is_file():
            clips.append(
                Clip(
                    r["sid"],
                    wav,
                    r["raw"],
                    r["norm"],
                    refs[r["sid"]],
                    r["gender"],
                    r["num_samples"],
                )
            )
    return clips
