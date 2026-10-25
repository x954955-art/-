from datetime import timedelta

import pytest

from furima_finder import cli, text
from furima_finder.finder import SearchPlan, run_scan
from furima_finder.profiles import ONEPIECE, onepiece_card_key, onepiece_group, onepiece_sold_keyword
from furima_finder.scoring import Settings

from .conftest import FakeSource, make_listing

SCAN_QUERY = text.tokens("ワンピースカード パラレル")
SETTINGS = Settings(resale=True, resale_shipping=230, min_profit=1000, buyer_shipping=300)


@pytest.mark.parametrize(
    "title, key",
    [
        ("ワンピースカード シャンクス SEC OP01-120", "OP01-120"),
        ("ワンピースカード シャンクス SEC パラレル OP01-120", "OP01-120 パラレル"),
        ("ワンピカ ナミ SR パラ OP01-016", "OP01-016 パラレル"),
        ("【PSA10】シャンクス コミパラ OP01-120", "OP01-120 スーパーパラレル"),
        ("シャンクス スーパーパラレル op01-120", "OP01-120 スーパーパラレル"),
        ("ルフィ SP OP05-119", "OP05-119 SP"),
        ("PRB01-001 ルフィ TR", "PRB01-001 TR"),
        ("ワンピースカード ゾロ L OP-01-001 リーダー", "OP01-001"),
        ("ワンピースカード ヤマト ST10 013", "ST10-013"),
        ("ワンピースカード ルフィ P-001 プロモ", "P-001"),
        ("ＯＰ０１－１２０ シャンクス", "OP01-120"),  # 全角
        ("OP01−120 シャンクス", "OP01-120"),  # 数学のマイナス記号
        ("ナミ SR OP01-016 パラレルではありません", "OP01-016"),
        ("ナミ SR OP01-016 非パラ", "OP01-016"),
        ("ワンピースカード シャンクス SEC", None),  # カード番号がない
    ],
)
def test_card_key_has_number_and_version(title, key):
    assert onepiece_card_key(title) == key


@pytest.mark.parametrize(
    "title, group",
    [
        ("シャンクス SEC OP01-120", "raw"),
        ("【PSA10】シャンクス コミパラ OP01-120", "psa10"),
        ("ワンピースカード ROMANCE DAWN BOX テープ付き", "box_tape"),
        ("ワンピースカード OP-05 BOX テープなし", "box_notape"),
        ("ワンピースカード 新時代の主役 未開封 BOX", "box"),
        ("ワンピースカード プレイマット", "other"),
    ],
)
def test_groups(title, group):
    assert onepiece_group(title, None) == group


@pytest.mark.parametrize(
    "title, query, reason",
    [
        ("シャンクス SEC パラレル OP01-120", SCAN_QUERY, None),
        ("ワンピースカード 英語版 シャンクス OP01-120", SCAN_QUERY, "海外版"),
        ("シャンクス OP01-120 アジア版", SCAN_QUERY, "海外版"),
        ("ワンピースカード ドン!! カード ゴールド", SCAN_QUERY, "ドン!!カード"),
        ("ワンピースカード OP01 1カートン", SCAN_QUERY, "まとめ売り・セット"),
        ("ワンピースカード オリパ パラレル確定", SCAN_QUERY, "オリパ"),
        ("ワンピースカード ROMANCE DAWN BOX テープ付き", SCAN_QUERY, "BOX・パック"),
        # カード番号を指定したら、パラレルと通常版も別のカードとして扱う
        ("シャンクス SEC パラレル OP01-120", text.tokens("シャンクス SEC OP01-120"), "別のカード"),
        ("シャンクス コミパラ OP01-120", text.tokens("シャンクス SEC パラレル OP01-120"), "別のカード"),
        ("シャンクス SEC OP01-120 美品", text.tokens("シャンクス SEC OP01-120"), None),
    ],
)
def test_exclusions(title, query, reason):
    assert ONEPIECE.exclusion_reason(title, query) == reason


def test_box_tape_damage_is_a_strong_risk():
    assert ONEPIECE.risk_level("ワンピースカード OP-05 BOX テープ剥がれ", text.tokens("OP-05 BOX")) == (2, "テープ剥がれ")


def test_sold_keyword_uses_the_most_common_spelling():
    titles = ["シャンクス コミパラ OP01-120", "コミパラ シャンクス OP01-120", "シャンクス スーパーパラレル OP01-120"]
    assert onepiece_sold_keyword("OP01-120 スーパーパラレル", titles) == "OP01-120 コミパラ"
    assert onepiece_sold_keyword("OP01-016 パラレル", ["ナミ SR パラレル OP01-016"]) == "OP01-016 パラレル"
    assert onepiece_sold_keyword("OP01-120", ["シャンクス SEC OP01-120"]) == "OP01-120"
    assert onepiece_sold_keyword("OP05-119 SP", ["ルフィ sp OP05-119"]) == "OP05-119 SP"


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


def test_scan_keeps_parallel_and_normal_versions_apart(now):
    on_sale = [
        card("シャンクス SEC パラレル OP01-120", 20000, id="para", age=timedelta(hours=1)),
        card("シャンクス SEC OP01-120", 2000, id="normal", age=timedelta(hours=2)),
        card("ナミ SR パラ OP01-016", 3000, id="nami", age=timedelta(hours=3)),
        card("シャンクス SEC", 1500, id="no-number"),
    ]
    sold = {
        "OP01-120 パラレル": [card(f"シャンクス SEC パラレル OP01-120 {i}", 30000, sold=True) for i in range(6)]
        + [card("シャンクス SEC OP01-120", 4000, sold=True)],  # 通常版は混ぜない
        "OP01-120": [card(f"シャンクス SEC OP01-120 {i}", 4000, sold=True) for i in range(6)]
        + [card("シャンクス SEC パラレル OP01-120", 30000, sold=True)],  # パラレルは混ぜない
        "OP01-016 パラ": [card(f"ナミ SR パラ OP01-016 {i}", 6000, sold=True) for i in range(6)],
    }
    source = KeywordSource(sold, on_sale=on_sale)
    plan = SearchPlan(keyword="ワンピースカード パラレル", profile="onepiece", scan=True, details=0, settings=SETTINGS)
    result = run_scan(plan, {"mercari": source}, now=now)

    # カード番号＋種類（出品でいちばん多い書き方）で売り切れを探す
    assert [q.keyword for q in source.queries if q.sold] == ["OP01-120 パラレル", "OP01-120", "OP01-016 パラ"]
    by_id = {e.listing.id: e for e in result.recommendations}
    assert by_id["para"].expected_sale == 30000  # パラレルの相場
    assert by_id["normal"].expected_sale == 4000  # 通常版はパラレルの相場と比べない
    assert by_id["nami"].expected_sale == 6000
    assert by_id["para"].era is None  # ワンピースカードは年代を推定しない（長い目で見た値動きは見る）
    assert result.excluded["カード番号なし"] == 1


def test_profile_aliases_and_defaults(tmp_path):
    plans = cli.build_plans(cli.build_parser().parse_args(["シャンクス SEC OP01-120", "--profile", "ワンピース"]))
    (plan,) = plans
    assert plan.profile == "onepiece" and plan.settings.resale is True
    assert (plan.settings.min_profit, plan.settings.resale_shipping, plan.settings.buyer_shipping) == (1000, 230, 300)
