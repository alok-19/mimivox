"""mimivox.session_library — full-text search over archived sessions.

The session JSON files stay the source of truth (including user tags). This
module keeps a derived SQLite index next to them so a library of dozens of
meetings can be searched by Japanese or English text, speaker, tag and date.

Data shape (one row per archived line, plus one meta row per session):

  sessions(id, label, started_at, started_ts, signature, line_count)
  tags(session_id, tag)
  lines(id, session_id, line_id, start, speaker, ja, en, ja_n, en_n, spk_n, meta_n)
  fts(ja, en, speaker, meta)         -- trigram, rowid = lines.id, normalized text

``line_id`` is the row ``index`` that ``export_session.pair_events`` yields, which
is the same id the client timeline uses, so a hit can be scrolled to. The meta
row has ``line_id = -1`` and carries the title, tags and date for sessions with
no lines.

Japanese has no spaces, so FTS5 uses the ``trigram`` tokenizer (substring
match). Trigram cannot match terms shorter than 3 characters (many Japanese
words are 2: 予算, 部長), so those terms fall back to an ``instr`` scan.
Text is NFKC-normalized and lowercased on both sides.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from mimivox.export_session import pair_events

META_LINE_ID = -1
MAX_TAG_LEN = 40
MAX_LINES_PER_SESSION = 5
SNIPPET_RADIUS = 36

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY, label TEXT NOT NULL, started_at TEXT NOT NULL,
    started_ts REAL NOT NULL, signature TEXT NOT NULL, line_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tags(
    session_id TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY(session_id, tag));
CREATE TABLE IF NOT EXISTS lines(
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, line_id INTEGER NOT NULL,
    start REAL NOT NULL, speaker TEXT NOT NULL, ja TEXT NOT NULL, en TEXT NOT NULL,
    ja_n TEXT NOT NULL, en_n TEXT NOT NULL, spk_n TEXT NOT NULL, meta_n TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS lines_session ON lines(session_id);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    ja, en, speaker, meta, tokenize='trigram');
"""


def norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def normalize_tag(raw: str) -> str:
    tag = re.sub(r"\s+", " ", (raw or "").strip().lstrip("#")).strip()
    if not tag:
        raise ValueError("tag must not be empty")
    if len(tag) > MAX_TAG_LEN:
        raise ValueError(f"tag longer than {MAX_TAG_LEN} characters")
    return tag


def merge_tags(existing: list[str], add: list[str] = (), remove: list[str] = ()) -> list[str]:
    """Case-insensitive de-duplicated tag list, order preserved."""
    drop = {norm(t) for t in remove}
    out: dict[str, str] = {}
    for t in [*existing, *add]:
        out.setdefault(norm(t), t)
    return [t for k, t in out.items() if k not in drop]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA secure_delete=ON")  # deleted transcript text must not linger on disk
    conn.executescript(_SCHEMA)
    return conn


def _signature(path: Path) -> str:
    st = path.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def _started_ts(started_at: str) -> float:
    try:
        dt = datetime.fromisoformat(started_at)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _drop(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute(
        "DELETE FROM fts WHERE rowid IN (SELECT id FROM lines WHERE session_id=?)",
        (session_id,),
    )
    conn.execute("DELETE FROM lines WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM tags WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def _index_payload(conn: sqlite3.Connection, payload: dict[str, Any], signature: str) -> None:
    sid = str(payload["id"])
    label = str(payload.get("label") or "session")
    started_at = str(payload.get("started_at") or "")
    tags = [str(t) for t in payload.get("tags") or []]
    rows = pair_events(list(payload.get("events") or []))
    title = str(payload.get("title") or "")
    meta_n = norm(" ".join([label, title, started_at[:10], *tags]))

    _drop(conn, sid)
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        (sid, label, started_at, _started_ts(started_at), signature, len(rows)),
    )
    conn.executemany("INSERT INTO tags VALUES (?,?)", [(sid, t) for t in tags])

    entries = [(META_LINE_ID, 0.0, "", "", "")] + [
        (
            r["index"] if isinstance(r.get("index"), int) else i,
            float(r.get("start") or 0.0),
            str(r.get("speaker") or ""),
            str(r.get("ja") or ""),
            str(r.get("en") or ""),
        )
        for i, r in enumerate(rows, start=1)
    ]
    for line_id, start, speaker, ja, en in entries:
        ja_n, en_n, spk_n = norm(ja), norm(en), norm(speaker)
        cur = conn.execute(
            "INSERT INTO lines(session_id,line_id,start,speaker,ja,en,ja_n,en_n,spk_n,meta_n)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, line_id, start, speaker, ja, en, ja_n, en_n, spk_n, meta_n),
        )
        conn.execute(
            "INSERT INTO fts(rowid,ja,en,speaker,meta) VALUES (?,?,?,?,?)",
            (cur.lastrowid, ja_n, en_n, spk_n, meta_n),
        )


def index_file(db_path: Path, session_path: Path) -> bool:
    """(Re)index one archive file. Returns False when it is unreadable."""
    try:
        payload = json.loads(session_path.read_text(encoding="utf-8"))
        signature = _signature(session_path)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not payload.get("id"):
        return False
    conn = connect(db_path)
    try:
        with conn:
            _index_payload(conn, payload, signature)
    finally:
        conn.close()
    return True


def remove_session(db_path: Path, session_id: str) -> None:
    conn = connect(db_path)
    try:
        with conn:
            _drop(conn, session_id)
    finally:
        conn.close()


def backfill(db_path: Path, sessions_dir: Path) -> dict[str, int]:
    """Bring the index in line with the archive directory. Idempotent."""
    stats = {"indexed": 0, "unchanged": 0, "removed": 0, "skipped": 0}
    files = (
        {p.stem: p for p in sessions_dir.glob("*.json") if p.name != "index.json"}
        if sessions_dir.exists()
        else {}
    )
    conn = connect(db_path)
    try:
        with conn:
            known = {
                r["id"]: r["signature"] for r in conn.execute("SELECT id, signature FROM sessions")
            }
            for sid in set(known) - set(files):
                _drop(conn, sid)
                stats["removed"] += 1
            for sid, path in files.items():
                try:
                    signature = _signature(path)
                    if known.get(sid) == signature:
                        stats["unchanged"] += 1
                        continue
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    stats["skipped"] += 1
                    continue
                if not isinstance(payload, dict) or str(payload.get("id") or "") != sid:
                    stats["skipped"] += 1
                    continue
                _index_payload(conn, payload, signature)
                stats["indexed"] += 1
    finally:
        conn.close()
    return stats


# -- search -------------------------------------------------------------------


def parse_when(value: str | None, *, end: bool) -> float | None:
    """ISO date or datetime to epoch seconds. A bare date covers the whole day (UTC)."""
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            dt = datetime.combine(date.fromisoformat(text), time.min, tzinfo=timezone.utc)
            return (dt + timedelta(days=1)).timestamp() if end else dt.timestamp()
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid date: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fts_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def snippet(text: str, terms: list[str]) -> list[dict[str, Any]]:
    """Window of ``text`` around the first hit, as [{t, hit}] segments.

    Matching is on NFKC-lowercased text; spans map back to the original string
    through a per-character index so full-width input still highlights.
    """
    if not text:
        return []
    folded: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        piece = norm(ch)
        folded.append(piece)
        origin.extend([i] * len(piece))
    flat = "".join(folded)
    spans: list[tuple[int, int]] = []
    for term in terms:
        pos = flat.find(term) if term else -1
        while pos != -1:
            spans.append((origin[pos], origin[pos + len(term) - 1] + 1))
            pos = flat.find(term, pos + len(term))
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    first = merged[0] if merged else [0, 0]
    lo = max(0, first[0] - SNIPPET_RADIUS)
    hi = min(len(text), max(first[1], lo) + SNIPPET_RADIUS)
    out: list[dict[str, Any]] = []
    cursor = lo
    for s, e in merged:
        if s >= hi:
            break
        s = max(s, cursor)
        if s > cursor:
            out.append({"t": text[cursor:s], "hit": False})
        out.append({"t": text[s : min(e, hi)], "hit": True})
        cursor = min(e, hi)
    if cursor < hi:
        out.append({"t": text[cursor:hi], "hit": False})
    if lo > 0:
        out.insert(0, {"t": "…", "hit": False})
    if hi < len(text):
        out.append({"t": "…", "hit": False})
    return out


def search(
    db_path: Path,
    *,
    q: str = "",
    since: str | None = None,
    until: str | None = None,
    speaker: str | None = None,
    tag: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search archived sessions, newest first. Raises ValueError on a bad date."""
    terms = [t for t in norm(q).split() if t]
    since_ts = parse_when(since, end=False)
    until_ts = parse_when(until, end=True)
    spk = norm(speaker).strip() if speaker else ""
    tag_n = norm(tag).strip().lstrip("#") if tag else ""
    limit = max(1, min(int(limit), 100))

    where: list[str] = []
    args: list[Any] = []
    if since_ts is not None:
        where.append("s.started_ts >= ?")
        args.append(since_ts)
    if until_ts is not None:
        where.append("s.started_ts < ?")
        args.append(until_ts)
    if tag_n:
        where.append("EXISTS (SELECT 1 FROM tags t WHERE t.session_id = s.id AND lower(t.tag) = ?)")
        args.append(tag_n)

    conn = connect(db_path)
    try:
        if not (terms or spk):
            rows = conn.execute(
                "SELECT s.* FROM sessions s"
                + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY s.started_ts DESC LIMIT ?",
                [*args, limit],
            ).fetchall()
            results = [_session_result(conn, r, [], []) for r in rows]
            return _response(conn, q, results, len(results))

        if spk:
            where.append("l.spk_n = ?")
            args.append(spk)
        long_terms = [t for t in terms if len(t) >= 3]
        if long_terms:
            where.append("l.id IN (SELECT rowid FROM fts WHERE fts MATCH ?)")
            args.append(" AND ".join(_fts_phrase(t) for t in long_terms))
        for term in terms:
            where.append(
                "instr(l.ja_n || char(10) || l.en_n || char(10) || l.spk_n"
                " || char(10) || l.meta_n, ?) > 0"
            )
            args.append(term)
        hits = conn.execute(
            "SELECT l.* FROM lines l JOIN sessions s ON s.id = l.session_id"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY s.started_ts DESC, s.id, l.line_id",
            args,
        ).fetchall()

        by_session: dict[str, list[sqlite3.Row]] = {}
        for h in hits:
            by_session.setdefault(h["session_id"], []).append(h)
        results = []
        for sid, group in list(by_session.items())[:limit]:
            sess = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            line_hits = [h for h in group if h["line_id"] != META_LINE_ID and _in_text(h, terms)]
            results.append(_session_result(conn, sess, line_hits, terms))
        return _response(conn, q, results, len(by_session))
    finally:
        conn.close()


def _in_text(row: sqlite3.Row, terms: list[str]) -> bool:
    text = f"{row['ja_n']}\n{row['en_n']}\n{row['spk_n']}"
    return not terms or any(t in text for t in terms)


def _session_result(
    conn: sqlite3.Connection,
    sess: sqlite3.Row,
    line_hits: list[sqlite3.Row],
    terms: list[str],
) -> dict[str, Any]:
    tags = [
        r["tag"]
        for r in conn.execute(
            "SELECT tag FROM tags WHERE session_id=? ORDER BY tag COLLATE NOCASE",
            (sess["id"],),
        )
    ]
    matched_in: list[str] = []
    lines = []
    for h in line_hits[:MAX_LINES_PER_SESSION]:
        fields = [
            f
            for f, col in (("ja", "ja_n"), ("en", "en_n"), ("speaker", "spk_n"))
            if not terms or any(t in h[col] for t in terms)
        ]
        matched_in.extend(f for f in fields if f not in matched_in)
        lines.append(
            {
                "line_id": h["line_id"],
                "start": h["start"],
                "speaker": h["speaker"] or None,
                "ja": h["ja"],
                "en": h["en"],
                "ja_snippet": snippet(h["ja"], terms) if "ja" in fields else [],
                "en_snippet": snippet(h["en"], terms) if "en" in fields else [],
            }
        )
    if terms:
        if any(t in norm(sess["label"]) or t in sess["started_at"][:10] for t in terms):
            matched_in.append("title")
        if any(t in norm(tg) for t in terms for tg in tags):
            matched_in.append("tag")
    return {
        "id": sess["id"],
        "label": sess["label"],
        "started_at": sess["started_at"],
        "line_count": sess["line_count"],
        "tags": tags,
        "match_count": len(line_hits),
        "matched_in": matched_in,
        "lines": lines,
    }


def _response(
    conn: sqlite3.Connection, q: str, results: list[dict[str, Any]], total: int
) -> dict[str, Any]:
    speakers = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT speaker FROM lines WHERE speaker != '' ORDER BY speaker COLLATE NOCASE"
        )
    ]
    all_tags = [
        r[0] for r in conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag COLLATE NOCASE")
    ]
    return {
        "query": q,
        "total_sessions": total,
        "results": results,
        "speakers": speakers,
        "tags": all_tags,
    }
