"""キーワードごとに「相場を調べる → 販売中を集める → 採点する」までを行う。

- run_search: 1つの商品（キーワード）について、売り切れから相場を出して販売中を採点する
- run_scan  : 広いキーワード（例「ポケモンカード SAR」）で販売中を集め、カード番号ごとに
              売り切れを調べて、カードをまたいで転売候補を探す
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import text
from .http import FetchError
from .market import MarketStats, estimate_market
from .models import Listing, SearchQuery
from .profiles import Profile, get_profile, most_common_rarity
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
SCAN_DEFAULT_MIN_PRICE = 1000  # スキャンでは安すぎるカードは利益が出にくいので既定で除く


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
    profile: str = "default"
    scan: bool = False
    scan_cards: int = 20  # スキャンで相場を調べるカードの種類数
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
    """販売中を検索する価格帯。相場から見て割安（転売なら利益が出る）になりうる範囲だけを取りに行く。"""
    price_min, price_max = plan.min_price, plan.max_price
    reference = market.reference_range()
    if reference is None:
        return price_min, price_max
    lowest, highest = reference
    settings = plan.settings
    band_min = int(lowest * plan.floor_ratio)
    if settings.resale:
        band_max = int(highest * (1 - settings.fee_rate)) - settings.resale_shipping - settings.min_profit
    else:
        band_max = int(highest * (1 - settings.min_discount))
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


def _rank_with_details(
    plan: SearchPlan,
    sources: Mapping[str, Source],
    score_all: Callable[[], list[Evaluation]],
    errors: list[str],
    progress: Callable[[str], None],
) -> tuple[list[Evaluation], list[Evaluation]]:
    """採点して並べ、上位の詳細を確認して採点し直す。（全評価, おすすめ順）を返す。"""
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
    return evaluations, ranked


def run_search(
    plan: SearchPlan,
    sources: Mapping[str, Source],
    *,
    now: datetime | None = None,
    progress: Callable[[str], None] = lambda message: None,
) -> SearchResult:
    now = now or datetime.now(timezone.utc)
    settings = plan.settings
    profile = get_profile(plan.profile)
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
            sold,
            query_tokens,
            min_match=settings.min_match,
            buyer_shipping=settings.buyer_shipping,
            profile=profile,
            now=now,
        )

    price_min, price_max = _on_sale_band(plan, market)
    on_sale: list[Listing] = []
    if price_max is not None and (price_max <= 0 or (price_min and price_min > price_max)):
        notes.append("指定した価格の範囲では、相場から見て割安な（利益の出る）出品が見込めないため、販売中の検索をしませんでした")
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
            profile=profile,
            now=now,
        )
        if fallback.ok:
            market = fallback

    def score_all() -> list[Evaluation]:
        return [evaluate(listing, market, query_tokens, settings, now, profile) for listing in on_sale]

    evaluations, ranked = _rank_with_details(plan, sources, score_all, errors, progress)
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


def card_query(key: str, listings: list[Listing], profile: Profile) -> str:
    """カードの売り切れを探すキーワード（カード番号＋よく書かれているレアリティ）。"""
    rarity = most_common_rarity(profile, (listing.title for listing in listings))
    return f"{key} {rarity}" if rarity else key


def run_scan(
    plan: SearchPlan,
    sources: Mapping[str, Source],
    *,
    now: datetime | None = None,
    progress: Callable[[str], None] = lambda message: None,
) -> SearchResult:
    now = now or datetime.now(timezone.utc)
    profile = get_profile(plan.profile)
    if profile.card_key is None:
        raise ValueError(f"プロファイル {profile.name} はスキャンに対応していません")
    # カード番号で同じカードかどうかを判定するので、タイトルとキーワードの一致は見ない
    settings = replace(plan.settings, min_match=0.0)
    query_tokens = text.tokens(plan.keyword)
    conditions = CONDITION_FILTERS[plan.condition]
    active = [sources[name] for name in plan.sources]
    errors: list[str] = []
    notes: list[str] = []

    progress(f"「{plan.keyword}」の販売中の出品を集めています…")
    on_sale_query = SearchQuery(
        keyword=plan.keyword,
        exclude=plan.exclude,
        price_min=plan.min_price if plan.min_price is not None else SCAN_DEFAULT_MIN_PRICE,
        price_max=plan.max_price,
        conditions=conditions,
        pages=plan.pages,
    )
    on_sale = _collect(active, on_sale_query, errors, "販売中")

    skipped: Counter[str] = Counter()
    cards: dict[str, list[Listing]] = {}
    for listing in on_sale:
        reason = "売り切れ・取引中" if listing.sold else profile.exclusion_reason(listing.title, query_tokens)
        key = None if reason else profile.card_key(listing.title)
        if reason or key is None:
            skipped[reason or "カード番号なし"] += 1
            continue
        cards.setdefault(key, []).append(listing)

    # 新しい出品があるカードから順に（安い新着はすぐ売れるため）、同じなら高いカードを優先
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(
        cards.items(),
        key=lambda item: (max(x.created_at or oldest for x in item[1]), max(x.price for x in item[1])),
        reverse=True,
    )
    targets = ordered[: plan.scan_cards]
    markets: dict[str, MarketStats] = {}
    sold_sources = [source for source in active if source.supports_sold]
    for index, (key, listings) in enumerate(targets, 1):
        if not sold_sources:
            notes.append("取得エラーが続いたため、残りのカードの相場確認を中止しました")
            break
        progress(f"[{index}/{len(targets)}] {key} の売り切れ価格を取得中…")
        sold_query = SearchQuery(
            keyword=card_query(key, listings, profile), exclude=plan.exclude, conditions=conditions, sold=True, pages=1
        )
        sold: list[Listing] = []
        for source in list(sold_sources):
            try:
                sold.extend(source.search(sold_query))
            except FetchError as exc:
                errors.append(f"{source.label}（売り切れ {key}）: {exc}")
                sold_sources.remove(source)  # 同じエラーを繰り返さない
        same_card = [listing for listing in sold if profile.card_key(listing.title) == key]
        markets[key] = estimate_market(
            same_card,
            query_tokens,
            min_match=0.0,
            buyer_shipping=settings.buyer_shipping,
            profile=profile,
            now=now,
        )
    if len(cards) > len(markets):
        notes.append(
            f"カード {len(cards)}種類のうち、新しい出品があるものから{len(markets)}種類の相場を調べました"
            "（--scan-cards で増やせます）"
        )

    def score_all() -> list[Evaluation]:
        return [
            evaluate(listing, markets[key], query_tokens, settings, now, profile)
            for key, listings in targets
            if key in markets
            for listing in listings
        ]

    evaluations, ranked = _rank_with_details(plan, sources, score_all, errors, progress)
    excluded = Counter(e.excluded for e in evaluations if e.excluded)
    excluded.update(skipped)
    return SearchResult(
        plan=plan,
        market=MarketStats(overall=None, basis="scan", fetched=sum(m.fetched for m in markets.values())),
        recommendations=ranked,
        checked=len(on_sale),
        excluded=excluded,
        errors=errors,
        searched_at=now,
        notes=notes,
    )


def run_plan(
    plan: SearchPlan,
    sources: Mapping[str, Source],
    *,
    now: datetime | None = None,
    progress: Callable[[str], None] = lambda message: None,
) -> SearchResult:
    runner = run_scan if plan.scan else run_search
    return runner(plan, sources, now=now, progress=progress)
