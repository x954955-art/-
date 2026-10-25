from datetime import timedelta

import pytest

from furima_finder import cli, text
from furima_finder.eras import YGO_ERAS, detect_ygo_era
from furima_finder.finder import SearchPlan, run_scan
from furima_finder.profiles import YUGIOH, yugioh_card_key, yugioh_group, yugioh_sold_keyword
from furima_finder.scoring import Settings

from .conftest import FakeSource, make_listing

SCAN_QUERY = text.tokens("遊戯王 プリシク クォーターセンチュリー レリーフ")
SETTINGS = Settings(resale=True, resale_shipping=230, min_profit=1000, buyer_shipping=300)


@pytest.mark.parametrize(
    "title, key",
    [
        ("遊戯王 灰流うらら プリシク RC04-JP010", "RC04-JP010 PSE"),
        ("灰流うらら プリズマティックシークレットレア RC04-JP010", "RC04-JP010 PSE"),
        ("遊戯王 灰流うらら PSE rc04-jp010", "RC04-JP010 PSE"),
        ("灰流うらら 25thシク QCCU-JP001", "QCCU-JP001 QCSE"),
        ("灰流うらら クォーターセンチュリーシークレットレア QCCU-JP001", "QCCU-JP001 QCSE"),
        ("遊戯王 ブラックマジシャン レリーフ 20CP-JPS01", "20CP-JPS01 UL"),
        ("ブラック・マジシャン 20thシク 20CP-JPS01", "20CP-JPS01 20thSE"),
        ("青眼の白龍 シークレット レア SD25-JP001", "SD25-JP001 SE"),
        ("灰流うらら シク RC04-JP010", "RC04-JP010 SE"),
        ("ブラマジ ウルパラ RC04-JP001", "RC04-JP001 UPR"),
        ("ブラマジ ノーパラ RC04-JP001", "RC04-JP001 NP"),
        ("青眼の白龍 SR14-JP001 ウルトラ", "SR14-JP001 UR"),  # 弾の記号の「SR」はレアリティではない
        ("スーパー・ヴィークロイド SR DP01-JP001", "DP01-JP001 SR"),  # カード名の「スーパー」は読まない
        ("灰流うらら プリシク（PSE）RC04-JP010", "RC04-JP010 PSE"),  # 同じレアリティの書き方が2つ
        ("青眼の白龍 SE UR RC04-JP010", None),  # 違うレアリティが2つ
        ("青眼の白龍 RC04-JP010", None),  # レアリティが分からない
        ("青眼の白龍 初期 ウルトラ", None),  # カード番号がない
    ],
)
def test_card_key_has_number_and_rarity(title, key):
    assert yugioh_card_key(title) == key


@pytest.mark.parametrize(
    "title, group",
    [
        ("灰流うらら プリシク RC04-JP010", "raw"),
        ("【PSA10】灰流うらら プリシク RC04-JP010", "psa10"),
        ("遊戯王 QUARTER CENTURY CHRONICLE BOX シュリンク付き", "box_shrink"),
        ("遊戯王 プレイマット", "other"),
    ],
)
def test_groups(title, group):
    assert yugioh_group(title, None) == group


@pytest.mark.parametrize(
    "title, query, reason",
    [
        ("灰流うらら プリシク RC04-JP010", SCAN_QUERY, None),
        ("灰流うらら 25thシク QCCU-JP001", SCAN_QUERY, None),  # 複数のキーワードのどれかのレアリティなら残す
        ("ブラック・マジシャン 20thシク 20CP-JPS01", SCAN_QUERY, "別のレアリティ"),
        ("遊戯王 青眼の白龍 RC04-EN010 PSE", SCAN_QUERY, "海外版"),
        ("遊戯王 青眼の白龍 アジア版 PSE RC04-JP010", SCAN_QUERY, "海外版"),
        ("ラッシュデュエル 青眼の白龍 RD/KP01-JP000 プリシク", SCAN_QUERY, "ラッシュデュエル（別のゲーム）"),
        ("遊戯王 オリパ プリシク確定", SCAN_QUERY, "オリパ"),
        ("遊戯王 BOX シュリンク付き", SCAN_QUERY, "BOX・パック"),
        # カード番号を指定したら、別の番号は外す
        ("灰流うらら プリシク QCCU-JP001", text.tokens("灰流うらら プリシク RC04-JP010"), "別のカード"),
        ("灰流うらら シク RC04-JP010", text.tokens("灰流うらら プリシク RC04-JP010"), "別のレアリティ"),
        ("灰流うらら プリシク RC04-JP010 美品", text.tokens("灰流うらら プリシク RC04-JP010"), None),
    ],
)
def test_exclusions(title, query, reason):
    assert YUGIOH.exclusion_reason(title, query) == reason


def test_sold_keyword_uses_the_most_common_spelling():
    titles = ["灰流うらら プリシク RC04-JP010", "プリシク うらら RC04-JP010", "PSE うらら RC04-JP010"]
    assert yugioh_sold_keyword("RC04-JP010 PSE", titles) == "RC04-JP010 プリシク"
    assert yugioh_sold_keyword("QCCU-JP001 QCSE", ["qcse うらら QCCU-JP001"]) == "QCCU-JP001 QCSE"


@pytest.mark.parametrize(
    "titles, period",
    [
        (["青眼の白龍 初期 ウルトラ"], "1"),
        (["ブラック・マジシャン 2期 レリーフ"], "2"),
        (["ブラマジ 2期 レリーフ"] * 2 + ["ブラマジ 3期 レリーフ"], "2"),  # 多数決
        (["初期傷あり 灰流うらら プリシク"], None),  # 「初期傷」は時期ではない
        (["期間限定 灰流うらら"], None),
    ],
)
def test_period_words_become_eras(titles, period):
    era = detect_ygo_era(titles)
    assert era is (YGO_ERAS[period] if period else None)


def test_older_periods_get_more_points():
    points = [YGO_ERAS[str(period)].points for period in range(1, 13)]
    assert points == sorted(points, reverse=True)
    assert YGO_ERAS["1"].description == "初期（1999〜2000年ごろ）"


class KeywordSource(FakeSource):
    """売り切れ検索では、キーワードごとに用意した出品を返す。"""

    def __init__(self, sold_by_keyword, **kwargs):
        super().__init__(**kwargs)
        self.sold_by_keyword = sold_by_keyword

    def search(self, query):
        self.queries.append(query)
        if query.sold:
            return self.sold_by_keyword.get(query.keyword, [])
        return [x for x in self.on_sale if not query.price_min or x.price >= query.price_min]


def card(title, price, **kwargs):
    return make_listing(title, price, condition=kwargs.pop("condition", 2), **kwargs)


def test_scan_keeps_rarities_apart(now):
    on_sale = [
        card("灰流うらら プリシク RC04-JP010", 20000, id="pse", age=timedelta(hours=1)),
        card("灰流うらら 25thシク QCCU-JP001", 15000, id="qcse", age=timedelta(hours=2)),
        card("ブラック・マジシャン 2期 レリーフ 20CP-JPS01", 4000, id="ul", age=timedelta(hours=3)),
        card("灰流うらら RC04-JP010", 1500, id="unknown"),
    ]
    sold = {
        "RC04-JP010 プリシク": [card(f"灰流うらら プリシク RC04-JP010 {i}", 30000, sold=True) for i in range(6)]
        + [card("灰流うらら シク RC04-JP010", 3000, sold=True)],  # 別のレアリティは混ぜない
        "QCCU-JP001 25thシク": [card(f"灰流うらら 25thシク QCCU-JP001 {i}", 22000, sold=True) for i in range(6)],
        "20CP-JPS01 レリーフ": [card(f"ブラック・マジシャン 2期 レリーフ 20CP-JPS01 {i}", 7000, sold=True) for i in range(6)],
    }
    source = KeywordSource(sold, on_sale=on_sale)
    plan = SearchPlan(
        keyword="遊戯王 プリシク", extra_keywords=["遊戯王 クォーターセンチュリー", "遊戯王 レリーフ"],
        profile="yugioh", scan=True, details=0, settings=SETTINGS,
    )
    result = run_scan(plan, {"mercari": source}, now=now)

    # カード番号＋レアリティ（出品でいちばん多い書き方）で売り切れを探す
    assert [q.keyword for q in source.queries if q.sold] == [
        "RC04-JP010 プリシク", "QCCU-JP001 25thシク", "20CP-JPS01 レリーフ",
    ]
    by_id = {e.listing.id: e for e in result.recommendations}
    assert by_id["pse"].expected_sale == 30000  # シク（3,000円）の相場は混ざらない
    assert by_id["qcse"].expected_sale == 22000
    assert by_id["ul"].expected_sale == 7000
    assert by_id["ul"].era is YGO_ERAS["2"]  # 「2期」と書かれた古いカード
    assert any("古い年代のカード（2期・2000〜2002年ごろ）" in reason for reason in by_id["ul"].reasons)
    assert result.excluded["レアリティが分からない"] == 1


def test_profile_aliases_and_defaults():
    (plan,) = cli.build_plans(cli.build_parser().parse_args(["灰流うらら プリシク", "--profile", "遊戯王"]))
    assert plan.profile == "yugioh" and plan.settings.resale is True
    assert (plan.settings.min_profit, plan.settings.resale_shipping, plan.settings.buyer_shipping) == (1000, 230, 300)
