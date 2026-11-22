import pytest

from mimivox.romaji import kana_to_romaji, to_romaji


@pytest.mark.parametrize(
    ("ja", "romaji"),
    [
        ("来週の会議はいつですか？", "raishuu no kaigi wa itsu desu ka?"),
        ("田中さんへの返事を送っていただけますか。", "tanaka san e no henji o okutte itadakemasu ka."),
        ("締め切りについてちょっと確認してもいいですか？", "shimekiri ni tsuite chotto kakunin shite mo ii desu ka?"),
        ("原因は何でしょうか", "gen'in wa nan deshou ka"),
        ("金曜日までに終わりそうですか？", "kin'youbi made ni owarisou desu ka?"),
        ("リリース日を私に教えてもらえますか？", "ririisubi o watashi ni oshiete moraemasu ka?"),
        ("ちょっと待ってください、それは決まりましたか？", "chotto matte kudasai, sore wa kimarimashita ka?"),
    ],
)  # fmt: skip
def test_sentences_read_with_particles_and_verb_endings(ja, romaji):
    assert to_romaji(ja) == romaji


@pytest.mark.parametrize(
    ("kana", "romaji"),
    [("コーヒー", "koohii"), ("まっちゃ", "matcha"), ("きって", "kitte"), ("しんよう", "shin'you"),
     ("きょう", "kyou"), ("ふぁいる", "fairu"), ("さんぽ", "sanpo")],
)  # fmt: skip
def test_kana_spelling(kana, romaji):
    assert kana_to_romaji(kana) == romaji
