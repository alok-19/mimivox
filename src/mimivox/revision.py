"""mimivox.revision — detect a Japanese sentence the VAD split mid-sentence.

The VAD closes an utterance after a silence (``VAD_MIN_SILENCE_MS``), so a
speaker pausing inside a sentence yields two finals that each translate as a
fragment. ``is_split_continuation`` decides, from the two JA texts and the
silence between them, whether the second continues the first.
"""

from __future__ import annotations

from mimivox.config import REVISION_MAX_GAP_S, REVISION_MAX_JOINED_CHARS

_TERMINAL = "。？！?!"
_COMMA = "、，,"
_CLOSERS = "」』）)\"'"
# Whisper usually writes 。 where it hears a sentence end, so after 。 only
# conjunctions that cannot close a sentence count. て/で/から/し are left out:
# "待って。" and "忙しいから。" are complete replies.
_MIN_UNPUNCTUATED_CHARS = 5
_STRONG_CONJUNCTIONS = ("けれども", "けれど", "けど", "ので", "が")


def _tail(text: str) -> str:
    return text.strip().rstrip(_CLOSERS)


def ends_mid_sentence(ja: str) -> bool:
    """True when ``ja`` looks like it stops before the sentence is finished."""
    t = _tail(ja)
    if not t:
        return False
    last = t[-1]
    if last in _COMMA:
        return True
    if last in "？！?!":
        return False
    if last == "。":
        return t[:-1].endswith(_STRONG_CONJUNCTIONS)
    # No closing punctuation at all: Whisper cut off mid-clause. A bare filler
    # ("はい", "えーと") is not a clause.
    return len(t) >= _MIN_UNPUNCTUATED_CHARS


def is_split_continuation(prev_ja: str, next_ja: str, gap_s: float) -> bool:
    """True when ``next_ja`` continues ``prev_ja`` across a VAD split.

    ``gap_s`` is silence between the end of ``prev_ja`` and the start of
    ``next_ja``. Merging also stops at ``REVISION_MAX_JOINED_CHARS`` so a long
    run-on is not retranslated over and over.
    """
    prev_ja, next_ja = prev_ja.strip(), next_ja.strip()
    if not prev_ja or not next_ja:
        return False
    if gap_s > REVISION_MAX_GAP_S:
        return False
    if len(prev_ja) + len(next_ja) > REVISION_MAX_JOINED_CHARS:
        return False
    return ends_mid_sentence(prev_ja)
