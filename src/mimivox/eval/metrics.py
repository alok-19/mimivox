"""Pure metric helpers for the eval harness: CER, chrF, alignment, speakers, percentiles."""

from __future__ import annotations

import dataclasses
import itertools
import unicodedata
from collections.abc import Sequence

import numpy as np


def normalize_ja(text: str, fold_case: bool = False) -> str:
    """NFKC, then drop punctuation, symbols and whitespace. CER never counts them.

    ``fold_case`` lowercases too (English sources; off for CJK so JA scores are unchanged).
    """
    text = unicodedata.normalize("NFKC", text)
    if fold_case:
        text = text.casefold()
    return "".join(c for c in text if unicodedata.category(c)[0] not in "PSZC")


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(refs: Sequence[str], hyps: Sequence[str], fold_case: bool = False) -> float:
    """Corpus CER: total edits over total reference characters, after normalize_ja."""
    edits = total = 0
    for r, h in zip(refs, hyps):
        r, h = normalize_ja(r, fold_case), normalize_ja(h, fold_case)
        edits += edit_distance(r, h)
        total += len(r)
    return edits / total if total else 0.0


def chrf(refs: Sequence[str], hyps: Sequence[str]) -> float:
    """Corpus chrF (0-100) via sacrebleu defaults."""
    from sacrebleu.metrics import CHRF

    return float(CHRF().corpus_score(list(hyps), [list(refs)]).score)


def percentile(values: Sequence[float], q: float) -> float:
    return float(np.percentile(values, q)) if len(values) else float("nan")


def latency_stats(values: Sequence[float]) -> dict[str, float]:
    return {"p50": percentile(values, 50), "p95": percentile(values, 95)}


@dataclasses.dataclass
class Row:
    """A pipeline caption row reduced to what alignment needs."""

    start_s: float
    end_s: float
    ja: str
    en: str
    speaker: str | None = None
    merged: bool = False

    @classmethod
    def from_log(cls, r: dict) -> Row:
        if "timeline_s" in r:
            start = float(r["timeline_s"])
        else:  # older logs only have mm:ss, floor-rounded
            m, s = str(r.get("audio_ts", "00:00")).split(":")[:2]
            start = int(m) * 60 + int(s)
        spk = r.get("speaker")
        return cls(
            start,
            start + float(r.get("duration_s") or 0.0),
            r.get("ja", ""),
            r.get("en", ""),
            spk.get("id") if isinstance(spk, dict) else None,
            bool(r.get("merged_ids")),
        )


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


@dataclasses.dataclass
class Group:
    """Ground-truth lines and caption rows that overlap each other in time.

    The VAD can merge adjacent lines into one row or split one line into
    several, and a row's text cannot be cut at a line boundary, so lines and
    rows are scored together as a group. ``rows`` is empty for a missed line.
    """

    lines: list[int]
    rows: list[Row]


@dataclasses.dataclass
class Alignment:
    groups: list[Group]
    unaligned: list[Row]  # rows overlapping no ground-truth line

    @property
    def missed_lines(self) -> int:
        return sum(len(g.lines) for g in self.groups if not g.rows)


def align_rows(rows: Sequence[Row], lines: Sequence[dict]) -> Alignment:
    """Connected components of the time-overlap graph between rows and lines."""
    rows = sorted(rows, key=lambda r: r.start_s)
    parent = list(range(len(lines)))  # union-find over lines

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    touching: list[list[int]] = []
    for row in rows:
        hit = [
            i
            for i, ln in enumerate(lines)
            if overlap(row.start_s, row.end_s, ln["start_s"], ln["end_s"]) > 0
        ]
        touching.append(hit)
        for i in hit[1:]:  # a row spanning several lines joins them
            parent[find(i)] = find(hit[0])
    by_root: dict[int, Group] = {}
    for i in range(len(lines)):
        by_root.setdefault(find(i), Group([], [])).lines.append(i)
    unaligned: list[Row] = []
    for row, hit in zip(rows, touching):
        if hit:
            by_root[find(hit[0])].rows.append(row)
        else:
            unaligned.append(row)
    return Alignment(sorted(by_root.values(), key=lambda g: g.lines[0]), unaligned)


def speaker_accuracy(truth: Sequence[str], pred: Sequence[str | None]) -> float:
    """Fraction of lines whose predicted label matches under the best label mapping.

    Labels are arbitrary, so try every injective map from true speakers to
    predicted labels and keep the best. A None prediction never matches.
    """
    if not truth:
        return 0.0
    speakers = sorted(set(truth))
    labels = sorted({p for p in pred if p is not None})
    best = 0
    k = min(len(speakers), len(labels))
    for chosen in itertools.permutations(labels, k):
        mapping = dict(zip(speakers, chosen))  # speakers beyond k stay unmapped
        best = max(
            best, sum(1 for t, p in zip(truth, pred) if p is not None and mapping.get(t) == p)
        )
    return best / len(truth)


def line_speaker(rows: Sequence[Row], line: dict) -> str | None:
    """Label of the row overlapping the line most; None if unlabeled."""
    best, best_ov = None, 0.0
    for r in rows:
        ov = overlap(r.start_s, r.end_s, line["start_s"], line["end_s"])
        if r.speaker is not None and ov > best_ov:
            best, best_ov = r.speaker, ov
    return best
