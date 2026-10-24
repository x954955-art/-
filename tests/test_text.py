import pytest

from furima_finder import text


def test_normalize_absorbs_width_case_and_kana():
    assert text.normalize("ＳＷＩＴＣＨ　ぽけもん") == "switch ポケモン"


def test_match_ratio_counts_query_words_in_title():
    tokens = text.tokens("Nintendo Switch 有機EL")
    assert text.match_ratio(tokens, "ニンテンドー Switch 有機ELモデル") == pytest.approx(2 / 3)
    assert text.match_ratio(tokens, "nintendo switch 有機el") == 1
    assert text.match_ratio([], "なんでも") == 1


@pytest.mark.parametrize(
    "title, query, expected",
    [
        ("たろう様専用 Switch 有機EL", "Switch", "専用・購入不可"),
        ("【専用】Switch 有機EL", "Switch", "専用・購入不可"),
        ("Switch 有機EL 購入不可", "Switch", "専用・購入不可"),
        ("PS5専用 コントローラー", "PS5 コントローラー", None),
        ("Switch 有機EL 箱のみ", "Switch", "箱・付属品のみ"),
        ("Switch 有機EL 空箱", "Switch", "箱・付属品のみ"),
        ("Switch 本体と箱のみ", "Switch", None),
        ("Switch 本体なし 箱のみ", "Switch", "箱・付属品のみ"),
        ("Switch ケースのみ", "Switch", "箱・付属品のみ"),
        ("Switch ケースのみ", "Switch ケース", None),
        ("Switch 写真のみ", "Switch", "写真のみ"),
        ("Switch 本体 レンタル", "Switch", "レンタル"),
        ("Switch 有機EL 美品", "Switch", None),
    ],
)
def test_exclusion_reason(title, query, expected):
    assert text.exclusion_reason(title, text.tokens(query)) == expected


def test_reserved_rules_can_be_skipped_for_sold_listings():
    assert text.exclusion_reason("たろう様専用 Switch", [], include_reserved=False) is None


@pytest.mark.parametrize(
    "title, query, expected",
    [
        ("Switch ジャンク", "Switch", (2, "ジャンク")),
        ("Switch ジャンク品ではありません", "Switch", (0, None)),
        ("Switch 故障なし", "Switch", (0, None)),
        ("Switch 動作未確認", "Switch", (1, "動作未確認")),
        ("Switch ジャンク", "Switch ジャンク", (0, None)),
        ("Switch 美品", "Switch", (0, None)),
    ],
)
def test_risk_level(title, query, expected):
    assert text.risk_level(title, text.tokens(query)) == expected


def test_description_risks_keeps_the_longest_word_and_skips_negations():
    description = "動作未確認です。目立つ破損なし。ジャンクではありません。"
    assert text.description_risks(description, []) == ["動作未確認"]


@pytest.mark.parametrize(
    "description, expected",
    [
        ("折れや水濡れはありません。", []),
        ("目立った折れ・水濡れなどはございません", []),
        ("折れもなく綺麗です", []),
        ("水濡れはしていません", []),
        ("ジャンク品ではありません", []),
        ("折れていないです", []),
        ("初期傷はありますが目立たない程度です", ["初期傷"]),
        ("白欠けが少しあります", ["白欠け"]),
        ("折れあり、返品はできません", ["折れ"]),
        ("折れと水濡れがあります", ["折れ", "水濡れ"]),
    ],
)
def test_description_negations(description, expected):
    from furima_finder.profiles import POKECA

    assert POKECA.description_risks(description, []) == expected
