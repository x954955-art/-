from datetime import timedelta

from furima_finder.finder import SearchPlan, run_search
from furima_finder.http import FetchError
from furima_finder.models import Seller
from furima_finder.scoring import Settings

from .conftest import FakeSource, make_listing


def sold_history(median=30000, count=12):
    prices = [median - 1000, median, median + 1000]
    return [make_listing("Nintendo Switch 有機EL", prices[i % 3], sold=True) for i in range(count)]


def test_market_band_and_ranking(now):
    on_sale = [
        make_listing("Switch 有機EL 美品", 21000, age=timedelta(hours=2), id="cheap"),
        make_listing("Switch 有機EL", 25500, id="ok"),
        make_listing("Switch 有機EL", 29500, id="not-cheap"),
        make_listing("たろう様専用 Switch 有機EL", 15000, id="reserved"),
    ]
    source = FakeSource(sold=sold_history(), on_sale=on_sale)
    plan = SearchPlan(keyword="Switch 有機EL", details=0)
    result = run_search(plan, {"mercari": source}, now=now)

    assert result.market.overall.median == 30000
    sold_query, on_sale_query = source.queries
    assert sold_query.sold and not on_sale_query.sold
    # 相場の30%〜（相場×(1-10%)）だけを検索する
    assert (on_sale_query.price_min, on_sale_query.price_max) == (9000, 27000)
    assert [e.listing.id for e in result.recommendations] == ["cheap", "ok"]
    assert result.excluded["専用・購入不可"] == 1
    assert result.checked == 3  # 29500 は価格帯の外なので取得されない
    assert not result.errors


def test_user_price_limits_are_respected(now):
    source = FakeSource(sold=sold_history(), on_sale=[])
    run_search(SearchPlan(keyword="Switch", min_price=15000, max_price=20000), {"mercari": source}, now=now)
    sold_query, on_sale_query = source.queries
    assert (sold_query.price_min, sold_query.price_max) == (15000, None)
    assert (on_sale_query.price_min, on_sale_query.price_max) == (15000, 20000)


def test_budget_below_market_band_skips_on_sale_search(now):
    source = FakeSource(sold=sold_history(), on_sale=[])
    result = run_search(SearchPlan(keyword="Switch", max_price=5000), {"mercari": source}, now=now)
    assert len(source.queries) == 1
    assert any("価格の範囲" in note for note in result.notes)


def test_details_are_checked_for_top_items_and_change_the_ranking(now):
    on_sale = [make_listing("Switch 有機EL", 20000 + i * 500, id=f"item{i}") for i in range(5)]

    def sold_out(listing):
        listing.sold = True

    def trusted(listing):
        listing.seller = Seller("s", good=300)

    source = FakeSource(sold=sold_history(), on_sale=on_sale, details={"item0": sold_out, "item2": trusted})
    result = run_search(SearchPlan(keyword="Switch 有機EL", details=2), {"mercari": source}, now=now)

    ids = [e.listing.id for e in result.recommendations]
    assert "item0" not in ids
    assert ids[0] == "item2"  # 評価の良い出品者が上位に
    assert source.detail_calls[:2] == ["item0", "item1"]
    assert "item2" in source.detail_calls  # 繰り上がった出品も確認する
    assert result.excluded["売り切れ・取引中"] == 1


def test_detail_errors_are_reported_and_stop_further_checks(now):
    on_sale = [make_listing("Switch 有機EL", 20000, id=f"item{i}") for i in range(3)]

    class FailingDetails(FakeSource):
        def fetch_details(self, listing):
            raise FetchError("api.mercari.jp から取得できませんでした（HTTP 403）")

    source = FailingDetails(sold=sold_history(), on_sale=on_sale)
    result = run_search(SearchPlan(keyword="Switch 有機EL", details=5), {"mercari": source}, now=now)
    assert len(result.recommendations) == 3
    assert result.errors == ["メルカリ（商品詳細）: api.mercari.jp から取得できませんでした（HTTP 403）"]


def test_falls_back_to_on_sale_prices_without_sold_history(now):
    on_sale = [make_listing("Switch 有機EL", price) for price in (30000, 30000, 31000, 20000)]
    source = FakeSource(sold=[], on_sale=on_sale)
    result = run_search(SearchPlan(keyword="Switch 有機EL", details=0), {"mercari": source}, now=now)
    assert result.market.basis == "on_sale"
    assert [e.listing.price for e in result.recommendations] == [20000]


def test_manual_market_price_skips_sold_search(now):
    source = FakeSource(on_sale=[make_listing("Switch", 8000)])
    result = run_search(SearchPlan(keyword="Switch", market_price=10000, details=0), {"mercari": source}, now=now)
    assert len(source.queries) == 1 and not source.queries[0].sold
    assert result.market.basis == "manual"
    assert len(result.recommendations) == 1


def test_fetch_errors_are_collected_per_source(now):
    broken = FakeSource(error=FetchError("fril.jp から取得できませんでした（HTTP 503）"), name="rakuma", label="ラクマ")
    working = FakeSource(sold=sold_history(), on_sale=[make_listing("Switch 有機EL", 20000)])
    plan = SearchPlan(keyword="Switch 有機EL", sources=["mercari", "rakuma"], details=0)
    result = run_search(plan, {"mercari": working, "rakuma": broken}, now=now)
    assert len(result.recommendations) == 1
    assert len(result.errors) == 2
    assert all(error.startswith("ラクマ") for error in result.errors)
    assert not result.fetch_failed


def test_total_failure_is_flagged(now):
    broken = FakeSource(error=FetchError("api.mercari.jp から取得できませんでした（プロキシに接続を拒否されました）"))
    result = run_search(SearchPlan(keyword="Switch"), {"mercari": broken}, now=now)
    assert result.fetch_failed
    assert not result.market.ok


def test_condition_filter_is_sent_to_both_searches(now):
    source = FakeSource(sold=sold_history(), on_sale=[])
    plan = SearchPlan(keyword="Switch", condition="good", settings=Settings(condition_filtered=True))
    run_search(plan, {"mercari": source}, now=now)
    assert all(query.conditions == [1, 2, 3] for query in source.queries)


# ---------------------------------------------------------------- ポケモンカード

from furima_finder.finder import SCAN_DEFAULT_MIN_PRICE, run_scan  # noqa: E402
from furima_finder.profiles import pokeca_card_key  # noqa: E402

POKECA_SETTINGS = Settings(resale=True, resale_shipping=230, min_profit=1000, buyer_shipping=300)


def card(title, price, **kwargs):
    return make_listing(title, price, condition=kwargs.pop("condition", 2), **kwargs)


def test_pokeca_search_compares_each_grade_with_its_own_market(now):
    sold = [card(f"リザードンex SAR 201/165 美品 {i}", 10000, sold=True) for i in range(6)]
    sold += [card("PSA10 リザードンex SAR 201/165", 40000, sold=True) for _ in range(4)]
    on_sale = [
        card("リザードンex SAR 201/165", 6000, id="raw"),
        card("【PSA10】リザードンex SAR 201/165", 30000, id="psa"),
        card("リザードンex SAR 201/165 PSA9", 20000, id="psa9"),
    ]
    source = FakeSource(sold=sold, on_sale=on_sale)
    plan = SearchPlan(keyword="リザードンex SAR 201/165", profile="pokeca", details=0, settings=POKECA_SETTINGS)
    result = run_search(plan, {"mercari": source}, now=now)

    by_id = {e.listing.id: e for e in result.recommendations}
    assert by_id["raw"].expected_sale == 10000 and by_id["raw"].profit == 9000 - 230 - 6000
    assert by_id["psa"].expected_sale == 40000 and by_id["psa"].profit == 36000 - 230 - 30000
    assert "psa9" not in by_id
    assert result.excluded["相場不明"] == 1
    # 利益が出うる価格帯だけを検索する: 相場の30% 〜 （最高の相場×0.9 − 送料 − 最低利益）
    on_sale_query = source.queries[1]
    assert (on_sale_query.price_min, on_sale_query.price_max) == (3000, 36000 - 230 - 1000)


class ScanSource(FakeSource):
    """売り切れ検索ではキーワードのカード番号に合う出品だけを返す。"""

    def __init__(self, sold_by_card, **kwargs):
        super().__init__(**kwargs)
        self.sold_by_card = sold_by_card

    def search(self, query):
        self.queries.append(query)
        if not query.sold:
            return [x for x in self.on_sale if not query.price_min or x.price >= query.price_min]
        if self.error:
            raise self.error
        return self.sold_by_card.get(query.keyword.split()[0], [])


def scan_fixture():
    on_sale = [
        card("リザードンex SAR 201/165 151", 6000, id="zard-cheap", age=timedelta(hours=1)),
        card("リザードンex SAR 201/165", 9500, id="zard-normal", age=timedelta(days=1)),
        card("ナンジャモ SAR 096/071", 4000, id="nanja", age=timedelta(hours=5)),
        card("ピカチュウ AR 120/100", 1500, id="pika", age=timedelta(days=3)),
        card("ポケカ オリパ SAR確定", 3000, id="oripa"),
        card("リザードン SAR 美品", 5000, id="no-number"),
        card("安いカード AR 001/100", 500, id="too-cheap"),
    ]
    sold_by_card = {
        "201/165": [card(f"リザードンex SAR 201/165 {i}", 10000, sold=True) for i in range(8)]
        + [card("リザードン SAR 美品", 2000, sold=True)],  # 番号違いは相場に入れない
        "096/071": [card("ナンジャモ SAR 096/071", 4500, sold=True) for _ in range(8)],
        "120/100": [card("ピカチュウ AR 120/100", 1800, sold=True) for _ in range(8)],
    }
    return ScanSource(sold_by_card, on_sale=on_sale)


def test_scan_finds_profitable_cards_across_card_numbers(now):
    source = scan_fixture()
    plan = SearchPlan(keyword="ポケモンカード SAR", profile="pokeca", scan=True, details=0, settings=POKECA_SETTINGS)
    result = run_scan(plan, {"mercari": source}, now=now)

    on_sale_query, *sold_queries = source.queries
    assert on_sale_query.price_min == SCAN_DEFAULT_MIN_PRICE and not on_sale_query.sold
    # 新しい出品があるカードから順に、カード番号＋レアリティで売り切れを探す
    assert [q.keyword for q in sold_queries] == ["201/165 SAR", "096/071 SAR", "120/100 AR"]
    assert all(q.sold and q.pages == 1 for q in sold_queries)

    assert [e.listing.id for e in result.recommendations] == ["zard-cheap"]
    top = result.recommendations[0]
    assert top.expected_sale == 10000 and top.profit == 9000 - 230 - 6000
    assert result.excluded["オリパ"] == 1
    assert result.excluded["カード番号なし"] == 1
    assert result.excluded["利益が少ない"] == 3  # 9,500円のリザードン・ナンジャモ・ピカチュウ
    assert result.market.basis == "scan"
    assert result.checked == 6  # 500円の出品は最低価格で除かれる


def test_scan_limits_the_number_of_cards(now):
    source = scan_fixture()
    plan = SearchPlan(keyword="ポケモンカード SAR", profile="pokeca", scan=True, scan_cards=1, details=0, settings=POKECA_SETTINGS)
    result = run_scan(plan, {"mercari": source}, now=now)
    assert len(source.queries) == 2
    assert any("--scan-cards" in note for note in result.notes)


def test_scan_stops_after_sold_errors(now):
    source = scan_fixture()
    source.error = FetchError("api.mercari.jp から取得できませんでした（HTTP 429: …）")
    plan = SearchPlan(keyword="ポケモンカード SAR", profile="pokeca", scan=True, details=0, settings=POKECA_SETTINGS)
    result = run_scan(plan, {"mercari": source}, now=now)
    assert len(source.queries) == 2  # 販売中 + 最初のカードの売り切れで止まる
    assert len(result.errors) == 1 and "201/165" in result.errors[0]
    assert any("中止" in note for note in result.notes)
    assert result.recommendations == []


def test_card_key_helper_matches_fixture():
    assert pokeca_card_key("リザードンex SAR 201/165 151") == "201/165"
