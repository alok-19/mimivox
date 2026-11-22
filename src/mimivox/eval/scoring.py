"""Turn pipeline log rows plus references into the eval report (no model code here)."""

from __future__ import annotations

from collections.abc import Sequence

from mimivox.eval.metrics import (
    Alignment,
    Row,
    align_rows,
    cer,
    chrf,
    latency_stats,
    line_speaker,
    speaker_accuracy,
)


def perceived_s(r: dict) -> float:
    """Speech end to EN on screen, as the pipeline's own summary defines it."""
    return (
        r.get("vad_wait_s", 0.0)
        + r.get("queue_wait_s", 0.0)
        + r["asr_latency_s"]
        + r["translate_latency_s"]
    )


def latency_block(log_rows: Sequence[dict]) -> dict:
    return {
        "asr_s": latency_stats([r["asr_latency_s"] for r in log_rows]),
        "cat_s": latency_stats([r["translate_latency_s"] for r in log_rows]),
        "perceived_s": latency_stats([perceived_s(r) for r in log_rows]),
    }


def revisions(log_rows: Sequence[dict]) -> int:
    """Number of later fragments merged back into an earlier caption."""
    return sum(len(r.get("merged_ids") or []) for r in log_rows)


def _join(rows: Sequence[Row], attr: str, sep: str) -> str:
    return sep.join(getattr(r, attr) for r in rows if getattr(r, attr))


def score_clips(items: Sequence[tuple[dict, Sequence[dict]]], fold_case: bool = False) -> dict:
    """items: (reference {sid, wav, ja_norm, en}, pipeline log rows) per clip.

    For a non-JA pair ``ja_*`` is the source text and ``en_*`` the translation.
    """
    refs_ja, hyps_ja, refs_en, hyps_en, per_clip = [], [], [], [], []
    all_rows: list[dict] = []
    for ref, rows in items:
        parsed = [Row.from_log(r) for r in rows]
        ja, en = _join(parsed, "ja", ""), _join(parsed, "en", " ")
        refs_ja.append(ref["ja_norm"])
        hyps_ja.append(ja)
        refs_en.append(ref["en"])
        hyps_en.append(en)
        all_rows.extend(rows)
        per_clip.append(
            {
                "sid": ref["sid"],
                "wav": ref["wav"],
                "rows": len(rows),
                "ja_ref": ref["ja_norm"],
                "ja_hyp": ja,
                "en_ref": ref["en"],
                "en_hyp": en,
                "cer": cer([ref["ja_norm"]], [ja], fold_case),
            }
        )
    return {
        "n": len(items),
        "empty": sum(1 for c in per_clip if not c["ja_hyp"]),
        "cer": cer(refs_ja, hyps_ja, fold_case),
        "chrf": chrf(refs_en, hyps_en),
        "latency": latency_block(all_rows),
        "revisions": revisions(all_rows),
        "per_clip": per_clip,
    }


def score_meeting(truth: dict, log_rows: Sequence[dict]) -> dict:
    lines = truth["lines"]
    parsed = [Row.from_log(r) for r in log_rows]
    al: Alignment = align_rows(parsed, lines)
    refs_ja = ["".join(lines[i]["ja"] for i in g.lines) for g in al.groups]
    refs_en = [" ".join(lines[i]["en"] for i in g.lines) for g in al.groups]
    hyps_ja = [_join(g.rows, "ja", "") for g in al.groups]
    hyps_en = [_join(g.rows, "en", " ") for g in al.groups]
    truth_spk = [ln["speaker"] for ln in lines]
    labels = [line_speaker(parsed, ln) for ln in lines]
    has_speakers = any(p.speaker is not None for p in parsed)
    return {
        "lines": len(lines),
        "rows": len(log_rows),
        "groups": len(al.groups),
        "missed_lines": al.missed_lines,
        "unaligned_rows": len(al.unaligned),
        "refs_ja": refs_ja,
        "hyps_ja": hyps_ja,
        "refs_en": refs_en,
        "hyps_en": hyps_en,
        "speaker_truth": truth_spk,
        "speaker_pred": labels,
        "speaker_accuracy": speaker_accuracy(truth_spk, labels) if has_speakers else None,
        "revisions": revisions(log_rows),
        "log_rows": list(log_rows),
    }


def aggregate_meetings(scored: Sequence[dict]) -> dict:
    def flat(key: str) -> list:
        return [t for m in scored for t in m[key]]

    rows = [r for m in scored for r in m["log_rows"]]
    spk = [m for m in scored if m["speaker_accuracy"] is not None]
    return {
        "n": len(scored),
        "lines": sum(m["lines"] for m in scored),
        "missed_lines": sum(m["missed_lines"] for m in scored),
        "unaligned_rows": sum(m["unaligned_rows"] for m in scored),
        "cer": cer(flat("refs_ja"), flat("hyps_ja")),
        "chrf": chrf(flat("refs_en"), flat("hyps_en")),
        "latency": latency_block(rows),
        "revisions": sum(m["revisions"] for m in scored),
        "speaker_accuracy": (
            sum(m["speaker_accuracy"] * m["lines"] for m in spk) / sum(m["lines"] for m in spk)
        )
        if spk
        else None,
    }


def _p50_p95(lat: dict, k: str) -> str:
    return f"{lat[k]['p50']:.2f} / {lat[k]['p95']:.2f}"


def markdown_table(clips: dict | None, meetings: dict | None) -> str:
    out = [
        "| set | n | CER | chrF | ASR p50/p95 (s) | CAT p50/p95 (s) | "
        "perceived p50/p95 (s) | speaker acc | revisions | missed lines |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, m in (("clips", clips), ("meetings", meetings)):
        if m is None:
            continue
        lat = m["latency"]
        n = f"{m['n']}" + (f" ({m['lines']} lines)" if "lines" in m else "")
        acc = m.get("speaker_accuracy")
        out.append(
            f"| {name} | {n} | {m['cer'] * 100:.1f}% | {m['chrf']:.1f} | {_p50_p95(lat, 'asr_s')} | "
            f"{_p50_p95(lat, 'cat_s')} | {_p50_p95(lat, 'perceived_s')} | "
            f"{'n/a' if acc is None else format(acc * 100, '.1f') + '%'} | {m['revisions']} | "
            f"{m.get('missed_lines', 'n/a')} |"
        )
    return "\n".join(out) + "\n"
