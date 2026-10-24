import pytest

from furima_finder import text
from furima_finder.market import (
    MarketStats,
    PriceStats,
    effective_price,
    estimate_market,
    outlier_bounds,
    percentile,
)

from .conftest import make_listing

TOKENS = text.tokens("Switch 有機EL")


def estimate(listings, **kwargs):
    kwargs.setdefault("min_match", 0.5)
    kwargs.setdefault("buyer_shipping", 800)
    return estimate_market(listings, TOKENS, **kwargs)


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([10], 0.25) == 10
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_price_stats():
    stats = PriceStats.of([100, 300, 200, 400, 500])
    assert (stats.count, stats.median, stats.q1, stats.q3, stats.low, stats.high) == (5, 300, 200, 400, 100, 500)
    assert stats.spread == pytest.approx(200 / 300)


def test_effective_price_adds_shipping_only_for_cash_on_delivery():
    assert effective_price(make_listing(price=1000, shipping_included=False), 800) == 1800
    assert effective_price(make_listing(price=1000, shipping_included=True), 800) == 1000
    assert effective_price(make_listing(price=1000, shipping_included=None), 800) == 1000


def test_outlier_bounds_need_enough_samples():
    assert outlier_bounds([100] * 7) is None
    low, high = outlier_bounds([10000] * 9 + [100])
    assert low < 10000 < high
    assert not low <= 100 <= high


def test_estimate_market_ignores_irrelevant_junk_and_outliers():
    sold = [make_listing("Switch 有機EL", price, sold=True) for price in (28000, 29000, 30000, 31000, 32000, 30500, 29500, 30000)]
    sold += [
        make_listing("Switch 有機EL ジャンク", 9000, sold=True),
        make_listing("Switch 有機EL 箱のみ", 1500, sold=True),
        make_listing("全然関係ない商品", 500, sold=True),
        make_listing("Switch 有機EL まとめて", 3000, sold=True),  # 外れ値
        make_listing("たろう様専用 Switch 有機EL", 30000, sold=True),  # 売れた取引なので含める
    ]
    market = estimate(sold)
    assert market.ok
    assert market.overall.count == 9
    assert market.overall.median == 30000
    assert market.excluded_rules == 2
    assert market.excluded_irrelevant == 1
    assert market.excluded_outliers == 1
    assert market.fetched == len(sold)


def test_estimate_market_needs_minimum_samples():
    market = estimate([make_listing(price=30000, sold=True), make_listing(price=31000, sold=True)])
    assert not market.ok
    assert market.reference(3) is None


def test_condition_groups_are_used_only_with_enough_samples():
    sold = [make_listing(price=36000, condition=1, sold=True) for _ in range(5)]
    sold += [make_listing(price=30000, condition=3, sold=True) for _ in range(4)]
    sold += [make_listing(price=20000, condition=5, sold=True) for _ in range(3)]
    market = estimate(sold)
    assert market.overall.median == 30000
    assert set(market.groups) == {"new"}
    assert market.reference(1) == (36000, "新品・未使用の相場")
    assert market.reference(3) == (30000, "相場")
    assert market.reference(None) == (30000, "相場")
    assert market.reference_range() == (30000, 36000)


def test_cash_on_delivery_sold_prices_include_shipping():
    sold = [make_listing(price=29200, shipping_included=False, sold=True) for _ in range(3)]
    assert estimate(sold).overall.median == 30000


def test_notes_explain_weak_estimates():
    sold = [make_listing(price=price, sold=True) for price in (10000, 20000, 30000, 60000)]
    notes = estimate(sold).notes()
    assert any("少なめ" in note for note in notes)
    assert any("ばらつき" in note for note in notes)
    assert "手動" in MarketStats.manual(5000).notes()[0]
    on_sale = estimate([make_listing(price=30000) for _ in range(3)], basis="on_sale")
    assert any("販売中の価格" in note for note in on_sale.notes())
