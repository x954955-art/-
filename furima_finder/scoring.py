"""販売中の出品を相場と比べて採点し、おすすめ順に並べる。

スコアは100点満点:
  価格 55点（相場より40%以上安いと満点）/ 状態 15点 / 送料 5点 / 出品の新しさ 10点 / 出品者 15点
から、ジャンク・動作未確認などの注意ワードやオークション形式の分を差し引く。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import text
from .market import MarketStats, effective_price
from .models import Listing

PRICE_POINTS = 55
FULL_DISCOUNT = 0.4
TOO_CHEAP = 0.5  # 相場の半額以下は念のため注意を出す
CONDITION_POINTS = {1: 15, 2: 13, 3: 10, 4: 5, 5: 2, 6: 0}
UNKNOWN_CONDITION_POINTS = 7
SHIPPING_POINTS = {True: 5, False: 0, None: 2}
FRESHNESS_STEPS = [
    (timedelta(hours=3), 10),
    (timedelta(hours=12), 8),
    (timedelta(days=1), 7),
    (timedelta(days=3), 5),
    (timedelta(days=7), 3),
    (timedelta(days=30), 1),
]
UNKNOWN_FRESHNESS_POINTS = 3
UNKNOWN_SELLER_POINTS = 7
STRONG_RISK_PENALTY = 30
MILD_RISK_PENALTY = 12
AUCTION_PENALTY = 20  # 表示価格は途中の入札額で、最終的には相場近くまで上がりやすい
POPULAR_LIKES = 20


@dataclass
class Settings:
    min_discount: float = 0.10  # 相場より何割以上安ければ候補にするか
    min_match: float = 0.5  # タイトルに含まれているべき検索語の割合
    buyer_shipping: int = 800  # 着払いの出品に足す想定送料
    condition_filtered: bool = False
    resale: bool = False
    fee_rate: float = 0.10  # メルカリの販売手数料
    resale_shipping: int = 750  # 転売するときの想定送料


@dataclass
class Evaluation:
    listing: Listing
    score: int = 0
    effective_price: int = 0
    reference_price: int | None = None
    reference_label: str = ""
    discount: float = 0.0
    match: float = 1.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    excluded: str | None = None  # 候補から外した理由
    profit: int | None = None

    @property
    def recommended(self) -> bool:
        return self.excluded is None


def yen(value: int) -> str:
    return f"¥{value:,}"


def format_age(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)}分前"
    if minutes < 60 * 24:
        return f"{minutes // 60}時間前"
    return f"{minutes // (60 * 24)}日前"


def _freshness_points(listing: Listing, now: datetime) -> tuple[int, timedelta | None]:
    if listing.created_at is None:
        return UNKNOWN_FRESHNESS_POINTS, None
    age = max(now - listing.created_at, timedelta(0))
    for limit, points in FRESHNESS_STEPS:
        if age < limit:
            return points, age
    return 0, age


def _seller_points(listing: Listing) -> tuple[int, str | None, str | None]:
    """（点数, 良い点の説明, 注意点）"""
    if listing.is_shop:
        return 10, "メルカリShops（事業者の出品）", None
    seller = listing.seller
    if seller is None:
        return UNKNOWN_SELLER_POINTS, None, None
    total, ratio = seller.total_ratings, seller.good_ratio
    if ratio is None or total < 5:
        return 4, None, f"出品者の評価が{total}件と少ない"
    label = f"出品者の評価 良い{ratio:.1%}（{total:,}件）"
    if ratio >= 0.99 and total >= 50:
        return 15, label, None
    if ratio >= 0.98 and total >= 20:
        return 12, label, None
    if ratio >= 0.95:
        return 8, label, None
    return 0, None, f"{label}：悪い評価が多め"


def evaluate(
    listing: Listing,
    market: MarketStats,
    query_tokens: list[str],
    settings: Settings,
    now: datetime,
) -> Evaluation:
    result = Evaluation(listing=listing)
    result.effective_price = effective_price(listing, settings.buyer_shipping)
    result.match = text.match_ratio(query_tokens, listing.title)
    if listing.sold:
        result.excluded = "売り切れ・取引中"
        return result
    reason = text.exclusion_reason(listing.title, query_tokens)
    if reason:
        result.excluded = reason
        return result
    if result.match < settings.min_match:
        result.excluded = "キーワード不一致"
        return result
    reference = market.reference(listing.condition)
    if reference is None:
        result.excluded = "相場不明"
        return result
    result.reference_price, result.reference_label = reference
    # 浮動小数点の誤差で 20% が 19.999…% にならないよう丸めておく
    result.discount = round(1 - result.effective_price / result.reference_price, 6)

    points = int(min(max(result.discount, 0.0), FULL_DISCOUNT) / FULL_DISCOUNT * PRICE_POINTS + 0.5)
    if result.discount > 0:
        saving = result.reference_price - result.effective_price
        result.reasons.append(
            f"{result.reference_label} {yen(result.reference_price)} より {yen(saving)} 安い"
        )
    if result.discount >= TOO_CHEAP:
        result.warnings.append("相場の半額以下：偽物・説明不足・写真と違う等がないかよく確認")

    if listing.condition in CONDITION_POINTS:
        points += CONDITION_POINTS[listing.condition]
        result.reasons.append(listing.condition_label or "")
    else:
        points += UNKNOWN_CONDITION_POINTS
        if settings.condition_filtered:
            result.warnings.append("商品の状態が不明（商品ページで確認）")

    points += SHIPPING_POINTS[listing.shipping_included]
    if listing.shipping_included:
        result.reasons.append("送料込み")
    elif listing.shipping_included is False:
        result.warnings.append(f"着払い（送料 約{yen(settings.buyer_shipping)}を足して比較）")

    fresh_points, age = _freshness_points(listing, now)
    points += fresh_points
    if age is not None:
        result.reasons.append(f"出品 {format_age(age)}")

    seller_points, seller_reason, seller_warning = _seller_points(listing)
    points += seller_points
    if seller_reason:
        result.reasons.append(seller_reason)
    if seller_warning:
        result.warnings.append(seller_warning)

    level, word = text.risk_level(listing.title, query_tokens)
    if level == 2:
        points -= STRONG_RISK_PENALTY
        result.warnings.append(f"タイトルに「{word}」")
    elif level == 1:
        points -= MILD_RISK_PENALTY
        result.warnings.append(f"タイトルに「{word}」")
    if listing.description:
        for found in text.description_risks(listing.description, query_tokens):
            if level == 0 or found != word:
                result.warnings.append(f"説明文に「{found}」")
    if listing.is_auction:
        points -= AUCTION_PENALTY
        result.warnings.append("オークション形式：表示は現在の入札額で、終了までに上がる可能性が高い")
    if listing.likes is not None and listing.likes >= POPULAR_LIKES:
        result.warnings.append(f"いいね{listing.likes}件（人気で売れやすい・早めの判断を）")

    result.score = max(0, min(100, points))
    if settings.resale:
        sale = result.reference_price
        result.profit = round(sale * (1 - settings.fee_rate)) - settings.resale_shipping - result.effective_price
    if result.discount < settings.min_discount:
        result.excluded = "割安ではない"
    return result


def rank(evaluations: list[Evaluation]) -> list[Evaluation]:
    recommended = [evaluation for evaluation in evaluations if evaluation.recommended]
    recommended.sort(key=lambda e: (-e.score, -e.discount, e.effective_price))
    return recommended
