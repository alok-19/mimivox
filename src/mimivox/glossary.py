"""mimivox.glossary — Glossary entry kinds, profiles, CSV import, ASR prompt.

One entry shape ``{source, target}``. The kind is derived, never stored:

* target contains Japanese -> ASR spelling fix (``てっこう`` -> ``結構``)
* otherwise                -> term (``部長`` -> ``department head``): the source
  is biased into Whisper's prompt and the target is substituted into the JA
  text before translation.
"""

from __future__ import annotations

from typing import Any

from mimivox.config import _JAPANESE_RE

# Whisper conditions on at most 224 prompt tokens; stay well inside that.
_PROMPT_BASE = "。、"
_PROMPT_MAX_CHARS = 160


def is_asr_fix(target: str) -> bool:
    return _JAPANESE_RE.search(target or "") is not None


def term_sources(glossary: dict[str, str]) -> list[str]:
    """Sources of term entries (Latin target), in glossary order."""
    return [s for s, t in glossary.items() if s and t and not is_asr_fix(t)]


def build_initial_prompt(glossary: dict[str, str] | None = None, pair: Any = None) -> str:
    """Whisper initial_prompt: neutral punctuation, then term sources.

    ``pair`` (a LanguagePair) picks the source language's base and separators;
    None keeps the JA prompt. Returns "" when a language has no base and no terms.
    """
    lang = pair.src if pair is not None else None
    base = lang.asr_prompt_base if lang else _PROMPT_BASE
    sep, end = (lang.prompt_sep, lang.prompt_end) if lang else ("、", "。")
    terms: list[str] = []
    used = len(base)
    for src in term_sources(glossary or {}):
        if used + len(src) + 1 > _PROMPT_MAX_CHARS:
            break
        terms.append(src)
        used += len(src) + 1
    return base + (sep.join(terms) + end if terms else "")


def entries_to_map(raw: Any) -> dict[str, str]:
    """Tolerantly turn ``[{source, target}]`` (or a dict) into ``{src: tgt}``."""
    out: dict[str, str] = {}
    items = (
        raw.items()
        if isinstance(raw, dict)
        else ((i.get("source"), i.get("target")) for i in raw or [] if isinstance(i, dict))
    )
    for src, tgt in items:
        if src is None or str(src).strip() == "":
            continue
        out[str(src).strip()] = "" if tgt is None else str(tgt).strip()
    return out


def parse_glossary_text(text: str) -> list[dict[str, str]]:
    """Parse pasted ``source,target`` (or tab-separated) lines into entries."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    entries: list[dict[str, str]] = []
    for line in lines:
        # Per line: tab wins so a TSV target may contain commas.
        row = line.split("\t" if "\t" in line else ",", 1)
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            continue
        entries.append({"source": row[0].strip(), "target": row[1].strip()})
    return entries


def find_profile(settings: dict[str, Any], name: str | None) -> dict[str, Any] | None:
    """Profile named ``name``; None when empty/unknown."""
    if not name:
        return None
    for profile in settings.get("glossary_profiles") or []:
        if isinstance(profile, dict) and profile.get("name") == name:
            return profile
    return None
