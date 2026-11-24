import pytest

from mimivox.number_check import group_digits, unsupported_numbers


@pytest.mark.parametrize(
    ("ja", "en"),
    [
        ("日本には約7000の島々があります。", "Japan has about 7,000 islands"),
        ("日本には約七千の島々があります。", "Japan has about 7,000 islands"),
        ("先月より二割高いですね。", "The price is 20 percent higher than last month"),
        ("世界で7番目に大きい島です。", "It is the 7th largest island in the world"),
        ("人口は1万2000人です。", "The population is 12,000"),
        ("予算は700万円です。", "The budget is 7 million yen"),
        ("1789年10月6日のことです。", "It happened on October 6, 1789"),
        ("円周率は３．１４です。", "Pi is 3.14"),
        ("明日話すつもりです。", "I will talk about it tomorrow"),
        ("最終列車は12時くらいです", "The last train departs at about 12:00"),
        ("最終列車は12時半です", "The last train departs at 12:30"),
        ("7時15分に始まります", "It starts at 7:15"),
    ],
)
def test_numbers_the_japanese_accounts_for_pass(ja, en):
    assert unsupported_numbers(ja, en) == []


@pytest.mark.parametrize(
    ("ja", "en", "wrong"),
    [
        (
            "日本には約7000の島々があり、世界で7番目に大きい島です。",
            "Japan has about 70,000 islands, the seventh largest in the world",
            ["70,000"],
        ),
        ("最終列車は12時です", "The last train departs at 12:30", ["30"]),
        ("最終列車は11時です", "The last train departs at 12:00", ["12"]),
        (
            "17、89年10月6日、国王ルイ16世は戻されました。",
            "Year 1989 – On October 6, 1863, King Louis 16 was returned",
            ["1989", "1863"],
        ),
    ],
)
def test_numbers_missing_from_the_japanese_are_reported(ja, en, wrong):
    assert unsupported_numbers(ja, en) == wrong


def test_group_digits_writes_separators_but_keeps_years():
    assert group_digits("約7000の島々、12345人、1789年、2024年度、3.1415") == (
        "約7,000の島々、12,345人、1789年、2024年度、3.1415"
    )
