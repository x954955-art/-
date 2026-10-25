import pytest

from furima_finder import text
from furima_finder.profiles import (
    DEFAULT,
    POKECA,
    get_profile,
    most_common_rarity,
    old_card_name,
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
        # 旧裏の初版（マークなし）や 1ED は別の相場
        ("旧裏 リザードン 初版", "raw_1ed"),
        ("旧裏 リザードン マークなし PSA9", "psa9_1ed"),
        ("旧裏 リザードン 初版ではありません", "raw"),
        ("ポケモンカードe リザードン 1ED 006/128", "raw_1ed"),
        ("リザードンex SAR 201/165 初版", "raw"),  # SV以降の「初版」は区別しない
        ("旧裏 拡張パック 未開封", "other"),
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
        # 旧裏はカード番号がないので名前で見分ける
        ("旧裏 リザードン 初版 マークなし", "旧裏 リザードン"),
        ("【旧裏】わるいリザードン", "旧裏 わるいリザードン"),
        ("旧裏面 リザードン 第1弾 拡張パック", "旧裏 リザードン"),  # 「拡張パック第1弾」は弾の名前
        ("旧裏 ピカチュウ プロモ", "旧裏 ピカチュウ プロモ"),
        ("リザードン 初版", None),  # 「旧裏」と書かれていなければ名前では見分けない
        ("旧裏 拡張パック 未開封", None),
        ("旧裏 リザードン スリーブ", None),
    ],
)
def test_card_key(title, key):
    assert pokeca_card_key(title) == key


@pytest.mark.parametrize(
    "title, name",
    [
        ("ポケモンカード 旧裏 リザードン LV.76 初版", "リザードン"),
        ("旧裏 わるいリザードン", "わるいリザードン"),
        ("旧裏 ひかるギャラドス neo", "ひかるギャラドス"),
        ("旧裏 カスミのトサキント", "カスミのトサキント"),
        ("旧裏 ロケット団のニャース", "ロケット団のニャース"),
        ("旧裏のミュウツー", "ミュウツー"),
        ("ポケモンカードリザードン 旧裏", "リザードン"),
        ("旧裏 キラ リザードンホロ", "リザードン"),
        ("旧裏 サンダース", "サンダース"),  # 「サンダー」ではない
        ("旧裏 レアコイル", "レアコイル"),  # 「コイル」ではない
        ("旧裏 ファンクラブ ピカチュウ", "ピカチュウ"),  # 「ファンクラブ」の「クラブ」はポケモンではない
        ("旧裏 リザードン カメックス", None),  # 2種類以上はまとめ売りの可能性
        ("旧裏 ブースターパック", None),
        ("旧裏 オーキド博士", None),
        ("旧裏 ナナミのピカチュウ", None),  # 知らない「〇〇の」は別のカードかもしれない
    ],
)
def test_old_card_name(title, name):
    assert old_card_name(title) == name


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
        # カード番号（旧裏はカード名）を指定したら、別のカードは外す
        ("リザードンex SAR 349/190", text.tokens("リザードンex SAR 201/165"), "別のカード"),
        ("PSA10 リザードンex SAR 201/165", text.tokens("リザードンex SAR 201/165"), None),
        ("旧裏 わるいリザードン 初版", text.tokens("旧裏 リザードン 初版"), "別のカード"),
        ("リザードン 初版 マークなし", text.tokens("旧裏 リザードン 初版"), None),
        ("リザードン 旧裏 スリーブ", text.tokens("旧裏 リザードン"), "カード以外・カードを特定できない"),
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


def test_missing_card_reason():
    assert POKECA.missing_card_reason("旧裏 オーキド博士") == "カード名を特定できない"
    assert POKECA.missing_card_reason("リザードン SAR 美品") == "カード番号なし"
    assert DEFAULT.missing_card_reason("Switch") == "カード番号なし"


def test_get_profile_accepts_aliases():
    assert get_profile("ポケカ") is POKECA
    assert get_profile("Pokeca") is POKECA
    assert get_profile("default") is DEFAULT
    with pytest.raises(ValueError):
        get_profile("yugioh")
