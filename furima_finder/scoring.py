"""販売中の出品を相場と比べて採点し、おすすめ順に並べる。

お得に買う（既定）: 100点満点
  価格 55点（相場より40%以上安いと満点）/ 状態 15点 / 出品者 15点 / 出品の新しさ 10点 / 送料 5点
転売目線（--resale）: 100点満点
  利益額 30点（1万円以上で満点）/ 利益率 20点（60%以上で満点）/ 売れ行き 15点 /
  値動き 15点（直近7日 5点・3か月〜1年前と比べた長期の動き 6点・年代 4点）/ 状態 10点 / 出品者 10点
どちらも、ジャンク・動作未確認などの注意ワードやオークション形式の分を差し引く。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import text
from .eras import Era
from .market import MarketStats, PriceStats, effective_price
from .models import Listing
from .profiles import DEFAULT, Profile

# お得に買う
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
SELLER_POINTS = {"top": 15, "good": 12, "ok": 8, "few": 4, "bad": 0, "shop": 10, "unknown": 7}

# 転売目線
PROFIT_POINTS = 30
PROFIT_FLOOR = 250  # この利益で0点、40倍（1万円）で満点（対数で増える）
ROI_POINTS = 20
FULL_ROI = 0.6
LIQUIDITY_STEPS = [(30, 15), (15, 12), (8, 9), (3, 5)]
UNKNOWN_LIQUIDITY_POINTS = 7
# 値動き（15点）: 直近7日の動き 5点 / 3か月〜1年前と比べた長期の動き 6点 / 年代 4点
SHORT_TREND_STEPS = [(0.10, 5), (-0.05, 3), (-0.15, 1)]
UNKNOWN_SHORT_TREND_POINTS = 2
LONG_TREND_STEPS = [(0.30, 6), (0.10, 5), (-0.10, 3), (-0.25, 1)]
UNKNOWN_LONG_TREND_POINTS = 2
UNKNOWN_ERA_POINTS = 1
OLD_ERA_POINTS = 3  # 年代の点数がこれ以上なら「古いカード」として説明する
HIGH_SPREAD = 0.6
RESALE_CONDITION_POINTS = {1: 10, 2: 9, 3: 7, 4: 3, 5: 1, 6: 0}
UNKNOWN_RESALE_CONDITION_POINTS = 5
RESALE_SELLER_POINTS = {"top": 10, "good": 8, "ok": 5, "few": 3, "bad": 0, "shop": 7, "unknown": 5}

# 傷・汚れありは相場（主に美品の価格）どおりには売れないので、転売の売値を割り引く。
# 売れたカードの多くも同じくらいの状態（旧裏など）なら、その差の分だけ割り引く
CONDITION_SALE_FACTORS = {4: 0.7, 5: 0.5, 6: 0.3}

# 共通の減点
STRONG_RISK_PENALTY = 30
MILD_RISK_PENALTY = 12
DESCRIPTION_RISK_PENALTY = 15  # 説明文に「折れ」「水濡れ」「ジャンク」など重い注意ワード
AUCTION_PENALTY = 20  # 表示価格は途中の入札額で、最終的には相場近くまで上がりやすい
POPULAR_LIKES = 20


@dataclass
class Settings:
    min_discount: float = 0.10  # 相場より何割以上安ければ候補にするか
    min_match: float = 0.5  # タイトルに含まれているべき検索語の割合
    buyer_shipping: int = 800  # 着払いの出品に足す想定送料
    condition_filtered: bool = False
    resale: bool = False  # 転売目線で採点する
    fee_rate: float = 0.10  # メルカリの販売手数料
    resale_shipping: int = 750  # 転売するときの発送の送料
    min_profit: int = 500  # 転売目線: これ未満の利益は候補にしない
    min_roi: float = 0.15  # 転売目線: 利益率（利益 ÷ 仕入れ値）の下限
    min_sales: int = 3  # 転売目線: 直近30日にこれ未満しか売れていないものは候補にしない


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
    # 転売目線のときだけ埋まる
    expected_sale: int | None = None
    profit: int | None = None
    roi: float | None = None
    sales_30d: int | None = None
    trend: float | None = None
    long_trend: float | None = None
    era: Era | None = None  # カードの年代（ポケカのみ）

    @property
    def recommended(self) -> bool:
        return self.excluded is None


def yen(value: int) -> str:
    return f"¥{value:,}"


def signed_pct(value: float) -> str:
    return f"{value:+.0%}".replace("-", "−")


def format_age(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)}分前"
    if minutes < 60 * 24:
        return f"{minutes // 60}時間前"
    return f"{minutes // (60 * 24)}日前"


def format_remaining(remaining: timedelta) -> str:
    minutes = int(remaining.total_seconds() // 60)
    if minutes <= 0:
        return "終了間近"
    if minutes < 60:
        return f"残り{minutes}分"
    if minutes < 60 * 24:
        return f"残り{minutes // 60}時間"
    return f"残り{minutes // (60 * 24)}日"


def _round(value: float) -> int:
    return int(value + 0.5)


def _freshness_points(listing: Listing, now: datetime) -> tuple[int, timedelta | None]:
    if listing.created_at is None:
        return UNKNOWN_FRESHNESS_POINTS, None
    age = max(now - listing.created_at, timedelta(0))
    for limit, points in FRESHNESS_STEPS:
        if age < limit:
            return points, age
    return 0, age


def _seller_tier(listing: Listing) -> tuple[str, str | None, str | None]:
    """（区分, 良い点の説明, 注意点）。区分は shop / top / good / ok / few / bad / unknown。"""
    if listing.is_shop:
        return "shop", "メルカリShops（事業者の出品）", None
    seller = listing.seller
    if seller is None or seller.total_ratings is None:
        return "unknown", None, None
    total = seller.total_ratings
    if total < 5:
        return "few", None, f"出品者の評価が{total}件と少ない"
    ratio = seller.good_ratio
    if ratio is not None:
        label = f"出品者の評価 良い{ratio:.1%}（{total:,}件）"
        if ratio >= 0.99 and total >= 50:
            return "top", label, None
        if ratio >= 0.98 and total >= 20:
            return "good", label, None
        if ratio >= 0.95:
            return "ok", label, None
        return "bad", None, f"{label}：悪い評価が多め"
    if seller.star_rating is not None:
        # 良い/普通/悪い の内訳がなく、5段階の平均だけが分かるとき
        stars = seller.star_rating
        label = f"出品者の評価 ★{stars:.1f}（{total:,}件）"
        if stars >= 4.8 and total >= 50:
            return "top", label, None
        if stars >= 4.6 and total >= 20:
            return "good", label, None
        if stars >= 4.0:
            return "ok", label, None
        return "bad", None, f"{label}：評価が低め"
    return "unknown", f"出品者の評価 {total:,}件", None


def _warn_if_too_cheap(result: Evaluation) -> None:
    if result.discount >= TOO_CHEAP:
        message = "相場の半額以下：偽物・説明不足・写真と違う等がないかよく確認"
        if result.era is not None and result.era.points >= OLD_ERA_POINTS:
            message += "（古いカードはコピー品も出回っています）"
        result.warnings.append(message)


def _bargain_points(result: Evaluation) -> int:
    points = _round(min(max(result.discount, 0.0), FULL_DISCOUNT) / FULL_DISCOUNT * PRICE_POINTS)
    if result.discount > 0:
        saving = result.reference_price - result.effective_price
        result.reasons.append(f"{result.reference_label} {yen(result.reference_price)} より {yen(saving)} 安い")
    _warn_if_too_cheap(result)
    return points


def sale_factor(condition: int | None, typical: int | None) -> float:
    """状態による売値の割引（1 なら割り引かない）。typical は売れた出品のよくある状態。"""
    factor = CONDITION_SALE_FACTORS.get(condition, 1.0) / CONDITION_SALE_FACTORS.get(typical, 1.0)
    return min(factor, 1.0)


def _step_points(value: float, steps: list[tuple[float, int]]) -> int:
    return next((points for minimum, points in steps if value >= minimum), 0)


def _movement_points(result: Evaluation, stats: PriceStats) -> int:
    """値動き（15点）: 直近7日の動き・3か月〜1年前と比べた長期の動き・年代（古いほど値上がりしやすい）。"""
    points = 0
    trend = stats.trend
    if trend is None:
        points += UNKNOWN_SHORT_TREND_POINTS
    else:
        points += _step_points(trend, SHORT_TREND_STEPS)
        if trend >= 0.10:
            result.reasons.append(f"相場が上がっている（直近7日 {signed_pct(trend)}）")
        elif trend < -0.15:
            result.warnings.append(f"相場が下がっている（直近7日 {signed_pct(trend)}）")
        elif trend < -0.05:
            result.warnings.append(f"相場がやや下がっている（直近7日 {signed_pct(trend)}）")

    long_trend = stats.long_trend
    if long_trend is None or stats.past_median is None:
        points += UNKNOWN_LONG_TREND_POINTS
    else:
        points += _step_points(long_trend, LONG_TREND_STEPS)
        since = f"3か月〜1年前の相場 {yen(stats.past_median)} から {signed_pct(long_trend)}"
        if long_trend >= 0.10:
            result.reasons.append(f"長い目で見て値上がりしている（{since}）")
        elif long_trend < -0.10:
            result.warnings.append(f"長い目で見て値下がりしている（{since}）")

    era = result.era
    if era is None:
        points += UNKNOWN_ERA_POINTS
    else:
        points += era.points
        if era.points >= OLD_ERA_POINTS:
            result.reasons.append(f"古い年代のカード（{era.label}・{era.years}）：再販がなく値上がりしやすい")
        elif era.points > 0:
            result.reasons.append(f"再販が終わった年代のカード（{era.label}・{era.years}）")
    return points


def _resale_points(result: Evaluation, stats: PriceStats, settings: Settings, profile: Profile) -> int:
    listing = result.listing
    if stats.spread > HIGH_SPREAD:
        sale, how = stats.q1, "ばらつきが大きいため下位25%で控えめに"
        result.warnings.append("相場のばらつきが大きい（別のカードや状態が混ざっている可能性）")
    elif stats.recent_median:
        sale, how = stats.recent_median, "直近14日の中央値"
    elif stats.month_median:
        # 売れるペースがゆっくりなカードは、昔の安い（高い）売り切れが混ざらないよう直近30日を使う
        sale, how = stats.month_median, "直近30日の中央値"
    else:
        sale, how = stats.median, "中央値"
    factor = sale_factor(listing.condition, stats.condition)
    if factor < 1:
        sale = round(sale * factor)
        how += f"×{factor:.0%}・{listing.condition_label}のため"
    if listing.condition is not None and listing.condition >= 4:
        if factor < 1:
            result.warnings.append("傷・汚れありの状態：売値を低めに見積もっています。実物の傷を写真でよく確認")
        else:
            result.warnings.append(
                "傷・汚れありの状態：売り切れも同じくらいの状態が多いため売値は割り引いていません。実物の傷を写真でよく確認"
            )
    _warn_if_too_cheap(result)
    shipping = profile.resale_shipping(listing, settings.resale_shipping)
    result.expected_sale = sale
    result.profit = round(sale * (1 - settings.fee_rate)) - shipping - result.effective_price
    result.roi = result.profit / result.effective_price
    result.reasons.append(
        f"売値の目安 {yen(sale)}（{result.reference_label}・{how}）、手数料{settings.fee_rate:.0%}と送料{yen(shipping)}を差し引き"
    )

    points = 0
    if result.profit > PROFIT_FLOOR:
        points += _round(min(1.0, math.log(result.profit / PROFIT_FLOOR) / math.log(40)) * PROFIT_POINTS)
    if result.roi > 0:
        points += _round(min(1.0, result.roi / FULL_ROI) * ROI_POINTS)

    if stats.sales_30d is None:
        points += UNKNOWN_LIQUIDITY_POINTS
    else:
        points += next((p for minimum, p in LIQUIDITY_STEPS if stats.sales_30d >= minimum), 0)
        if stats.sales_30d >= 8:
            result.reasons.append(f"直近30日で{stats.sales_30d}件売れている")
        else:
            result.warnings.append(f"売れ行きが少なめ（直近30日で{stats.sales_30d}件）")
    return points + _movement_points(result, stats)


def evaluate(
    listing: Listing,
    market: MarketStats,
    query_tokens: list[str],
    settings: Settings,
    now: datetime,
    profile: Profile = DEFAULT,
) -> Evaluation:
    result = Evaluation(listing=listing)
    result.effective_price = effective_price(listing, settings.buyer_shipping)
    result.match = text.match_ratio(query_tokens, listing.title)
    if listing.sold:
        result.excluded = "売り切れ・取引中"
        return result
    reason = profile.exclusion_reason(listing.title, query_tokens)
    if reason:
        result.excluded = reason
        return result
    if result.match < settings.min_match:
        result.excluded = "キーワード不一致"
        return result
    found = market.lookup(profile.group(listing))
    if found is None:
        result.excluded = "相場不明"
        return result
    stats, group_label = found
    result.reference_price = stats.median
    result.reference_label = f"{group_label}の相場" if group_label else "相場"
    # 浮動小数点の誤差で 20% が 19.999…% にならないよう丸めておく
    result.discount = round(1 - result.effective_price / stats.median, 6)
    result.sales_30d = stats.sales_30d
    result.trend = stats.trend
    result.long_trend = stats.long_trend
    result.era = market.era
    if result.era is None and profile.era_of is not None:
        result.era = profile.era_of([listing.title])

    resale = settings.resale
    points = _resale_points(result, stats, settings, profile) if resale else _bargain_points(result)

    condition_table = RESALE_CONDITION_POINTS if resale else CONDITION_POINTS
    if listing.condition in condition_table:
        points += condition_table[listing.condition]
        result.reasons.append(listing.condition_label or "")
    else:
        points += UNKNOWN_RESALE_CONDITION_POINTS if resale else UNKNOWN_CONDITION_POINTS
        if settings.condition_filtered:
            result.warnings.append("商品の状態が不明（商品ページで確認）")

    if not resale:
        points += SHIPPING_POINTS[listing.shipping_included]
    if listing.shipping_included:
        result.reasons.append("送料込み")
    elif listing.shipping_included is False:
        result.warnings.append(f"着払い（送料 約{yen(settings.buyer_shipping)}を足して計算）")

    fresh_points, age = _freshness_points(listing, now)
    if not resale:
        points += fresh_points
    if age is not None:
        result.reasons.append(f"出品 {format_age(age)}")

    tier, seller_reason, seller_warning = _seller_tier(listing)
    points += (RESALE_SELLER_POINTS if resale else SELLER_POINTS)[tier]
    if seller_reason:
        result.reasons.append(seller_reason)
    if seller_warning:
        result.warnings.append(seller_warning)

    level, word = profile.risk_level(listing.title, query_tokens)
    if level == 2:
        points -= STRONG_RISK_PENALTY
        result.warnings.append(f"タイトルに「{word}」")
    elif level == 1:
        points -= MILD_RISK_PENALTY
        result.warnings.append(f"タイトルに「{word}」")
    if listing.description:
        found_words = [
            found_word
            for found_word in profile.description_risks(listing.description, query_tokens)
            if level == 0 or found_word != word
        ]
        result.warnings.extend(f"説明文に「{found_word}」" for found_word in found_words)
        if any(profile.is_strong_risk(found_word) for found_word in found_words):
            points -= DESCRIPTION_RISK_PENALTY
    if listing.is_auction:
        points -= AUCTION_PENALTY
        details = [f"入札{listing.auction_bids}件"] if listing.auction_bids is not None else []
        if listing.auction_end is not None:
            details.append(format_remaining(listing.auction_end - now))
        suffix = f"（{'・'.join(details)}）" if details else ""
        result.warnings.append(f"オークション形式{suffix}：表示は現在の入札額で、終了までに上がる可能性が高い")
        if resale and result.expected_sale is not None:
            # この金額で落札すると利益がちょうど最低利益になる
            extra = settings.buyer_shipping if listing.shipping_included is False else 0
            max_bid = (
                round(result.expected_sale * (1 - settings.fee_rate))
                - profile.resale_shipping(listing, settings.resale_shipping)
                - settings.min_profit
                - extra
            )
            if max_bid > 0:
                result.reasons.append(
                    f"入札上限の目安 {yen(max_bid)}（ここまでで落札できれば利益{yen(settings.min_profit)}以上）"
                )
    if listing.caution:
        result.warnings.append(listing.caution)
    if listing.likes is not None and listing.likes >= POPULAR_LIKES:
        result.warnings.append(f"いいね{listing.likes}件（人気で売れやすい・早めの判断を）")

    result.score = max(0, min(100, points))
    if resale:
        if result.profit < settings.min_profit:
            result.excluded = "利益が少ない"
        elif result.roi < settings.min_roi:
            result.excluded = "利益率が低い"
        elif result.sales_30d is not None and result.sales_30d < settings.min_sales:
            result.excluded = "売れ行きが少ない"
    elif result.discount < settings.min_discount:
        result.excluded = "割安ではない"
    return result


def rank(evaluations: list[Evaluation]) -> list[Evaluation]:
    recommended = [evaluation for evaluation in evaluations if evaluation.recommended]
    # オークションはすぐには買えず最終価格も分からないので、即決で買える出品の後ろに並べる
    recommended.sort(key=lambda e: (e.listing.is_auction, -e.score, -(e.profit or 0), -e.discount, e.effective_price))
    return recommended
