"""mimivox.meeting_notes — Summary / decisions / action items and "Ask the meeting".

Pure logic over final transcript lines. The LLM is injected as a ``Generator``
(``messages, max_tokens -> text``), so everything here is testable with a fake.

Every model reply is parsed and validated at the boundary: malformed JSON gets
one repair retry, and cited line ids that do not exist in the transcript are
dropped (an item left with no real citation is dropped too, so the notes never
carry a decision the transcript cannot back up).
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from mimivox.export_session import pair_events


class Generator(Protocol):
    def __call__(self, messages: list[dict[str, str]], *, max_tokens: int) -> str: ...


class NotesError(Exception):
    """The model never produced a usable reply (after the retry)."""


# Chunk budget in *estimated* tokens. Mixed JA/EN averages ~2 chars per token,
# so chars // 2 over-estimates a little, which is the safe direction.
CHUNK_TOKEN_BUDGET = 3000
NOTES_MAX_TOKENS = 1400
ASK_MAX_TOKENS = 400
MERGE_MAX_TOKENS = 700


def estimate_tokens(text: str) -> int:
    return len(text) // 2 + 1


# ---------------------------------------------------------------------------
# Transcript lines
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Line:
    id: int
    start: float
    ja: str
    en: str
    speaker: str | None = None

    def render(self) -> str:
        who = f"{self.speaker}: " if self.speaker else ""
        en = f" (EN: {self.en})" if self.en else ""
        return f"[{self.id}] {who}{self.ja}{en}"


def build_lines(events: list[dict[str, Any]]) -> list[Line]:
    """Final lines with stable ids (the event index), revisions applied."""
    lines: list[Line] = []
    for pos, row in enumerate(pair_events(events), start=1):
        if not (row.get("ja") or row.get("en")):
            continue
        idx = row.get("index")
        lines.append(
            Line(
                id=idx if isinstance(idx, int) else pos,
                start=float(row.get("start") or 0.0),
                ja=row.get("ja") or "",
                en=row.get("en") or "",
                speaker=row.get("speaker"),
            )
        )
    return lines


def chunk_lines(lines: list[Line], budget: int = CHUNK_TOKEN_BUDGET) -> list[list[Line]]:
    """Greedy split in order; a single oversized line still gets its own chunk."""
    chunks: list[list[Line]] = []
    cur: list[Line] = []
    used = 0
    for ln in lines:
        cost = estimate_tokens(ln.render()) + 1
        if cur and used + cost > budget:
            chunks.append(cur)
            cur, used = [], 0
        cur.append(ln)
        used += cost
    if cur:
        chunks.append(cur)
    return chunks


def _transcript(lines: list[Line]) -> str:
    return "\n".join(ln.render() for ln in lines)


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    text: str
    line_ids: list[int]
    text_ja: str = ""


@dataclass
class ActionItem:
    text: str
    line_ids: list[int]
    owner: str | None = None
    due: str | None = None
    text_ja: str = ""


@dataclass
class MeetingNotes:
    summary_en: str = ""
    title_en: str = ""
    summary_ja: str = ""
    decisions: list[Decision] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title_en": self.title_en,
            "summary_en": self.summary_en,
            "summary_ja": self.summary_ja,
            "decisions": [
                {"text": d.text, "text_ja": d.text_ja, "line_ids": d.line_ids}
                for d in self.decisions
            ],
            "action_items": [
                {
                    "owner": a.owner,
                    "text": a.text,
                    "text_ja": a.text_ja,
                    "due": a.due,
                    "line_ids": a.line_ids,
                }
                for a in self.action_items
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MeetingNotes:
        """Reader for already-validated (cached) notes."""
        return cls(
            title_en=str(data.get("title_en") or ""),
            summary_en=str(data.get("summary_en") or ""),
            summary_ja=str(data.get("summary_ja") or ""),
            decisions=[
                Decision(str(d["text"]), list(d.get("line_ids") or []), str(d.get("text_ja") or ""))
                for d in data.get("decisions") or []
            ],
            action_items=[
                ActionItem(
                    str(a["text"]),
                    list(a.get("line_ids") or []),
                    a.get("owner"),
                    a.get("due"),
                    str(a.get("text_ja") or ""),
                )
                for a in data.get("action_items") or []
            ],
        )


# ---------------------------------------------------------------------------
# Parsing / validation (the boundary)
# ---------------------------------------------------------------------------
def extract_json(raw: str) -> Any:
    """First balanced JSON object in ``raw`` (tolerates fences and chatter)."""
    text = raw.strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unterminated JSON object")


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clean_title(value: Any) -> str:
    """A model title, without the quotes or closing period models like to add."""
    return " ".join(_clean_str(value).split()).strip("\"'“”「」").rstrip(".。")


def _opt_str(value: Any) -> str | None:
    s = _clean_str(value)
    return None if s.lower() in ("", "null", "none", "unknown", "n/a") else s


def _ids(value: Any, valid: set[int]) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for v in value:
        if isinstance(v, str) and v.strip().isdigit():
            v = int(v.strip())
        if isinstance(v, int) and not isinstance(v, bool) and v in valid and v not in out:
            out.append(v)
    return out


def parse_notes(raw: str, valid_ids: set[int]) -> MeetingNotes:
    """Validate a model reply into MeetingNotes. Raises ValueError if unusable."""
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    summary_en, summary_ja = _clean_str(data.get("summary_en")), _clean_str(data.get("summary_ja"))
    if not summary_en and not summary_ja:
        raise ValueError("reply has no summary")
    for key in ("decisions", "action_items"):
        if not isinstance(data.get(key, []), list):
            raise ValueError(f"{key} must be a list")
    notes = MeetingNotes(
        summary_en=summary_en, summary_ja=summary_ja, title_en=_clean_title(data.get("title_en"))
    )
    for d in data.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        ids, text = _ids(d.get("line_ids"), valid_ids), _clean_str(d.get("text"))
        if text and ids:
            notes.decisions.append(Decision(text, ids, _clean_str(d.get("text_ja"))))
    for a in data.get("action_items") or []:
        if not isinstance(a, dict):
            continue
        ids, text = _ids(a.get("line_ids"), valid_ids), _clean_str(a.get("text"))
        if text and ids:
            notes.action_items.append(
                ActionItem(
                    text,
                    ids,
                    _opt_str(a.get("owner")),
                    _opt_str(a.get("due")),
                    _clean_str(a.get("text_ja")),
                )
            )
    return notes


def _call_validated(
    gen: Generator,
    messages: list[dict[str, str]],
    parse: Callable[[str], Any],
    max_tokens: int,
) -> Any:
    """One generation, one repair retry on invalid output, then a clear error."""
    raw = gen(messages, max_tokens=max_tokens)
    try:
        return parse(raw)
    except (ValueError, KeyError, TypeError) as first:
        retry = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": f"That reply was invalid ({first}). Reply again with ONLY the "
                "corrected JSON object, nothing else.",
            },
        ]
        raw = gen(retry, max_tokens=max_tokens)
        try:
            return parse(raw)
        except (ValueError, KeyError, TypeError) as second:
            raise NotesError(f"The model returned invalid JSON twice ({second})") from second


# ---------------------------------------------------------------------------
# Notes: map over chunks, merge
# ---------------------------------------------------------------------------
_TITLE_RULE = (
    "title_en is a noun phrase naming what the meeting was about, like a document title "
    '(e.g. "Q3 budget and hiring freeze"): no verb, no closing period, and never open with '
    '"The group", "The speakers", "The team" or "Discussion of".'
)

_NOTES_SYSTEM = (
    "You write meeting notes from a bilingual (Japanese/English) transcript. Each line is "
    "`[id] speaker: Japanese (EN: English)`. Use ONLY what the transcript says; never invent "
    "facts, names, numbers or dates.\n"
    "Reply with ONE JSON object and nothing else:\n"
    '{"title_en": "3-7 word English title naming the topic",\n'
    ' "summary_en": "2-4 sentence English summary",\n'
    ' "summary_ja": "the same summary in natural Japanese",\n'
    ' "decisions": [{"text": "English", "text_ja": "Japanese", "line_ids": [ids]}],\n'
    ' "action_items": [{"owner": "name or null", "text": "English", "text_ja": "Japanese", '
    '"due": "deadline in the speakers\' words, or null", "line_ids": [ids]}]}\n'
    "Rules: a decision is something the speakers explicitly agreed or settled; a postponed or "
    "open question is not a decision. An action item is a task someone commits to or is asked "
    "to do. Both must cite the line ids that state "
    "them. If there are none, use an empty list. owner and due are null unless said. " + _TITLE_RULE
)

_MERGE_SYSTEM = (
    "You combine partial summaries of consecutive parts of one meeting into a single summary. "
    "Use only the given text. Reply with ONE JSON object and nothing else:\n"
    '{"title_en": "3-7 word English title for the whole meeting", '
    '"summary_en": "3-5 sentence English summary", "summary_ja": "the same in natural Japanese"}\n'
    + _TITLE_RULE
)


def _norm(text: str) -> str:
    return re.sub(r"\W+", "", text.lower())


def _notes_chunk(gen: Generator, chunk: list[Line]) -> MeetingNotes:
    valid = {ln.id for ln in chunk}
    messages = [
        {"role": "system", "content": _NOTES_SYSTEM},
        {"role": "user", "content": "Transcript:\n" + _transcript(chunk)},
    ]
    return _call_validated(gen, messages, lambda r: parse_notes(r, valid), NOTES_MAX_TOKENS)


def _parse_summaries(raw: str) -> MeetingNotes:
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    en, ja = _clean_str(data.get("summary_en")), _clean_str(data.get("summary_ja"))
    if not en and not ja:
        raise ValueError("reply has no summary")
    return MeetingNotes(summary_en=en, summary_ja=ja, title_en=_clean_title(data.get("title_en")))


def _merge_summaries(gen: Generator, parts: list[MeetingNotes]) -> MeetingNotes:
    """Reduce chunk summaries; recurse in groups if they exceed the budget."""
    while len(parts) > 1:
        groups: list[list[MeetingNotes]] = [[]]
        used = 0
        for p in parts:
            cost = estimate_tokens(p.summary_en + p.summary_ja) + 1
            if groups[-1] and used + cost > CHUNK_TOKEN_BUDGET:
                groups.append([])
                used = 0
            groups[-1].append(p)
            used += cost
        if len(groups) >= len(parts):  # every part alone is over budget: cannot shrink
            groups = [parts]
        merged: list[MeetingNotes] = []
        for g in groups:
            if len(g) == 1:
                merged.append(g[0])
                continue
            body = "\n\n".join(
                f"Part {i}:\nEN: {p.summary_en}\nJA: {p.summary_ja}" for i, p in enumerate(g, 1)
            )
            merged.append(
                _call_validated(
                    gen,
                    [
                        {"role": "system", "content": _MERGE_SYSTEM},
                        {"role": "user", "content": body},
                    ],
                    _parse_summaries,
                    MERGE_MAX_TOKENS,
                )
            )
        parts = merged
    return parts[0]


def generate_notes(
    gen: Generator,
    lines: list[Line],
    *,
    budget: int = CHUNK_TOKEN_BUDGET,
) -> MeetingNotes:
    if not lines:
        raise NotesError("No transcript lines to summarize")
    parts = [_notes_chunk(gen, c) for c in chunk_lines(lines, budget)]
    if len(parts) == 1:
        return parts[0]
    # Decisions/actions are kept verbatim from their chunk (with their citations),
    # deduplicated; only the prose summary is re-written by the model.
    summary = _merge_summaries(gen, parts)
    out = MeetingNotes(
        summary_en=summary.summary_en, summary_ja=summary.summary_ja, title_en=summary.title_en
    )
    seen_d: set[str] = set()
    seen_a: set[str] = set()
    for p in parts:
        for d in p.decisions:
            if _norm(d.text) not in seen_d:
                seen_d.add(_norm(d.text))
                out.decisions.append(d)
        for a in p.action_items:
            if _norm(a.text) not in seen_a:
                seen_a.add(_norm(a.text))
                out.action_items.append(a)
    return out


# ---------------------------------------------------------------------------
# Ask the meeting
# ---------------------------------------------------------------------------
@dataclass
class Phrase:
    """How to ask a question aloud in Japanese: the sentence and its romaji."""

    ja: str
    romaji: str

    def to_dict(self) -> dict[str, str]:
        return {"ja": self.ja, "romaji": self.romaji}


@dataclass
class Answer:
    answer: str
    line_ids: list[int]
    # Set when the transcript has no answer, so the user can ask the room instead.
    ask_in_japanese: Phrase | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"answer": self.answer, "line_ids": self.line_ids}
        if self.ask_in_japanese is not None:
            out["ask_in_japanese"] = self.ask_in_japanese.to_dict()
        return out


_ASK_SYSTEM = (
    "You answer a question about a meeting using ONLY the transcript lines given. Each line "
    "is `[id] speaker: Japanese (EN: English)`. Answer in the language of the question, in one "
    "to three sentences, and cite the ids of the lines your answer rests on. A question put to "
    "a named person is usually answered by the next lines. If the transcript "
    "does not say, set answer to a short statement that it was not discussed, and line_ids to "
    "[]. Reply with ONE JSON object and nothing else:\n"
    '{"answer": "...", "line_ids": [ids]}'
)

_ASK_REDUCE_SYSTEM = (
    "You are given candidate answers to one question about a meeting, each from a different "
    "part of the transcript, with the lines they cite. Write the single best answer using only "
    "those lines, in the language of the question, and cite line ids. Reply with ONE JSON "
    'object and nothing else: {"answer": "...", "line_ids": [ids]}'
)


_PHRASE_SYSTEM = (
    "The user is in a live meeting with Japanese colleagues and wants to ask the room a "
    "question out loud. Rewrite their question as ONE natural spoken Japanese sentence in a "
    "business-casual register: polite です/ます forms, as you would say to colleagues, not "
    "stiff keigo (avoid でございます, いただけますでしょうか, 恐れ入りますが). Keep names "
    "and numbers as given. Keep it short and grammatical. Examples: "
    '"did we decide on the vendor yet" -> {"ja": "ベンダーはもう決まりましたか？"}; '
    '"what\'s the deadline" -> {"ja": "締め切りはいつですか？"}. '
    "Reply with ONE JSON object and nothing else: "
    '{"ja": "..."}'
)
PHRASE_MAX_TOKENS = 120
_KANA_OR_KANJI = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def _parse_phrase(raw: str) -> str:
    data = extract_json(raw)
    ja = _clean_str(data.get("ja")) if isinstance(data, dict) else ""
    if not _KANA_OR_KANJI.search(ja):
        raise ValueError("ja is not a Japanese sentence")
    return ja


def phrase_in_japanese(gen: Generator, question: str) -> Phrase:
    """The question as a business-casual Japanese sentence, with dictionary romaji."""
    from mimivox.romaji import to_romaji

    messages = [
        {"role": "system", "content": _PHRASE_SYSTEM},
        {"role": "user", "content": question},
    ]
    ja = _call_validated(gen, messages, _parse_phrase, PHRASE_MAX_TOKENS)
    return Phrase(ja, to_romaji(ja))


def _parse_answer(raw: str, valid: set[int]) -> Answer:
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    text = _clean_str(data.get("answer"))
    if not text:
        raise ValueError("reply has no answer")
    return Answer(text, _ids(data.get("line_ids"), valid))


def _ask_once(gen: Generator, question: str, lines: list[Line]) -> Answer:
    valid = {ln.id for ln in lines}
    messages = [
        {"role": "system", "content": _ASK_SYSTEM},
        {"role": "user", "content": f"Transcript:\n{_transcript(lines)}\n\nQuestion: {question}"},
    ]
    return _call_validated(gen, messages, lambda r: _parse_answer(r, valid), ASK_MAX_TOKENS)


def answer_question(
    gen: Generator,
    lines: list[Line],
    question: str,
    *,
    budget: int = CHUNK_TOKEN_BUDGET,
    suggest_phrase: bool = False,
) -> Answer:
    """Answer from the transcript; with ``suggest_phrase``, an unanswered question also
    comes back phrased in Japanese so the user can put it to the room."""
    answer = _answer(gen, lines, question.strip(), budget)
    if suggest_phrase and not answer.line_ids:
        with contextlib.suppress(NotesError):  # without a phrase, the answer still stands
            answer.ask_in_japanese = phrase_in_japanese(gen, question.strip())
    return answer


def _answer(gen: Generator, lines: list[Line], question: str, budget: int) -> Answer:
    if not question:
        raise NotesError("Question is empty")
    if not lines:
        raise NotesError("No transcript lines yet")
    chunks = chunk_lines(lines, budget)
    if len(chunks) == 1:
        return _ask_once(gen, question, lines)
    # Long meeting: ask each part, then reduce the answers that cite something.
    by_id = {ln.id: ln for ln in lines}
    partials = [a for a in (_ask_once(gen, question, c) for c in chunks) if a.line_ids]
    if not partials:
        return Answer("This was not discussed in the transcript.", [])
    cited = sorted({i for a in partials for i in a.line_ids})
    body = (
        "Candidate answers:\n"
        + "\n".join(f"- {a.answer} (lines {a.line_ids})" for a in partials)
        + f"\n\nCited lines:\n{_transcript([by_id[i] for i in cited])}\n\nQuestion: {question}"
    )
    valid = set(cited)
    return _call_validated(
        gen,
        [{"role": "system", "content": _ASK_REDUCE_SYSTEM}, {"role": "user", "content": body}],
        lambda r: _parse_answer(r, valid),
        ASK_MAX_TOKENS,
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def render_notes_markdown(notes: MeetingNotes, positions: dict[int, int] | None = None) -> str:
    """Notes as Markdown. ``positions`` maps line id -> row number to cite."""

    def cite(ids: list[int]) -> str:
        nums = [positions.get(i, i) if positions else i for i in ids]
        return f" _(lines {', '.join(str(n) for n in nums)})_" if nums else ""

    def both(en: str, ja: str) -> str:
        return f"{en} / {ja}" if ja else en

    out = [
        "## Summary",
        "",
        notes.summary_en or "—",
        "",
        "### 要約",
        "",
        notes.summary_ja or "—",
        "",
    ]
    out += ["## Decisions", ""]
    out += [f"- {both(d.text, d.text_ja)}{cite(d.line_ids)}" for d in notes.decisions] or [
        "_None._"
    ]
    out += ["", "## Action items", ""]
    for a in notes.action_items:
        meta = ", ".join(
            x
            for x in (f"owner: {a.owner}" if a.owner else "", f"due: {a.due}" if a.due else "")
            if x
        )
        out.append(f"- {both(a.text, a.text_ja)}{f' ({meta})' if meta else ''}{cite(a.line_ids)}")
    if not notes.action_items:
        out.append("_None._")
    out.append("")
    return "\n".join(out)
