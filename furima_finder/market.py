"""売り切れ（＝実際に売れた）価格から相場を推定する。"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from . import text
from .models import Listing

# 状態ごとに相場を分ける単位。件数が足りないグループは全体の相場で代用する
CONDITION_GROUPS = {
    "new": ("新品・未使用", frozenset({1})),
    "good": ("美品", frozenset({2, 3})),
    "used": ("傷・汚れあり", frozenset({4, 5, 6})),
}
MIN_SAMPLES = 3
MIN_GROUP_SAMPLES = 5
OUTLIER_MIN_SAMPLES = 8


def condition_group(condition: int | None) -> str | None:
    for key, (_, members) in CONDITION_GROUPS.items():
        if condition in members:
            return key
    return None


def percentile(sorted_values: list[float], q: float) -> float:
    """線形補間のパーセンタイル（q は 0〜1）。"""
    if not sorted_values:
        raise ValueError("empty")
    position = (len(sorted_values) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


@dataclass
class PriceStats:
    count: int
    median: int
    q1: int
    q3: int
    low: int
    high: int

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

    @property
    def spread(self) -> float:
        """中央50%の幅が中央値の何割か。大きいほど相場がばらついている。"""
        return (self.q3 - self.q1) / self.median if self.median else 0.0


@dataclass
class MarketStats:
    overall: PriceStats | None
    groups: dict[str, PriceStats] = field(default_factory=dict)
    basis: str = "sold"  # sold（売り切れ）/ on_sale（販売中から推定）/ manual（手動指定）
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

    def reference(self, condition: int | None) -> tuple[int, str] | None:
        """比較に使う相場（状態別の相場があればそちら）とその名前。"""
        if self.overall is None:
            return None
        key = condition_group(condition)
        if key in self.groups:
            return self.groups[key].median, f"{CONDITION_GROUPS[key][0]}の相場"
        return self.overall.median, "相場"

    def reference_range(self) -> tuple[int, int] | None:
        """状態別も含めた相場の最小値と最大値。"""
        if self.overall is None:
            return None
        medians = [self.overall.median] + [group.median for group in self.groups.values()]
        return min(medians), max(medians)

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
            if self.overall.spread > 0.6:
                notes.append(
                    "価格のばらつきが大きいです。キーワードを具体的にする（型番・容量など）か、"
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


def estimate_market(
    listings: Iterable[Listing],
    query_tokens: list[str],
    *,
    min_match: float,
    buyer_shipping: int,
    basis: str = "sold",
) -> MarketStats:
    listings = list(listings)
    stats = MarketStats(overall=None, basis=basis, fetched=len(listings))
    usable: list[tuple[Listing, int]] = []
    for listing in listings:
        if text.match_ratio(query_tokens, listing.title) < min_match:
            stats.excluded_irrelevant += 1
            continue
        # 「〇〇様専用」は実際に売れた取引なので相場に含める。箱のみ・ジャンク等は含めない
        if text.exclusion_reason(listing.title, query_tokens, include_reserved=basis != "sold") or (
            text.risk_level(listing.title, query_tokens)[0] > 0
        ):
            stats.excluded_rules += 1
            continue
        usable.append((listing, effective_price(listing, buyer_shipping)))

    bounds = outlier_bounds([price for _, price in usable])
    if bounds is not None:
        low, high = bounds
        kept = [(listing, price) for listing, price in usable if low <= price <= high]
        stats.excluded_outliers = len(usable) - len(kept)
        usable = kept
    if len(usable) < MIN_SAMPLES:
        return stats

    stats.overall = PriceStats.of(price for _, price in usable)
    for key, (_, members) in CONDITION_GROUPS.items():
        prices = [price for listing, price in usable if listing.condition in members]
        if len(prices) >= MIN_GROUP_SAMPLES:
            stats.groups[key] = PriceStats.of(prices)
    times = [listing.updated_at or listing.created_at for listing, _ in usable]
    times = [moment for moment in times if moment is not None]
    if times:
        stats.oldest, stats.newest = min(times), max(times)
    return stats
