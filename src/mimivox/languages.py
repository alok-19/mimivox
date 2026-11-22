"""mimivox.languages — which language a session hears and which it captions in.

One immutable ``LanguagePair`` is the whole shape: settings persist its two
codes (``source_lang`` / ``target_lang``), session start resolves it once, and
every stage reads its facts from here instead of branching on a language code.

``ja-en`` is the original product path (CAT-Translate, JA-specific repair and
revision heuristics). Every other supported pair translates with the local
Qwen instruct model that meeting notes already ship (see ``pair_translate``).

Wire format note: caption events and log rows keep their ``ja`` / ``en`` keys.
For a pair, ``ja`` carries the *source* text and ``en`` the *translation*.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any


@dataclasses.dataclass(frozen=True)
class Language:
    code: str  # ISO 639-1; also Whisper's language code
    name: str  # English name, used in translation prompts
    native: str  # shown in pickers
    terminal: str  # sentence-final mark appended to unpunctuated finals
    asr_prompt_base: str  # neutral Whisper initial_prompt before glossary terms
    prompt_sep: str  # joins glossary terms inside the Whisper prompt
    prompt_end: str


LANGUAGES: dict[str, Language] = {
    "ja": Language("ja", "Japanese", "日本語", "。", "。、", "、", "。"),
    "zh": Language("zh", "Chinese", "中文", "。", "", "，", "。"),
    "ko": Language("ko", "Korean", "한국어", ".", "", ", ", "."),
    "en": Language("en", "English", "English", ".", "", ", ", "."),
}

_SUPPORTED: tuple[tuple[str, str], ...] = (("ja", "en"), ("zh", "en"), ("ko", "en"), ("en", "ja"))


class UnsupportedPair(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class LanguagePair:
    source: str
    target: str

    def __post_init__(self) -> None:
        if (self.source, self.target) not in _SUPPORTED:
            raise UnsupportedPair(f"Unsupported language pair: {self.source}->{self.target}")

    @property
    def code(self) -> str:
        return f"{self.source}-{self.target}"

    @property
    def src(self) -> Language:
        return LANGUAGES[self.source]

    @property
    def tgt(self) -> Language:
        return LANGUAGES[self.target]

    @property
    def whisper_language(self) -> str:
        return self.source

    @property
    def uses_cat(self) -> bool:
        """The original JA->EN path: CAT-Translate and its JA-specific repair."""
        return self.code == "ja-en"

    @property
    def label(self) -> str:
        return f"{self.src.native} → {self.tgt.native}"


SUPPORTED_PAIRS: tuple[LanguagePair, ...] = tuple(LanguagePair(s, t) for s, t in _SUPPORTED)
DEFAULT_PAIR = LanguagePair("ja", "en")


def parse_pair(source: Any, target: Any) -> LanguagePair:
    """Strict: raises ``UnsupportedPair`` on anything outside the supported set."""
    return LanguagePair(str(source or "").strip().lower(), str(target or "").strip().lower())


def pair_from_settings(settings: Mapping[str, Any] | None) -> LanguagePair:
    """Tolerant: a missing or unsupported stored value falls back to JA->EN."""
    settings = settings or {}
    try:
        return parse_pair(settings.get("source_lang") or "ja", settings.get("target_lang") or "en")
    except UnsupportedPair:
        return DEFAULT_PAIR


def resolve_pair(
    settings: Mapping[str, Any] | None, body: Mapping[str, Any] | None = None
) -> LanguagePair:
    """Session start: the request's source_lang/target_lang win over settings.

    Raises ``UnsupportedPair`` for an explicit unsupported value.
    """
    settings, body = settings or {}, body or {}
    src = body.get("source_lang") or settings.get("source_lang") or "ja"
    tgt = body.get("target_lang") or settings.get("target_lang") or "en"
    return parse_pair(src, tgt)


def pair_catalog() -> list[dict[str, str]]:
    """What pickers show: one row per supported pair."""
    return [
        {"source": p.source, "target": p.target, "code": p.code, "label": p.label}
        for p in SUPPORTED_PAIRS
    ]
