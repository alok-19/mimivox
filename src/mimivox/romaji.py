"""mimivox.romaji — Hepburn romaji for a Japanese sentence, from SudachiPy readings.

Readings come from the dictionary, not the LLM, so particles and kanji read right:
「来週の会議はいつですか？」 -> "raishuu no kaigi wa itsu desu ka?". Long vowels are
spelled out (``ou``, ``ii``) rather than with macrons, so the result can be typed.
"""

from __future__ import annotations

from mimivox.learning import _tok_lock, _tokenizer, to_hiragana

_DIGRAPHS = {
    "きゃ": "kya", "きゅ": "kyu", "きょ": "kyo", "ぎゃ": "gya", "ぎゅ": "gyu", "ぎょ": "gyo",
    "しゃ": "sha", "しゅ": "shu", "しょ": "sho", "しぇ": "she", "じゃ": "ja", "じゅ": "ju",
    "じょ": "jo", "じぇ": "je", "ちゃ": "cha", "ちゅ": "chu", "ちょ": "cho", "ちぇ": "che",
    "ぢゃ": "ja", "ぢゅ": "ju", "ぢょ": "jo", "にゃ": "nya", "にゅ": "nyu", "にょ": "nyo",
    "ひゃ": "hya", "ひゅ": "hyu", "ひょ": "hyo", "びゃ": "bya", "びゅ": "byu", "びょ": "byo",
    "ぴゃ": "pya", "ぴゅ": "pyu", "ぴょ": "pyo", "みゃ": "mya", "みゅ": "myu", "みょ": "myo",
    "りゃ": "rya", "りゅ": "ryu", "りょ": "ryo", "ふぁ": "fa", "ふぃ": "fi", "ふぇ": "fe",
    "ふぉ": "fo", "てぃ": "ti", "でぃ": "di", "とぅ": "tu", "どぅ": "du", "うぃ": "wi",
    "うぇ": "we", "うぉ": "wo", "ゔぁ": "va", "ゔぃ": "vi", "ゔぇ": "ve", "ゔぉ": "vo",
}  # fmt: skip

_MONO = dict(
    zip(
        "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめも"
        "やゆよらりるれろわゐゑをがぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽゔ"
        "ぁぃぅぇぉゃゅょゎ",
        "a i u e o ka ki ku ke ko sa shi su se so ta chi tsu te to na ni nu ne no "  # noqa: SIM905
        "ha hi fu he ho ma mi mu me mo ya yu yo ra ri ru re ro wa i e o "
        "ga gi gu ge go za ji zu ze zo da ji zu de do ba bi bu be bo pa pi pu pe po vu "
        "a i u e o ya yu yo wa".split(),
        strict=True,
    )
)

_PUNCT = {"。": ".", "、": ",", "？": "?", "！": "!", "「": '"', "」": '"', "・": " ", "　": " "}
_PARTICLES = {"は": "wa", "へ": "e", "を": "o"}
# Readings the dictionary gives in a register too formal for conversation.
_SPOKEN = {"私": "わたし"}
_VERBAL = {"動詞", "助動詞"}
_COPULA = {"です", "だ", "でし", "だっ", "でしょ", "だろ"}


def _attaches(surface: str, pos: tuple[str, ...], prev: tuple[str, ...] | None) -> bool:
    """Whether a morpheme is written onto the previous word ("okutte", "arimashita").

    Particles and the copula stand alone ("kaigi wa", "ii desu"), as in learner Hepburn.
    """
    if prev is None or prev[0] not in _VERBAL:
        return False
    if pos[0] == "助動詞":
        return surface not in _COPULA
    return (pos[0] == "助詞" and pos[1] == "接続助詞") or pos[1] == "助動詞語幹"


def kana_to_romaji(kana: str) -> str:
    """Hepburn for a run of hiragana or katakana; other characters pass through."""
    text = to_hiragana(kana)
    out: list[str] = []
    i = 0
    double_next = False
    while i < len(text):
        pair, ch = text[i : i + 2], text[i]
        if ch == "っ":
            double_next = True
            i += 1
            continue
        if ch == "ー":
            out.append(next((c for c in reversed("".join(out)) if c in "aeiou"), ""))
            i += 1
            continue
        if ch == "ん":
            follows = _MONO.get(text[i + 1 : i + 2], "")
            out.append("n'" if follows[:1] in ("a", "i", "u", "e", "o", "y") and follows else "n")
            i += 1
            continue
        if pair in _DIGRAPHS:
            syl, i = _DIGRAPHS[pair], i + 2
        elif ch in _MONO:
            syl, i = _MONO[ch], i + 1
        else:
            syl, i = _PUNCT.get(ch, ch), i + 1
        if double_next and syl[:1].isalpha() and syl[0] not in "aeiou":
            syl = ("t" if syl.startswith("ch") else syl[0]) + syl
        double_next = False
        out.append(syl)
    return "".join(out)


def _tokens(ja: str) -> list[tuple[str, str, tuple[str, ...]]]:
    from sudachipy import tokenizer

    with _tok_lock:
        return [
            (m.surface(), m.reading_form(), tuple(m.part_of_speech()))
            for m in _tokenizer().tokenize(ja, tokenizer.Tokenizer.SplitMode.C)
        ]


def to_romaji(ja: str) -> str:
    """Spaced Hepburn romaji for a Japanese sentence."""
    words: list[str] = []  # kana per word, punctuation already romanized
    prev: tuple[str, ...] | None = None
    for surface, reading, pos in _tokens(ja):
        if pos[0] == "空白":
            continue
        if pos[0] == "補助記号":
            mark = _PUNCT.get(surface, surface).strip()
            if mark and words:
                words[-1] = kana_to_romaji(words[-1]) + mark if words[-1] else mark
                words.append("")  # the next word starts fresh
            prev = None
            continue
        kana = _SPOKEN.get(surface) or to_hiragana(reading or surface)
        if surface == "日" and prev and prev[0] == "名詞" and words and words[-1]:
            words[-1] += "び"  # rendaku: 金曜日 kin'youbi, リリース日 ririisubi
            prev = pos
            continue
        if pos[0] == "助詞" and kana in _PARTICLES:
            kana = _PARTICLES[kana]
        if words and words[-1] and _attaches(surface, pos, prev):
            words[-1] += kana
        else:
            words.append(kana)
        prev = pos
    return " ".join(kana_to_romaji(w) for w in words if w)
