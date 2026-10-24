"""売り切れ（＝実際に売れた）価格から相場を推定する。"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import text
from .models import Listing
from .profiles import DEFAULT, Profile

MIN_SAMPLES = 3
MIN_GROUP_SAMPLES = 5
OUTLIER_MIN_SAMPLES = 8
RECENT_DAYS = 14  # 「直近の相場」に使う期間
RECENT_MIN_SAMPLES = 5
TREND_DAYS = 7  # 直近7日とそれ以前（30日まで）を比べて相場の動きを見る
TREND_MIN_SAMPLES = 3
SALES_WINDOW_DAYS = 30


def percentile(sorted_values: list[float], q: float) -> float:
    """線形補間のパーセンタイル（q は 0〜1）。"""
    if not sorted_values:
        raise ValueError("empty")
    position = (len(sorted_values) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def _median(values: Iterable[float]) -> float:
    return percentile(sorted(values), 0.5)


@dataclass
class PriceStats:
    count: int
    median: int
    q1: int
    q3: int
    low: int
    high: int
    recent_median: int | None = None  # 直近14日に売れた価格の中央値（5件以上あるとき）
    sales_30d: int | None = None  # 直近30日に売れた件数（売れた日時が分かるときだけ）
    trend: float | None = None  # 直近7日の中央値が、それより前（30日まで）より何割高いか

    @classmethod
    def of(cls, prices: Iterable[int]) -> PriceStats:
        values = sorted(prices)
        return cls(
            count=len(values),
            median=round(percentile(values, 0.5)),
            q1=round(percentile(values, 0.25)),
            q3=round(percentile(values, 0.75)),
            low=values[0],
            high=values[-1],
        )

    @classmethod
    def from_sales(cls, sales: list[tuple[int, datetime | None]], now: datetime) -> PriceStats:
        """（価格, 売れた日時）の一覧から、件数・中央値に加えて直近の動きも出す。"""
        stats = cls.of(price for price, _ in sales)
        dated = [(price, now - moment) for price, moment in sales if moment is not None]
        if not dated:
            return stats
        recent = [price for price, age in dated if age <= timedelta(days=RECENT_DAYS)]
        if len(recent) >= RECENT_MIN_SAMPLES:
            stats.recent_median = round(_median(recent))
        stats.sales_30d = sum(1 for _, age in dated if age <= timedelta(days=SALES_WINDOW_DAYS))
        last_week = [price for price, age in dated if age <= timedelta(days=TREND_DAYS)]
        before = [
            price
            for price, age in dated
            if timedelta(days=TREND_DAYS) < age <= timedelta(days=SALES_WINDOW_DAYS)
        ]
        if len(last_week) >= TREND_MIN_SAMPLES and len(before) >= TREND_MIN_SAMPLES:
            stats.trend = _median(last_week) / _median(before) - 1
        return stats

    @property
    def spread(self) -> float:
        """中央50%の幅が中央値の何割か。大きいほど相場がばらついている。"""
        return (self.q3 - self.q1) / self.median if self.median else 0.0


@dataclass
class MarketStats:
    overall: PriceStats | None
    groups: dict[str, PriceStats] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    # True なら、出品と同じグループ（PSA10 など）の相場がないときは比較しない
    strict: bool = False
    basis: str = "sold"  # sold（売り切れ）/ on_sale（販売中から推定）/ manual（手動指定）/ scan
    fetched: int = 0
    excluded_irrelevant: int = 0
    excluded_rules: int = 0
    excluded_outliers: int = 0
    oldest: datetime | None = None
    newest: datetime | None = None

    @classmethod
    def manual(cls, price: int) -> MarketStats:
        return cls(overall=PriceStats(0, price, price, price, price, price), basis="manual")

    @property
    def ok(self) -> bool:
        return self.overall is not None

    def lookup(self, group: str | None) -> tuple[PriceStats, str] | None:
        """比較に使う相場と、そのグループ名（全体の相場なら空文字）。"""
        if self.overall is None:
            return None
        if group in self.groups:
            return self.groups[group], self.labels.get(group, group)
        if self.strict and self.basis != "manual":
            return None
        return self.overall, ""

    def reference_range(self) -> tuple[int, int] | None:
        """グループ別も含めた相場の最小値と最大値（販売中を検索する価格帯に使う）。"""
        if self.overall is None:
            return None
        values = []
        for stats in (self.overall, *self.groups.values()):
            values.append(stats.median)
            if stats.recent_median:
                values.append(stats.recent_median)
        return min(values), max(values)

    def notes(self) -> list[str]:
        notes = []
        if self.basis == "manual":
            notes.append("相場は手動で指定された値です")
            return notes
        if self.basis == "on_sale":
            notes.append("売り切れのデータが足りないため、販売中の価格から出した参考値です（実際の相場より高めに出がちです）")
        if self.overall is not None:
            if self.overall.count < 10:
                notes.append(f"相場の元データが{self.overall.count}件と少なめです")
            spread_stats = list(self.groups.values()) if self.strict and self.groups else [self.overall]
            if any(stats.spread > 0.6 for stats in spread_stats):
                notes.append(
                    "価格のばらつきが大きいです。キーワードを具体的にする（型番・カード番号など）か、"
                    "--min-price で付属品などを除くと精度が上がります"
                )
        return notes


def effective_price(listing: Listing, buyer_shipping: int) -> int:
    """実際に払う金額。着払いなら想定送料を足す。"""
    return listing.price + (buyer_shipping if listing.shipping_included is False else 0)


def outlier_bounds(prices: list[int], k: float = 1.5) -> tuple[float, float] | None:
    """対数をとった価格で四分位範囲の k 倍より外を外れ値とする（件数が少なければ判定しない）。"""
    if len(prices) < OUTLIER_MIN_SAMPLES:
        return None
    logs = sorted(math.log(price) for price in prices)
    q1, q3 = percentile(logs, 0.25), percentile(logs, 0.75)
    iqr = max(q3 - q1, 0.1)  # 同じ価格ばかりのときに削りすぎないよう最低幅を持たせる
    return math.exp(q1 - k * iqr), math.exp(q3 + k * iqr)


def _drop_outliers(items: list[tuple[Listing, int]]) -> list[tuple[Listing, int]]:
    bounds = outlier_bounds([price for _, price in items])
    if bounds is None:
        return items
    low, high = bounds
    return [(listing, price) for listing, price in items if low <= price <= high]


def estimate_market(
    listings: Iterable[Listing],
    query_tokens: list[str],
    *,
    min_match: float,
    buyer_shipping: int,
    basis: str = "sold",
    profile: Profile = DEFAULT,
    now: datetime | None = None,
) -> MarketStats:
    listings = list(listings)
    now = now or datetime.now(timezone.utc)
    stats = MarketStats(
        overall=None,
        labels=dict(profile.group_labels),
        strict=profile.strict_groups,
        basis=basis,
        fetched=len(listings),
    )
    usable: list[tuple[Listing, int]] = []
    for listing in listings:
        if text.match_ratio(query_tokens, listing.title) < min_match:
            stats.excluded_irrelevant += 1
            continue
        # 「〇〇様専用」は実際に売れた取引なので相場に含める。箱のみ・ジャンク等は含めない
        if profile.exclusion_reason(listing.title, query_tokens, include_reserved=basis != "sold") or (
            profile.risk_level(listing.title, query_tokens)[0] > 0
        ):
            stats.excluded_rules += 1
            continue
        usable.append((listing, effective_price(listing, buyer_shipping)))

    # PSA10 と未鑑定のように価格帯が違うものを混ぜないよう、厳密なプロファイルではグループごとに外れ値を判定する
    by_group: dict[str | None, list[tuple[Listing, int]]] = defaultdict(list)
    for item in usable:
        by_group[profile.group(item[0])].append(item)
    if profile.strict_groups:
        kept = [item for items in by_group.values() for item in _drop_outliers(items)]
    else:
        kept = _drop_outliers(usable)
    stats.excluded_outliers = len(usable) - len(kept)
    if len(kept) < MIN_SAMPLES:
        return stats

    def summarize(items: list[tuple[Listing, int]]) -> PriceStats:
        if basis != "sold":  # 販売中の出品の日時は「売れた日時」ではない
            return PriceStats.of(price for _, price in items)
        return PriceStats.from_sales([(price, listing.updated_at or listing.created_at) for listing, price in items], now)

    stats.overall = summarize(kept)
    min_group = MIN_SAMPLES if profile.strict_groups else MIN_GROUP_SAMPLES
    kept_ids = {id(listing) for listing, _ in kept}
    for key in profile.group_labels:
        items = [(listing, price) for listing, price in by_group.get(key, []) if id(listing) in kept_ids]
        if len(items) >= min_group:
            stats.groups[key] = summarize(items)
    times = [listing.updated_at or listing.created_at for listing, _ in kept]
    times = [moment for moment in times if moment is not None]
    if times:
        stats.oldest, stats.newest = min(times), max(times)
    return stats
