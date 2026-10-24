"""1つのキーワードについて「相場を調べる → 販売中を集める → 採点する」までを行う。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import text
from .http import FetchError
from .market import MarketStats, estimate_market
from .models import Listing, SearchQuery
from .scoring import Evaluation, Settings, evaluate, rank
from .sources import Source

# --condition の値 → 検索で指定する「商品の状態」
CONDITION_FILTERS = {
    "any": [],
    "new": [1],
    "near-new": [1, 2],
    "good": [1, 2, 3],
    "fair": [1, 2, 3, 4],
}


@dataclass
class SearchPlan:
    keyword: str
    exclude: list[str] = field(default_factory=list)
    min_price: int | None = None
    max_price: int | None = None
    condition: str = "any"
    sources: list[str] = field(default_factory=lambda: ["mercari"])
    pages: int = 2
    sold_pages: int = 2
    market_price: int | None = None
    details: int = 10
    floor_ratio: float = 0.3  # 相場のこの割合より安い出品は付属品などとみなして検索しない
    settings: Settings = field(default_factory=Settings)


@dataclass
class SearchResult:
    plan: SearchPlan
    market: MarketStats
    recommendations: list[Evaluation]
    checked: int
    excluded: Counter[str]
    errors: list[str]
    searched_at: datetime
    notes: list[str] = field(default_factory=list)

    @property
    def fetch_failed(self) -> bool:
        """通信エラーなどで出品を1件も取得できなかった。"""
        return bool(self.errors) and self.checked == 0 and self.market.fetched == 0


def _on_sale_band(plan: SearchPlan, market: MarketStats) -> tuple[int | None, int | None]:
    """販売中を検索する価格帯。相場から見て割安になりうる範囲だけを取りに行く。"""
    price_min, price_max = plan.min_price, plan.max_price
    reference = market.reference_range()
    if reference is None:
        return price_min, price_max
    lowest, highest = reference
    band_min = int(lowest * plan.floor_ratio)
    band_max = int(highest * (1 - plan.settings.min_discount))
    price_min = max(price_min or 0, band_min) or None
    price_max = min(price_max, band_max) if price_max else band_max
    return price_min, price_max


def _collect(
    sources: list[Source], query: SearchQuery, errors: list[str], what: str
) -> list[Listing]:
    listings: list[Listing] = []
    for source in sources:
        if query.sold and not source.supports_sold:
            continue
        try:
            listings.extend(source.search(query))
        except FetchError as exc:
            errors.append(f"{source.label}（{what}）: {exc}")
    return listings


def _check_details(
    plan: SearchPlan,
    sources: Mapping[str, Source],
    ranked: list[Evaluation],
    errors: list[str],
    progress: Callable[[str], None],
) -> tuple[int, bool]:
    """上位の出品について出品者評価・説明文を取りに行く。（取得できた件数, 失敗したか）を返す。"""
    targets = [
        evaluation.listing
        for evaluation in ranked[: plan.details]
        if not evaluation.listing.detail_checked
        and not evaluation.listing.is_shop
        and sources[evaluation.listing.source].supports_details
    ]
    if targets:
        progress(f"上位{len(targets)}件の出品者評価・説明文を確認中…")
    checked = 0
    for listing in targets:
        source = sources[listing.source]
        try:
            source.fetch_details(listing)
        except FetchError as exc:
            errors.append(f"{source.label}（商品詳細）: {exc}")
            return checked, True
        checked += 1
    return checked, False


def run_search(
    plan: SearchPlan,
    sources: Mapping[str, Source],
    *,
    now: datetime | None = None,
    progress: Callable[[str], None] = lambda message: None,
) -> SearchResult:
    now = now or datetime.now(timezone.utc)
    settings = plan.settings
    query_tokens = text.tokens(plan.keyword)
    conditions = CONDITION_FILTERS[plan.condition]
    active = [sources[name] for name in plan.sources]
    errors: list[str] = []
    notes: list[str] = []

    if plan.market_price:
        market = MarketStats.manual(plan.market_price)
    else:
        progress(f"「{plan.keyword}」の売り切れ価格を取得中…")
        sold_query = SearchQuery(
            keyword=plan.keyword,
            exclude=plan.exclude,
            price_min=plan.min_price,
            conditions=conditions,
            sold=True,
            pages=plan.sold_pages,
        )
        sold = _collect(active, sold_query, errors, "売り切れ")
        market = estimate_market(
            sold, query_tokens, min_match=settings.min_match, buyer_shipping=settings.buyer_shipping
        )

    price_min, price_max = _on_sale_band(plan, market)
    on_sale: list[Listing] = []
    if price_min and price_max and price_min > price_max:
        notes.append(
            f"指定した価格の範囲と、相場から見て割安な価格帯（{price_min:,}〜{price_max:,}円）が重ならないため、"
            "販売中の検索をしませんでした"
        )
    else:
        progress(f"「{plan.keyword}」の販売中の出品を取得中…")
        on_sale_query = SearchQuery(
            keyword=plan.keyword,
            exclude=plan.exclude,
            price_min=price_min,
            price_max=price_max,
            conditions=conditions,
            pages=plan.pages,
        )
        on_sale = _collect(active, on_sale_query, errors, "販売中")

    if not market.ok and on_sale:
        fallback = estimate_market(
            on_sale,
            query_tokens,
            min_match=settings.min_match,
            buyer_shipping=settings.buyer_shipping,
            basis="on_sale",
        )
        if fallback.ok:
            market = fallback

    def score_all() -> list[Evaluation]:
        return [evaluate(listing, market, query_tokens, settings, now) for listing in on_sale]

    evaluations = score_all()
    ranked = rank(evaluations)
    # 詳細を見て順位が入れ替わったら、新しく上位に来たものも確認する（最大2回）
    for _ in range(2 if plan.details > 0 else 0):
        checked, failed = _check_details(plan, sources, ranked, errors, progress)
        if checked:
            evaluations = score_all()
            ranked = rank(evaluations)
        if failed or not checked:
            break

    return SearchResult(
        plan=plan,
        market=market,
        recommendations=ranked,
        checked=len(on_sale),
        excluded=Counter(e.excluded for e in evaluations if e.excluded),
        errors=errors,
        searched_at=now,
        notes=notes,
    )
