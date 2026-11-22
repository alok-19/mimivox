"""Build synthetic multi-speaker meetings from FLEURS ja_jp dev clips.

    PYTHONPATH=src python scripts/eval/build_meetings.py [-n 5] [--seed 0]

Writes data/eval/meetings/meeting-<i>.wav plus a ground-truth .json in the same
shape as scripts/make_meeting_fixture.py (speaker, ja, start_s, end_s per line,
plus the English reference). Speaker A is a male clip, B a female clip; turns
alternate. Clips are levelled to -23 dBFS RMS (see --raw-levels). FLEURS has no speaker id, so within a gender the voices may differ.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

from mimivox.eval.fleurs import DEFAULT_CACHE, load_clips
from mimivox.eval.meetings import LEVEL_DBFS, plan_meeting, render_meeting, write_meeting


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-n", type=int, default=5, help="number of meetings")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--raw-levels",
        action="store_true",
        help="keep FLEURS's original per-clip levels instead of levelling to -23 dBFS RMS",
    )
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--out", type=Path, default=Path("data/eval/meetings"))
    args = ap.parse_args()

    clips = load_clips(args.cache)
    if not clips:
        raise SystemExit("no FLEURS clips; run scripts/eval/fetch_fleurs.py first")
    rng = random.Random(args.seed)
    use_counts: dict[Path, int] = {}
    for i in range(1, args.n + 1):
        plan = plan_meeting(clips, rng, use_counts)
        pcm, truth = render_meeting(plan, rng, None if args.raw_levels else LEVEL_DBFS)
        sidecar = write_meeting(args.out / f"meeting-{i}.wav", pcm, truth)
        print(f"{sidecar.with_suffix('.wav')}  {len(pcm) / 16000:.0f}s  {len(plan)} lines")


if __name__ == "__main__":
    main()
