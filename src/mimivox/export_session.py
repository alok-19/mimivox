"""mimivox.export_session — Write session transcripts in multiple formats.

Input is a list of Swift MeetingEvent-shaped dicts (AsrCompletedEvent +
TranslationCompletedEvent pairs preferred). Also accepts raw pipeline finals.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mimivox.config import DATA_DIR

SUPPORTED_FORMATS = frozenset({"markdown", "jsonl", "srt", "vtt", "csv", "text", "json"})

_EXT = {
    "markdown": "md",
    "jsonl": "jsonl",
    "srt": "srt",
    "vtt": "vtt",
    "csv": "csv",
    "text": "txt",
    "json": "json",
}


def extension_for(fmt: str) -> str:
    return _EXT.get(fmt, fmt)


def _ts_seconds(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        if ":" in s:
            try:
                parts = s.split(":")
                if len(parts) == 2:
                    return int(parts[0]) * 60 + float(parts[1])
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            except ValueError:
                pass
        try:
            return float(s)
        except ValueError:
            return default
    return default


def _fmt_clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    mm = int(seconds // 60)
    ss = seconds - mm * 60
    return f"{mm:02d}:{ss:05.2f}"


def _fmt_srt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _fmt_vtt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _speaker_field(*events: dict[str, Any] | None) -> dict[str, str]:
    """``{"speaker": label}`` from the first event carrying one, else empty."""
    for ev in events:
        sp = (ev or {}).get("speaker")
        if isinstance(sp, dict) and sp.get("label"):
            return {"speaker": str(sp["label"])}
    return {}


def _confidence_field(*events: dict[str, Any] | None) -> dict[str, Any]:
    """First event carrying a confidence dict wins."""
    for ev in events:
        conf = ev.get("confidence") if ev else None
        if isinstance(conf, dict) and conf.get("level"):
            return {"confidence": conf}
    return {}


def _flagged(row: dict[str, Any]) -> dict[str, Any] | None:
    """The row's confidence when it is worth flagging (medium or low)."""
    conf = row.get("confidence")
    return conf if conf and conf.get("level") in ("medium", "low") else None


def _confidence_note(row: dict[str, Any]) -> str:
    conf = _flagged(row)
    if not conf:
        return ""
    reasons = "; ".join(conf.get("reasons") or [])
    return f"{conf['level']} confidence" + (f": {reasons}" if reasons else "")


def _skip_note(row: dict[str, Any]) -> str:
    kind = row.get("skipped")
    if kind == "gap":
        return "skipped while catching up" + (
            "" if row.get("recoverable") else " (not recoverable)"
        )
    if kind == "translation":
        return "not translated while catching up"
    return ""


def _gap_row(ev: dict[str, Any]) -> dict[str, Any]:
    start = _ts_seconds(ev.get("start"))
    end = max(start, _ts_seconds(ev.get("end"), start))
    return {
        "index": ev.get("index"),
        "start": start,
        "end": end,
        "ja": "",
        "en": f"[Skipped {end - start:.0f}s of speech while catching up]",
        "skipped": "gap",
        "recoverable": bool(ev.get("recoverable")),
    }


def _tag(row: dict[str, Any]) -> str:
    return f"{row['speaker']}: " if row.get("speaker") else ""


def pair_events(
    events: list[dict[str, Any]],
    pinned_ids: Iterable[int] | None = None,
    *,
    pinned_only: bool = False,
) -> list[dict[str, Any]]:
    """Pair ASR + Translation completed events into bilingual rows.

    Falls back to raw pipeline ``final`` events when present. Rows the viewer
    pinned (by ASR or translation index) get ``"pinned": True``; with
    ``pinned_only`` the rest are dropped. Without pins no key is added.
    """
    rows: list[dict[str, Any]] = []
    asr_by_index: dict[Any, dict[str, Any]] = {}
    # target index -> latest revision; merged indexes are dropped as rows.
    revisions: dict[Any, dict[str, Any]] = {}
    merged_away: set[Any] = set()

    for ev in events:
        kind = ev.get("type") or ev.get("kind")
        if kind in ("TranscriptRevisionEvent", "revision"):
            target = ev.get("index") if ev.get("index") is not None else ev.get("target_id")
            revisions[target] = ev
            merged_away.update(ev.get("merged_indexes") or ev.get("merged_ids") or [])
            continue
        if kind == "CaptionGapEvent":
            rows.append(_gap_row(ev))
        elif kind == "AsrCompletedEvent":
            idx = ev.get("index")
            asr_by_index[idx] = ev
        elif kind == "TranslationCompletedEvent":
            idx = ev.get("index")
            src_indexes = ev.get("source_indexes") or []
            asr = None
            if idx in asr_by_index:
                asr = asr_by_index[idx]
            elif src_indexes:
                asr = asr_by_index.get(src_indexes[0])
            start = _ts_seconds(ev.get("start"), _ts_seconds(asr.get("start") if asr else 0))
            end = _ts_seconds(
                ev.get("end"),
                _ts_seconds(asr.get("end") if asr else start, start),
            )
            if end < start:
                end = start
            rows.append(
                {
                    "index": idx,
                    "start": start,
                    "end": end,
                    "ja": (asr.get("text") if asr else "") or "",
                    "en": ev.get("text") or "",
                    "_asr": asr.get("index") if asr else None,
                    **_speaker_field(ev, asr),
                    **_confidence_field(ev, asr),
                }
            )
        elif kind == "final":
            start = _ts_seconds(ev.get("ts") or ev.get("start"))
            duration = _ts_seconds(ev.get("duration_s"), 0.0)
            end = start + duration if duration > 0 else start
            rows.append(
                {
                    "index": ev.get("id") if ev.get("id") is not None else len(rows) + 1,
                    "start": start,
                    "end": end,
                    "ja": ev.get("ja") or "",
                    "en": ev.get("en") or "",
                    **_speaker_field(ev),
                    **_confidence_field(ev),
                }
            )

    # ASR without translation: still export JA-only rows.
    used_asr = {r["index"] for r in rows if r.get("index") is not None}
    for idx, asr in asr_by_index.items():
        if idx in used_asr:
            continue
        start = _ts_seconds(asr.get("start"))
        end = _ts_seconds(asr.get("end"), start)
        rows.append(
            {
                "index": idx,
                "start": start,
                "end": end if end >= start else start,
                "ja": asr.get("text") or "",
                "en": "",
                **({"skipped": "translation"} if asr.get("translation_skipped") else {}),
                **_speaker_field(asr),
                **_confidence_field(asr),
            }
        )

    rows = [r for r in rows if r.get("index") not in merged_away]
    for row in rows:
        rev = revisions.get(row.get("index"))
        if rev is None:
            continue
        row["ja"] = rev.get("ja") or row["ja"]
        row["en"] = rev.get("text") if rev.get("text") is not None else rev.get("en", row["en"])
        row.update(_confidence_field(rev))
        duration = _ts_seconds(rev.get("duration_s"), 0.0)
        end = _ts_seconds(rev.get("end"), row["start"] + duration)
        if end > row["end"]:
            row["end"] = end

    rows.sort(key=lambda r: (r["start"], r.get("index") or 0))
    pins = set(pinned_ids or ())
    for row in rows:
        asr_index = row.pop("_asr", None)
        if pins and (row.get("index") in pins or asr_index in pins):
            row["pinned"] = True
    if pinned_only:
        rows = [r for r in rows if r.get("pinned")]
    return rows


def render_markdown(rows: list[dict[str, Any]], notes: dict[str, Any] | None = None) -> str:
    lines: list[str] = []
    if notes:
        from mimivox.meeting_notes import MeetingNotes, render_notes_markdown

        positions = {
            r["index"]: i for i, r in enumerate(rows, start=1) if r.get("index") is not None
        }
        lines += [render_notes_markdown(MeetingNotes.from_dict(notes), positions)]
    lines += ["## Transcript", ""]
    if not rows:
        lines.append("_No utterances._")
        lines.append("")
        return "\n".join(lines)
    for i, row in enumerate(rows, start=1):
        start = _fmt_clock(row["start"])
        end = _fmt_clock(row["end"])
        who = f" {row['speaker']}" if row.get("speaker") else ""
        star = " ★" if row.get("pinned") else ""
        lines.append(f"### {i}. [{start} → {end}]{who}{star}")
        lines.append("")
        if note := _confidence_note(row):
            lines.append(f"> ⚠ {note}")
            lines.append("")
        if note := _skip_note(row):
            lines.append(f"> ⏭ {note}")
            lines.append("")
        lines.append("**JA**  ")
        lines.append(row["ja"] or "—")
        lines.append("")
        lines.append("**EN**  ")
        lines.append(row["en"] or "—")
        lines.append("")
    return "\n".join(lines)


def render_jsonl(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "\n"
    lines = []
    for row in rows:
        lines.append(
            json.dumps(
                {
                    "start": row["start"],
                    "end": row["end"],
                    "ja": row["ja"],
                    "en": row["en"],
                    **({"speaker": row["speaker"]} if row.get("speaker") else {}),
                    **({"pinned": True} if row.get("pinned") else {}),
                    **({"skipped": row["skipped"]} if row.get("skipped") else {}),
                    **({"confidence": row["confidence"]} if row.get("confidence") else {}),
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines) + "\n"


def render_srt(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "\n"
    blocks: list[str] = []
    for i, row in enumerate(rows, start=1):
        start = row["start"]
        end = row["end"] if row["end"] > start else start + 1.0
        text_parts = []
        if row["ja"]:
            text_parts.append(row["ja"])
        if row["en"]:
            text_parts.append(row["en"])
        text = "\n".join(text_parts) if text_parts else "—"
        text = _tag(row) + text
        blocks.append(f"{i}\n{_fmt_srt_ts(start)} --> {_fmt_srt_ts(end)}\n{text}\n")
    return "\n".join(blocks)


def render_vtt(rows: list[dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for i, row in enumerate(rows, start=1):
        start = row["start"]
        end = row["end"] if row["end"] > start else start + 1.0
        text_parts = []
        if row["ja"]:
            text_parts.append(row["ja"])
        if row["en"]:
            text_parts.append(row["en"])
        text = "\n".join(text_parts) if text_parts else "—"
        text = _tag(row) + text
        lines.append(str(i))
        lines.append(f"{_fmt_vtt_ts(start)} --> {_fmt_vtt_ts(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def render_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    with_speaker = any(r.get("speaker") for r in rows)
    with_conf = any(r.get("confidence") for r in rows)
    writer.writerow(
        ["start", "end", "ja", "en"]
        + (["speaker"] if with_speaker else [])
        + (["confidence", "confidence_reasons"] if with_conf else [])
    )
    for row in rows:
        writer.writerow(
            [
                f"{row['start']:.3f}",
                f"{row['end']:.3f}",
                row["ja"],
                row["en"],
            ]
            + ([row.get("speaker", "")] if with_speaker else [])
            + (
                [
                    (row.get("confidence") or {}).get("level", ""),
                    "; ".join((row.get("confidence") or {}).get("reasons") or []),
                ]
                if with_conf
                else []
            )
        )
    return buf.getvalue()


def render_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no utterances)\n"
    parts: list[str] = []
    for row in rows:
        parts.append(f"JA: {_tag(row)}{row['ja']}")
        parts.append(f"EN: {row['en']}")
        if note := _confidence_note(row):
            parts.append(f"[{note}]")
        if note := _skip_note(row):
            parts.append(f"[{note}]")
        parts.append("")
    return "\n".join(parts)


def render_json(
    events: list[dict[str, Any]],
    *,
    session_id: str | None = None,
    settings: dict[str, Any] | None = None,
    pinned_ids: Iterable[int] | None = None,
    pinned_only: bool = False,
) -> str:
    payload = {
        "session_id": session_id,
        "events": events,
        "settings": settings,
        "rows": pair_events(events, pinned_ids, pinned_only=pinned_only),
    }
    if pinned_ids:
        payload["pinned_ids"] = sorted(set(pinned_ids))
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render(
    fmt: str,
    events: list[dict[str, Any]],
    *,
    session_id: str | None = None,
    settings: dict[str, Any] | None = None,
    pinned_ids: Iterable[int] | None = None,
    pinned_only: bool = False,
    notes: dict[str, Any] | None = None,
) -> str:
    """Render events into the requested format. Raises ValueError if unknown.

    ``pinned_ids`` marks the viewer's pinned lines (``pinned_only`` keeps just
    those). ``notes`` (cached MeetingNotes dict) is included in the markdown
    export only.
    """
    key = (fmt or "json").strip().lower()
    if key not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unknown export format: {fmt!r}. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    if key == "json":
        return render_json(
            events,
            session_id=session_id,
            settings=settings,
            pinned_ids=pinned_ids,
            pinned_only=pinned_only,
        )
    rows = pair_events(events, pinned_ids, pinned_only=pinned_only)
    if key == "markdown":
        return render_markdown(rows, notes)
    if key == "jsonl":
        return render_jsonl(rows)
    if key == "srt":
        return render_srt(rows)
    if key == "vtt":
        return render_vtt(rows)
    if key == "csv":
        return render_csv(rows)
    if key == "text":
        return render_text(rows)
    raise ValueError(f"Unknown export format: {fmt!r}")


def write_export(
    fmt: str,
    events: list[dict[str, Any]],
    *,
    session_id: str | None = None,
    settings: dict[str, Any] | None = None,
    out_dir: str | Path = Path(DATA_DIR) / "transcripts",
    pinned_ids: Iterable[int] | None = None,
    pinned_only: bool = False,
    notes: dict[str, Any] | None = None,
) -> Path:
    """Render and write under out_dir/{session_id}.{ext}. Returns path.

    A ``pinned_only`` export is written as ``{session_id}.pinned.{ext}`` so it
    never overwrites the full transcript.
    """
    content = render(
        fmt,
        events,
        session_id=session_id,
        settings=settings,
        pinned_ids=pinned_ids,
        pinned_only=pinned_only,
        notes=notes,
    )
    key = (fmt or "json").strip().lower()
    sid = session_id or "nosession"
    if pinned_only:
        sid = f"{sid}.pinned"
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sid}.{extension_for(key)}"
    path.write_text(content, encoding="utf-8")
    return path
