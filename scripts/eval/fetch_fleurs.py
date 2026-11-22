"""Download FLEURS dev audio and transcripts into the eval cache (idempotent).

    PYTHONPATH=src python scripts/eval/fetch_fleurs.py                      # ja_jp + en_us
    PYTHONPATH=src python scripts/eval/fetch_fleurs.py --langs cmn_hans_cn ko_kr --limit 30
    PYTHONPATH=src python scripts/eval/fetch_fleurs.py --langs ja_jp en_us --audio-langs en_us --limit 30

Every language gets its TSV (en_us and ja_jp double as reference translations).
Audio is fetched for ``--audio-langs`` (default: every language except en_us).
``--limit N`` streams the dev tarball and stops after N clips instead of
downloading it whole (not hash-checked, since the stream is cut short).
``zh_cn`` is accepted as an alias of FLEURS's ``cmn_hans_cn``. A second run
downloads nothing.
FLEURS is CC-BY-4.0, see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

from mimivox.eval.fleurs import (
    ALIASES,
    DEFAULT_CACHE,
    HF_BASE,
    SPLIT,
    audio_dir,
    parse_tsv,
    tsv_path,
)

TEXT_ONLY = {"en_us"}


def _head(url: str) -> dict:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req) as r:
        h = r.headers
        return {
            "size": int(h.get("x-linked-size") or h.get("content-length") or 0),
            "sha256": (h.get("x-linked-etag") or "").strip('"'),
        }


def _download(url: str, dest: Path, size: int, sha256: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading {url} -> {dest} ({size / 1e6:.1f} MB)", flush=True)
    h = hashlib.sha256()
    with urllib.request.urlopen(url) as r, open(part, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
            h.update(chunk)
    if len(sha256) == 64 and h.hexdigest() != sha256:
        part.unlink()
        raise SystemExit(f"sha256 mismatch for {url}: got {h.hexdigest()}, want {sha256}")
    part.rename(dest)


def _wav_format_tag(path: Path) -> int:
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack("<H", head[20:22])[0] if len(head) == 24 else 0


def ensure_pcm16(path: Path) -> None:
    """FLEURS ships float32 WAVs; the pipeline reads 16-bit PCM. Convert in place."""
    if _wav_format_tag(path) == 1:
        return
    tmp = path.with_suffix(".conv.wav")
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            str(tmp),
        ],
        check=True,
    )
    tmp.replace(path)


def fetch_text(cache: Path, lang: str) -> None:
    dest = tsv_path(cache, lang)
    if dest.is_file():
        return
    url = f"{HF_BASE}/{lang}/{SPLIT}.tsv"
    info = _head(url)
    _download(url, dest, info["size"], info["sha256"])


MAX_MEMBER_BYTES = 64 << 20  # a 30 s float32 clip is ~2 MB; refuse anything absurd


def fetch_audio_limited(cache: Path, lang: str, limit: int) -> None:
    """Stream the dev tarball, keep the first ``limit`` listed wavs, then stop."""
    out = audio_dir(cache, lang)
    wanted = {r["wav"] for r in parse_tsv(tsv_path(cache, lang))}
    out.mkdir(parents=True, exist_ok=True)
    have = {w for w in wanted if (out / w).is_file()}
    if len(have) < limit:
        url = f"{HF_BASE}/{lang}/audio/{SPLIT}.tar.gz"
        print(f"streaming {url} until {limit} clips", flush=True)
        with urllib.request.urlopen(url) as r, tarfile.open(fileobj=r, mode="r|gz") as tar:
            for m in tar:
                name = Path(m.name).name  # flatten; never trust archive paths
                if not (m.isfile() and name in wanted and name not in have):
                    continue
                if m.size > MAX_MEMBER_BYTES:
                    continue
                f = tar.extractfile(m)
                if f is not None:
                    (out / name).write_bytes(f.read())
                    have.add(name)
                if len(have) >= limit:
                    break
    for w in sorted(have):
        ensure_pcm16(out / w)
    print(f"{lang}: {len(have)} clips in {out}", flush=True)


def fetch_audio(cache: Path, lang: str) -> None:
    out = audio_dir(cache, lang)
    wanted = {r["wav"] for r in parse_tsv(tsv_path(cache, lang))}
    if all((out / w).is_file() for w in wanted):
        for w in sorted(wanted):
            ensure_pcm16(out / w)
        return
    url = f"{HF_BASE}/{lang}/audio/{SPLIT}.tar.gz"
    archive = cache / lang / "audio" / f"{SPLIT}.tar.gz"
    if not archive.is_file():
        info = _head(url)
        _download(url, archive, info["size"], info["sha256"])
    out.mkdir(parents=True, exist_ok=True)
    print(f"extracting {len(wanted)} wavs -> {out}", flush=True)
    with tarfile.open(archive) as tar:
        for m in tar:
            name = Path(m.name).name  # flatten; never trust archive paths
            if m.isfile() and name in wanted and not (out / name).is_file():
                f = tar.extractfile(m)
                if f is not None:
                    (out / name).write_bytes(f.read())
    missing = [w for w in wanted if not (out / w).is_file()]
    if missing:
        raise SystemExit(f"{len(missing)} wavs missing after extract (e.g. {missing[0]})")
    for w in sorted(wanted):
        ensure_pcm16(out / w)
    archive.unlink()  # extracted; keep the cache small


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--langs", nargs="+", default=["ja_jp", "en_us"])
    ap.add_argument(
        "--audio-langs",
        nargs="+",
        default=None,
        help="languages to fetch audio for (default: all --langs except en_us)",
    )
    ap.add_argument(
        "--limit", type=int, default=0, help="stop after N clips per language (0 = whole dev set)"
    )
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = ap.parse_args()
    norm = lambda ls: [ALIASES.get(x, x) for x in ls]  # noqa: E731
    langs = list(dict.fromkeys([*norm(args.langs), "en_us"]))  # references always needed
    audio = set(norm(args.audio_langs)) if args.audio_langs else set(langs) - TEXT_ONLY
    for lang in langs:
        fetch_text(args.cache, lang)
        if lang in audio:
            if args.limit:
                fetch_audio_limited(args.cache, lang, args.limit)
            else:
                fetch_audio(args.cache, lang)
    print(f"FLEURS {SPLIT} ready in {args.cache}")


if __name__ == "__main__":
    sys.exit(main())
