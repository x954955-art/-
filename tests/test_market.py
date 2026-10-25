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
    assert market.lookup("good") is None


def test_condition_groups_are_used_only_with_enough_samples():
    sold = [make_listing(price=36000, condition=1, sold=True) for _ in range(5)]
    sold += [make_listing(price=30000, condition=3, sold=True) for _ in range(4)]
    sold += [make_listing(price=20000, condition=5, sold=True) for _ in range(3)]
    market = estimate(sold)
    assert market.overall.median == 30000
    assert set(market.groups) == {"new"}
    assert market.lookup("new") == (market.groups["new"], "新品・未使用")
    assert market.groups["new"].median == 36000
    assert market.lookup("good") == (market.overall, "")
    assert market.lookup(None) == (market.overall, "")
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


def test_recent_median_sales_count_and_trend(now):
    from datetime import timedelta

    from furima_finder.market import PriceStats

    def sold(price, days):
        return make_listing("Switch 有機EL", price, sold=True, updated_at=now - timedelta(days=days))

    listings = [sold(11000, days) for days in (1, 2, 3, 4, 5, 6)]
    listings += [sold(10000, days) for days in (10, 12, 15, 20, 25, 28)]
    listings.append(sold(9000, 40))
    stats = estimate(listings, now=now).overall
    assert stats.count == 13
    assert stats.recent_median == 11000  # 14日以内の8件
    assert stats.sales_30d == 12
    assert stats.trend == pytest.approx(0.1)  # 直近7日 11,000円 ÷ それより前 10,000円
    # 日時が分からなければ計算しない
    plain = PriceStats.of([1, 2, 3])
    assert (plain.recent_median, plain.sales_30d, plain.trend) == (None, None, None)


def test_on_sale_basis_has_no_sales_activity(now):
    market = estimate([make_listing(price=30000) for _ in range(5)], basis="on_sale", now=now)
    assert market.overall.sales_30d is None


def test_long_term_trend_compares_recent_sales_with_3_to_12_months_ago(now):
    from datetime import timedelta

    def sold(price, days):
        return make_listing("Switch 有機EL", price, sold=True, updated_at=now - timedelta(days=days))

    listings = [sold(12000, days) for days in (3, 10, 20, 25)]
    listings += [sold(11000, days) for days in (40, 60)]  # 1〜3か月前は比べない
    listings += [sold(8000, days) for days in (100, 150, 200, 300)]
    listings.append(sold(5000, 400))  # 1年より前も使わない
    stats = estimate(listings, now=now).overall
    assert stats.past_median == 8000
    assert stats.long_trend == pytest.approx(0.5)  # 直近30日 12,000円 ÷ 3か月〜1年前 8,000円
    assert (stats.month_median, stats.median) == (12000, 11000)

    # 直近30日に3件なければ60日まで広げる
    sparse = [sold(12000, 20), sold(13000, 45), sold(12500, 55)] + [sold(10000, days) for days in (120, 200, 250)]
    sparse_stats = estimate(sparse, now=now).overall
    assert sparse_stats.long_trend == pytest.approx(0.25)
    assert sparse_stats.month_median is None  # 売値の目安に使う「直近30日」は3件以上のときだけ
    # 昔の売り切れがなければ出さない
    recent_only = estimate([sold(10000, days) for days in (1, 2, 3, 4, 5)], now=now).overall
    assert (recent_only.long_trend, recent_only.past_median) == (None, None)


def test_typical_condition_of_sold_listings():
    sold = [make_listing("Switch 有機EL", 30000, sold=True, condition=c) for c in (2, 3, 4, 5)]
    assert estimate(sold).overall.condition == 3  # 真ん中の2つ（3と4）のうち良いほう
    unknown = [make_listing("Switch 有機EL", 30000, sold=True, condition=None) for _ in range(4)]
    assert estimate(unknown).overall.condition is None


def test_market_era_comes_from_sold_titles(now):
    from furima_finder.profiles import POKECA

    sold = [make_listing(f"旧裏 リザードン 美品{i}", 10000, sold=True) for i in range(4)]
    market = estimate_market(
        sold, text.tokens("旧裏 リザードン"), min_match=0.5, buyer_shipping=300, profile=POKECA, now=now
    )
    assert market.era.key == "kyuura"
    assert estimate([make_listing("Switch 有機EL", 30000, sold=True) for _ in range(3)]).era is None


def test_strict_profile_keeps_grades_apart(now):
    from furima_finder.profiles import POKECA

    sold = [make_listing(f"リザードンex SAR 201/165 美品{i}", 10000 + i * 100, sold=True) for i in range(10)]
    sold += [make_listing("【PSA10】リザードンex SAR 201/165", price, sold=True) for price in (40000, 42000, 41000)]
    market = estimate_market(
        sold, text.tokens("リザードンex SAR"), min_match=0.5, buyer_shipping=300, profile=POKECA, now=now
    )
    # PSA10 は未鑑定の中では外れ値だが、グループごとに判定するので消えない
    assert market.excluded_outliers == 0
    assert market.groups["raw"].median == 10450
    assert market.groups["psa10"].median == 41000
    assert market.lookup("psa10") == (market.groups["psa10"], "PSA10")
    assert market.lookup("psa9") is None  # 同じ種類の売り切れがなければ比べない
    assert market.reference_range() == (10450, 41000)
