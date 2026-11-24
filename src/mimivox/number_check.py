"""mimivox.number_check — catch translations whose numbers are not in the source.

CAT misreads bare numerals: "約7000の島々" came back as "about 70,000 islands",
while "約7,000" and "約七千" translate correctly. ``unsupported_numbers`` lists
the English numbers that no Japanese number accounts for, so the caller can
retry and, failing that, flag the line instead of showing a wrong figure.
"""

from __future__ import annotations

import re
import unicodedata

_KANJI_DIGITS = {c: i for i, c in enumerate("〇一二三四五六七八九")}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}
_KANJI_MYRIADS = {"万": 10**4, "億": 10**8, "兆": 10**12}
# A counter right after a number that scales it in English ("2割" is "20 percent").
_JA_COUNTER_SCALES = {"割": 10}
_EN_SCALES = {
    "hundred": 100,
    "thousand": 1000,
    "million": 10**6,
    "billion": 10**9,
    "trillion": 10**12,
}

_ARABIC = r"\d+(?:,\d{3})*(?:\.\d+)?"
_JA_NUMBER_RE = re.compile(rf"(?:{_ARABIC}|[〇一二三四五六七八九十百千万億兆])+")
_EN_NUMBER_RE = re.compile(
    rf"(?<![\w.])(?:(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})(?!\d)"
    rf"|(?P<number>{_ARABIC})(?:\s+(?P<scale>{'|'.join(_EN_SCALES)})\b)?)",
    re.IGNORECASE,
)
# A bare integer of 4+ digits that is not a year: "7000" but not "1789年".
_UNGROUPED_RE = re.compile(r"(?<![\d,.])(\d{4,})(?![\d,.]|\s*年)")


def _value(arabic: str) -> float:
    return float(arabic.replace(",", ""))


def _parse_ja(token: str) -> float:
    """Value of a numeral run mixing Arabic digits and kanji ("7千", "三百五十")."""
    total = section = digit = 0.0
    for part in re.findall(rf"{_ARABIC}|.", token):
        if part[0].isdigit():
            digit = _value(part)
        elif part in _KANJI_DIGITS:
            digit = digit * 10 + _KANJI_DIGITS[part]  # "二〇二四" reads digit by digit
        elif part in _KANJI_UNITS:
            section += (digit or 1) * _KANJI_UNITS[part]
            digit = 0.0
        else:
            total += ((section + digit) or 1) * _KANJI_MYRIADS[part]
            section = digit = 0.0
    return total + section + digit


def ja_numbers(ja: str) -> set[float]:
    """Every value a Japanese line accounts for: each numeral run, its Arabic
    parts, and the run scaled by a counter such as 割."""
    text = unicodedata.normalize("NFKC", ja)
    values: set[float] = set()
    for m in _JA_NUMBER_RE.finditer(text):
        run = {_parse_ja(m.group()), *(_value(p) for p in re.findall(_ARABIC, m.group()))}
        scale = _JA_COUNTER_SCALES.get(text[m.end() : m.end() + 1], 1)
        values |= run | {v * scale for v in run}
    return values


def _clock_minute_missing(ja: str, allowed: set[float], minute: str) -> bool:
    """A clock time's minutes: ":00" has no Japanese counterpart, and ":30" may be 半."""
    if minute == "00":
        return False
    if minute == "30" and "時半" in unicodedata.normalize("NFKC", ja):
        return False
    return float(minute) not in allowed


def unsupported_numbers(ja: str, en: str) -> list[str]:
    """English numbers that match no number in the Japanese line, in order."""
    allowed = ja_numbers(ja)
    missing: list[str] = []
    for m in _EN_NUMBER_RE.finditer(en):
        if m.group("hour"):
            if float(m.group("hour")) not in allowed:
                missing.append(m.group("hour"))
            if _clock_minute_missing(ja, allowed, m.group("minute")):
                missing.append(m.group("minute"))
            continue
        value = _value(m.group("number"))
        scale = m.group("scale")
        scaled = value * _EN_SCALES[scale.lower()] if scale else value
        if value not in allowed and scaled not in allowed:
            missing.append(m.group())
    return missing


def group_digits(ja: str) -> str:
    """Write bare 4+ digit integers with thousands separators ("7000" -> "7,000").

    Years ("1789年") keep their form: CAT reads them correctly, and "1,789年"
    would no longer read as a year.
    """
    return _UNGROUPED_RE.sub(lambda m: f"{int(m.group(1)):,}", ja)
