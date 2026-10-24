import pytest

from furima_finder import text
from furima_finder.profiles import (
    DEFAULT,
    POKECA,
    get_profile,
    most_common_rarity,
    pokeca_card_key,
    pokeca_group,
    pokeca_rarity,
)

from .conftest import make_listing

CARD_QUERY = text.tokens("ポケモンカード SAR")
BOX_QUERY = text.tokens("151 BOX シュリンク付き")


@pytest.mark.parametrize(
    "title, group",
    [
        ("リザードンex SAR 201/165 151 美品", "raw"),
        ("【PSA10】リザードンex SAR 201/165", "psa10"),
        ("ＰＳＡ１０ ピカチュウ プロモ", "psa10"),
        ("PSA 9 リザードン SAR", "psa9"),
        ("ARS10 ミュウ SAR 鑑定品", "graded"),
        ("リザードンex SAR 201/165 PSA10狙い", "raw"),
        ("ミュウ SAR 鑑定用 未鑑定", "raw"),
        ("ポケモンカード 151 BOX シュリンク付き", "box_shrink"),
        ("151 ボックス シュリンクなし", "box_noshrink"),
        ("テラスタルフェスex 1BOX", "box"),
        ("ナンジャモ 096/071 SAR BOX開封品", "raw"),
        ("ポケモンカード スターターセット", "other"),
    ],
)
def test_pokeca_groups(title, group):
    assert pokeca_group(title, None) == group


@pytest.mark.parametrize(
    "title, key",
    [
        ("リザードンex SAR 201/165 151", "201/165"),
        ("ナンジャモ SAR 096 / 071", "096/071"),
        ("ピカチュウ プロモ 001/SV-P", "001/SV-P"),
        ("ピカチュウ ２０１／１６５", "201/165"),
        ("10/24発売 リザードン SAR", None),
        ("リザードンex SAR", None),
    ],
)
def test_card_key(title, key):
    assert pokeca_card_key(title) == key


def test_rarity():
    assert pokeca_rarity("リザードンex sar 201/165") == "SAR"
    assert pokeca_rarity("ピカチュウ MUR") == "MUR"
    assert pokeca_rarity("SARAH") is None
    assert most_common_rarity(POKECA, ["a SAR", "b SAR", "c SR", "d"]) == "SAR"
    assert most_common_rarity(DEFAULT, ["a SAR"]) is None


@pytest.mark.parametrize(
    "title, query, reason",
    [
        ("リザードンex SAR 201/165 美品", CARD_QUERY, None),
        ("ポケカ オリパ 1口 SAR確定", CARD_QUERY, "オリパ"),
        ("リザードン SAR プロキシ", CARD_QUERY, "非公式品・偽物の可能性"),
        ("リザードン SAR 英語版", CARD_QUERY, "海外版"),
        ("リザードン SAR 英語版", text.tokens("リザードン SAR 英語版"), None),
        ("ナンジャモ SAR 2枚セット", CARD_QUERY, "まとめ売り・セット"),
        ("引退品 SAR まとめ売り", CARD_QUERY, "まとめ売り・セット"),
        ("ナンジャモ SAR おまとめ割あり", CARD_QUERY, None),
        ("リザードンデッキ 構築済み", CARD_QUERY, "まとめ売り・セット"),
        ("ナンジャモ SAR デッキパーツ", CARD_QUERY, None),
        ("ピカチュウ スリーブのみ", CARD_QUERY, "サプライ品のみ"),
        ("デッキシールド ピカチュウ", CARD_QUERY, "カード以外・カードを特定できない"),
        ("ポケモンカード 151 BOX シュリンク付き", CARD_QUERY, "BOX・パック"),
        ("たろう様専用 ナンジャモ SAR", CARD_QUERY, "専用・購入不可"),
        ("ポケモンカード 151 BOX シュリンク付き", BOX_QUERY, None),
        ("リザードンex SAR 201/165", BOX_QUERY, "単品カード"),
        ("151 BOX 2箱", BOX_QUERY, "まとめ売り・セット"),
        ("ナンジャモ", text.tokens("ナンジャモ"), None),
    ],
)
def test_pokeca_exclusions(title, query, reason):
    assert POKECA.exclusion_reason(title, query) == reason


@pytest.mark.parametrize(
    "title, expected",
    [
        ("ナンジャモ SAR 初期傷あり", (1, "初期傷")),
        ("ナンジャモ SAR 白欠けなし 美品", (0, None)),
        ("ナンジャモ SAR 折れあり", (2, "折れ")),
        ("151 BOX 再シュリンク", (2, "再シュリンク")),
        ("ナンジャモ SAR ジャンク", (2, "ジャンク")),
    ],
)
def test_pokeca_risk_words(title, expected):
    assert POKECA.risk_level(title, CARD_QUERY) == expected


def test_box_items_use_larger_shipping():
    assert POKECA.resale_shipping(make_listing("151 BOX シュリンク付き"), 230) == 520
    assert POKECA.resale_shipping(make_listing("リザードン SAR 201/165"), 230) == 230
    assert POKECA.resale_shipping(make_listing("リザードン SAR 201/165"), 600) == 600
    assert DEFAULT.resale_shipping(make_listing("151 BOX"), 750) == 750


def test_get_profile_accepts_aliases():
    assert get_profile("ポケカ") is POKECA
    assert get_profile("Pokeca") is POKECA
    assert get_profile("default") is DEFAULT
    with pytest.raises(ValueError):
        get_profile("yugioh")
