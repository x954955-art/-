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
