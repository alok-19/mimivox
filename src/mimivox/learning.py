"""mimivox.learning — furigana, glosses and a vocabulary list for Japanese learners.

Off unless ``settings["learning_mode"]`` is true, so normal captions pay nothing.
Everything is local: SudachiPy (Apache-2.0) with its small dictionary tokenizes,
and a packaged subset of JMdict (EDRDG, CC-BY-SA 4.0, see THIRD_PARTY_NOTICES.md)
glosses. No network.

Data shape. A final or revised caption gains ``tokens``, a list whose surfaces
concatenate back to the JA text exactly::

    {"surface": "届き", "reading": "とどき", "base": "届く", "gloss": "to reach; to arrive"}

``reading`` (hiragana) is present only when ``surface`` holds kanji. ``base`` (the
dictionary form) is present only for content words. ``gloss`` is present only
with ``base`` and a lexicon hit.
"""

from __future__ import annotations

import csv
import hashlib
import gzip
import io
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_LEXICON_PATH = Path(__file__).parent / "lexicon" / "jmdict_common.tsv.gz"
_KANJI = re.compile("[㐀-鿿々〇豈-﫿]")
_JAPANESE = re.compile("[ぁ-ゖァ-ヺ㐀-鿿々〇豈-﫿]")
# Sudachi top-level part of speech kept as a vocabulary word.
_CONTENT_POS = frozenset({"名詞", "代名詞", "動詞", "形容詞", "形状詞", "副詞", "連体詞", "感動詞"})
_SKIP_POS2 = frozenset({"固有名詞", "数詞"})
# Every line has these and a learner knows them on day one; they only add noise.
_STOP_BASES = frozenset(
    {
        "する",
        "為る",
        "いる",
        "居る",
        "ある",
        "有る",
        "なる",
        "成る",
        "こと",
        "事",
        "もの",
        "物",
        "ない",
    }
)


def learning_enabled(settings: dict[str, Any] | None) -> bool:
    return bool(settings and settings.get("learning_mode"))


def to_hiragana(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    readings: tuple[str, ...]
    gloss: str


@lru_cache(maxsize=1)
def _lexicon() -> dict[str, list[Entry]]:
    """form (kanji spelling or kana reading) -> entries, most common first."""
    index: dict[str, list[Entry]] = {}
    with gzip.open(_LEXICON_PATH, "rt", encoding="utf-8") as fh:
        for line in fh:
            head, _, gloss = line.rstrip("\n").partition("\t")
            kanji, _, kana = head.partition("|")
            entry = Entry(tuple(kana.split("·")), gloss)
            # Kana also keys the entry: a word written in kana (まだ, よる) finds it.
            for form in [f for f in kanji.split("·") if f] + list(entry.readings):
                index.setdefault(form, []).append(entry)
    return index


def lookup(base: str, reading: str = "") -> Entry | None:
    """Best lexicon entry for a dictionary form, preferring one that reads ``reading``."""
    entries = _lexicon().get(base)
    if not entries:
        return None
    for entry in entries:
        if reading and reading in entry.readings:
            return entry
    return entries[0]


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------
_tok_lock = threading.Lock()


@lru_cache(maxsize=1)
def _tokenizer():
    from sudachipy import dictionary

    return dictionary.Dictionary(dict="small").tokenizer()


def _morphemes(text: str) -> list[tuple[str, str, str, tuple[str, ...]]]:
    from sudachipy import tokenizer

    with _tok_lock:
        return [
            (m.surface(), m.reading_form(), m.dictionary_form(), tuple(m.part_of_speech()))
            for m in _tokenizer().tokenize(text, tokenizer.Tokenizer.SplitMode.B)
        ]


@lru_cache(maxsize=4096)
def _reading_of(word: str) -> str:
    """Hiragana reading of a dictionary form (a surface's reading differs when inflected)."""
    return to_hiragana("".join(m[1] for m in _morphemes(word)))


def warm() -> None:
    """Load the tokenizer and lexicon so the first caption does not pay for them."""
    tokenize("今日は会議です。")
    _lexicon()


def tokenize(ja: str) -> list[dict[str, str]]:
    """Tokens for one JA caption (see the module docstring for the shape)."""
    out: list[dict[str, str]] = []
    for surface, reading_form, base, pos in _morphemes(ja):
        tok = {"surface": surface}
        reading = to_hiragana(reading_form)
        if _KANJI.search(surface) and reading and reading != surface:
            tok["reading"] = reading
        if pos[0] in _CONTENT_POS and pos[1] not in _SKIP_POS2 and base not in _STOP_BASES:
            tok["base"] = base
            entry = lookup(base, reading if base == surface else _reading_of(base))
            if entry:
                tok["gloss"] = entry.gloss
        out.append(tok)
    return out


def annotate(event: dict[str, Any], settings: dict[str, Any] | None) -> None:
    """Add ``tokens`` to a pipeline final/revision event in place when learning mode is on."""
    if not learning_enabled(settings) or event.get("kind") not in ("final", "revision"):
        return
    ja = event.get("ja")
    if not ja:
        return
    try:
        event["tokens"] = tokenize(ja)
    except Exception as exc:  # a learning aid must never cost a caption
        print(f"⚠️  learning tokens failed: {exc}")


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
def _reading_for(word: str) -> str:
    entry = lookup(word, _reading_of(word))
    if entry is None:
        return _reading_of(word)
    reading = _reading_of(word)
    return reading if reading in entry.readings else entry.readings[0]


def vocabulary(
    rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Words from a session in order of first appearance, with the line each came from.

    ``rows`` are bilingual rows (``export_session.pair_events``), so revisions are
    applied. Works on sessions recorded without learning mode: it tokenizes the
    JA text itself.
    """
    source_language = (settings or {}).get("source_lang") or "ja"
    target_language = (settings or {}).get("target_lang") or "en"
    if source_language == "en" and target_language == "ja":
        rows = [{**row, "ja": row.get("en") or "", "en": row.get("ja") or ""} for row in rows]
    elif source_language != "ja" or target_language != "en":
        rows = [{**row, "ja": "", "en": row.get("en") or ""} for row in rows]

    words: dict[str, dict[str, Any]] = {}
    for row in rows:
        ja = row.get("ja") or ""
        for tok in tokenize(ja) if ja else []:
            base, gloss = tok.get("base"), tok.get("gloss")
            if not (base and gloss):
                continue
            word = words.setdefault(
                base,
                {
                    "word": base,
                    "reading": _reading_for(base),
                    "gloss": gloss,
                    "count": 0,
                    "line_id": row.get("index"),
                    "example_ja": ja,
                    "example_en": row.get("en") or "",
                },
            )
            word["count"] += 1

    if settings:
        from mimivox.settings_store import get_effective_glossary

        reverse = source_language == "en" and target_language == "ja"
        if source_language != "ja" and not reverse:
            return list(words.values())
        for source, target in get_effective_glossary(settings).items():
            source_is_japanese = bool(_JAPANESE.search(source))
            target_is_japanese = bool(_JAPANESE.search(target))
            if not source or not target or (source_is_japanese and target_is_japanese):
                continue
            if reverse and target_is_japanese and not source_is_japanese:
                word_text, gloss = target, source
            elif source_is_japanese:
                word_text, gloss = source, target
            elif target_is_japanese:
                word_text, gloss = target, source
            else:
                word_text, gloss = source, target
            occurrences = 0
            example = None
            for row in rows:
                ja = row.get("ja") or ""
                en = row.get("en") or ""
                count = ja.casefold().count(word_text.casefold())
                if not count:
                    count = en.casefold().count(source.casefold())
                if not count:
                    count = en.casefold().count(target.casefold())
                if count:
                    occurrences += count
                    if example is None:
                        example = row
            if not occurrences or example is None:
                continue
            word = words.setdefault(
                word_text,
                {
                    "word": word_text,
                    "reading": _reading_for(word_text) if _JAPANESE.search(word_text) else "",
                    "gloss": gloss,
                    "count": 0,
                    "line_id": example.get("index"),
                    "example_ja": example.get("ja") or "",
                    "example_en": example.get("en") or "",
                },
            )
            word["gloss"] = gloss
            if word["count"] == 0:
                word["count"] = occurrences

    return list(words.values())


def render_csv(words: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["word", "reading", "meaning", "count", "example_ja", "example_en"])
    for w in words:
        writer.writerow(
            [w["word"], w["reading"], w["gloss"], w["count"], w["example_ja"], w["example_en"]]
        )
    return buf.getvalue()


def render_anki(words: list[dict[str, Any]], deck: str = "MimiVox") -> str:
    """Tab-separated text Anki imports directly (File > Import): one note per word."""
    lines = [
        "#separator:tab",
        "#html:false",
        f"#deck:{deck}",
        "#columns:Word\tReading\tMeaning\tExample\tTranslation\tTags",
        "#tags column:6",
    ]
    for w in words:
        cells = [w["word"], w["reading"], w["gloss"], w["example_ja"], w["example_en"], "mimivox"]
        lines.append("\t".join(c.replace("\t", " ").replace("\n", " ") for c in cells))
    return "\n".join(lines) + "\n"


def _flashcard_sides(word: dict[str, Any]) -> tuple[tuple[str, str], tuple[str, str]]:
    reading = word.get("reading") or ""
    front_ja = word["word"] + (f" ({reading})" if reading else "")
    back_en = word["gloss"]
    if word.get("example_ja"):
        back_en += f" | {word['example_ja']}"
    if word.get("example_en"):
        back_en += f" | {word['example_en']}"
    return (front_ja, back_en), (word["gloss"], front_ja)


def render_flashcards_csv(words: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["front", "back", "tags"])
    for word in words:
        for front, back in _flashcard_sides(word):
            writer.writerow([front, back, "mimivox"])
    return buf.getvalue()


def _flashcard_guid(word: dict[str, Any]) -> str:
    """Stable per word and reading, so re-importing updates a note instead of duplicating it."""
    key = f"{word['word']}\t{word.get('reading') or ''}"
    return "mimivox-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def render_flashcards_anki(words: list[dict[str, Any]], deck: str = "MimiVox") -> str:
    """One Anki 'Basic (and reversed card)' note per word: it generates both directions."""
    header = (
        "\n".join(
            [
                "#separator:tab",
                "#html:false",
                f"#deck:{deck}",
                "#notetype:Basic (and reversed card)",
                "#guid column:1",
                "#columns:GUID\tFront\tBack\tTags",
                "#tags column:4",
            ]
        )
        + "\n"
    )
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, delimiter="\t", lineterminator="\n")
    for word in words:
        front, back = _flashcard_sides(word)[0]
        writer.writerow([_flashcard_guid(word), front, back, "mimivox"])
    return header + buf.getvalue()


# format -> (file suffix, renderer)
VOCAB_FORMATS = {
    "csv": ("-vocabulary.csv", render_csv),
    "anki": ("-vocabulary-anki.txt", render_anki),
    "csv-cards": ("-flashcards.csv", render_flashcards_csv),
    "anki-cards": ("-flashcards-anki.txt", render_flashcards_anki),
}


def write_vocabulary(
    fmt: str, words: list[dict[str, Any]], session_id: str | None, out_dir: str | Path
) -> Path:
    suffix, render = VOCAB_FORMATS[fmt]
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id or 'nosession'}{suffix}"
    path.write_text(render(words), encoding="utf-8")
    return path
