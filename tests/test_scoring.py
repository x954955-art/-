from datetime import timedelta

import pytest

from furima_finder import text
from furima_finder.market import MarketStats
from furima_finder.models import Seller
from furima_finder.scoring import Settings, evaluate, format_age, rank

from .conftest import make_listing

TOKENS = text.tokens("Switch 有機EL")
MARKET = MarketStats.manual(30000)


def ev(listing, now, settings=None, market=MARKET):
    return evaluate(listing, market, TOKENS, settings or Settings(), now)


def test_discount_and_reasons(now):
    result = ev(make_listing(price=24000, condition=2, age=timedelta(hours=1)), now)
    assert result.recommended
    assert result.discount == pytest.approx(0.2)
    assert result.reference_price == 30000
    assert "相場 ¥30,000 より ¥6,000 安い" in result.reasons
    assert "未使用に近い" in result.reasons
    assert "送料込み" in result.reasons
    assert "出品 1時間前" in result.reasons
    # 価格 28（20%引き / 40%で満点）+ 状態 13 + 送料 5 + 新しさ 10 + 出品者（不明）7
    assert result.score == 63


def test_cash_on_delivery_is_compared_with_shipping(now):
    result = ev(make_listing(price=24000, shipping_included=False), now)
    assert result.effective_price == 24800
    assert any("着払い" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "listing, reason",
    [
        (make_listing(price=20000, sold=True), "売り切れ・取引中"),
        (make_listing("じろう様専用 Switch 有機EL", 20000), "専用・購入不可"),
        (make_listing("Switch 有機EL 箱のみ", 20000), "箱・付属品のみ"),
        (make_listing("PS5 コントローラー", 20000), "キーワード不一致"),
        (make_listing(price=28500), "割安ではない"),
    ],
)
def test_exclusions(listing, reason, now):
    assert ev(listing, now).excluded == reason


def test_without_market_nothing_is_recommended(now):
    assert ev(make_listing(price=1000), now, market=MarketStats(overall=None)).excluded == "相場不明"


def test_risk_words_and_auction_are_penalized(now):
    clean = ev(make_listing(price=20000), now)
    junk = ev(make_listing("Switch 有機EL ジャンク", 20000), now)
    unchecked = ev(make_listing("Switch 有機EL 動作未確認", 20000), now)
    auction = ev(make_listing(price=20000, is_auction=True), now)
    assert clean.score - junk.score == 30
    assert clean.score - unchecked.score == 12
    assert clean.score - auction.score == 20
    assert "タイトルに「ジャンク」" in junk.warnings


def test_seller_ratings_change_the_score(now):
    trusted = make_listing(price=20000, seller=Seller("s1", good=500, bad=1))
    newbie = make_listing(price=20000, seller=Seller("s2", good=2))
    risky = make_listing(price=20000, seller=Seller("s3", good=80, bad=20))
    shop = make_listing(price=20000, is_shop=True)
    scores = {name: ev(item, now).score for name, item in
              {"trusted": trusted, "newbie": newbie, "risky": risky, "shop": shop}.items()}
    assert scores["trusted"] > scores["shop"] > scores["newbie"] > scores["risky"]
    assert any("悪い評価が多め" in w for w in ev(risky, now).warnings)


def test_description_and_likes_add_warnings(now):
    listing = make_listing(price=20000, description="動作未確認のため格安です", likes=35)
    warnings = ev(listing, now).warnings
    assert "説明文に「動作未確認」" in warnings
    assert any("いいね35件" in w for w in warnings)


def test_too_cheap_listing_gets_a_warning(now):
    assert any("半額以下" in w for w in ev(make_listing(price=12000), now).warnings)


def test_resale_profit(now):
    settings = Settings(resale=True, fee_rate=0.10, resale_shipping=750)
    result = ev(make_listing(price=20000), now, settings)
    assert result.profit == 30000 * 0.9 - 750 - 20000


def test_unknown_condition_warns_only_when_filtering(now):
    listing = make_listing(price=20000, condition=None)
    assert not any("状態が不明" in w for w in ev(listing, now).warnings)
    assert any("状態が不明" in w for w in ev(listing, now, Settings(condition_filtered=True)).warnings)


def test_rank_orders_by_score_then_discount(now):
    fresh = make_listing(price=24000, age=timedelta(hours=1))
    old = make_listing(price=24000, age=timedelta(days=40))
    cheaper_old = make_listing(price=21000, age=timedelta(days=40))
    excluded = make_listing(price=29900)
    ranked = rank([ev(item, now) for item in (old, excluded, cheaper_old, fresh)])
    assert [r.listing for r in ranked] == [cheaper_old, fresh, old]


def test_format_age():
    assert format_age(timedelta(seconds=10)) == "1分前"
    assert format_age(timedelta(minutes=59)) == "59分前"
    assert format_age(timedelta(hours=5)) == "5時間前"
    assert format_age(timedelta(days=3, hours=2)) == "3日前"


# ---------------------------------------------------------------- 転売目線

from furima_finder.market import PriceStats  # noqa: E402
from furima_finder.profiles import POKECA  # noqa: E402

CARD_TOKENS = text.tokens("リザードンex SAR")
RESALE = Settings(resale=True, resale_shipping=230, min_profit=1000, buyer_shipping=300, min_match=0.5)


def card_market(group="raw", median=10000, recent=10000, sales=25, trend=0.0, q1=9500, q3=10500):
    stats = PriceStats(20, median, q1, q3, q1, q3, recent_median=recent, sales_30d=sales, trend=trend)
    return MarketStats(overall=stats, groups={group: stats}, labels=dict(POKECA.group_labels), strict=True)


def resale_ev(listing, now, market=None, settings=RESALE, tokens=CARD_TOKENS):
    return evaluate(listing, market or card_market(), tokens, settings, now, POKECA)


def test_resale_profit_roi_and_score(now):
    result = resale_ev(make_listing("リザードンex SAR 201/165", 6000, condition=2), now)
    assert result.recommended
    assert result.expected_sale == 10000
    assert result.profit == 9000 - 230 - 6000
    assert result.roi == pytest.approx(2770 / 6000)
    assert result.reasons[0] == "売値の目安 ¥10,000（未鑑定の相場・直近14日の中央値）、手数料10%と送料¥230を差し引き"
    assert "直近30日で25件売れている" in result.reasons
    # 利益 23 + 利益率 15 + 売れ行き 12 + 相場の動き 7 + 状態 9 + 出品者（不明）5
    assert result.score == 71


@pytest.mark.parametrize(
    "price, settings, market, reason",
    [
        (8500, RESALE, None, "利益が少ない"),
        (8000, Settings(resale=True, resale_shipping=230, min_profit=0), None, "利益率が低い"),
        (6000, RESALE, card_market(sales=2), "売れ行きが少ない"),
    ],
)
def test_resale_exclusions(price, settings, market, reason, now):
    assert resale_ev(make_listing("リザードンex SAR 201/165", price), now, market, settings).excluded == reason


def test_unknown_sales_activity_is_not_excluded(now):
    result = resale_ev(make_listing("リザードンex SAR 201/165", 6000), now, card_market(recent=None, sales=None, trend=None))
    assert result.recommended
    assert result.expected_sale == 10000  # 直近の中央値がなければ全体の中央値


def test_falling_and_rising_markets(now):
    listing = make_listing("リザードンex SAR 201/165", 6000)
    falling = resale_ev(listing, now, card_market(trend=-0.2))
    rising = resale_ev(listing, now, card_market(trend=0.12))
    assert "相場が下がっている（直近7日 −20%）" in falling.warnings
    assert "相場が上がっている（直近7日 +12%）" in rising.reasons
    assert rising.score - falling.score == 10


def test_wide_spread_uses_conservative_sale_price(now):
    result = resale_ev(make_listing("リザードンex SAR 201/165", 3000), now, card_market(q1=6000, q3=14000))
    assert result.expected_sale == 6000
    assert any("ばらつき" in warning for warning in result.warnings)


def test_damaged_cards_warn_in_resale_mode(now):
    result = resale_ev(make_listing("リザードンex SAR 201/165", 6000, condition=4), now)
    assert any("傷・汚れあり" in warning for warning in result.warnings)


def test_graded_card_needs_the_same_grade_market(now):
    result = resale_ev(make_listing("【PSA10】リザードンex SAR 201/165", 20000), now)
    assert result.excluded == "相場不明"


def test_box_uses_larger_shipping(now):
    market = card_market(group="box_shrink", median=30000, recent=30000)
    box = make_listing("ポケモンカード 151 BOX シュリンク付き", 20000)
    result = resale_ev(box, now, market, tokens=text.tokens("151 BOX"))
    assert result.profit == 27000 - 520 - 20000



def test_star_ratings_and_unknown_sellers(now):
    def seller_result(seller):
        return ev(make_listing(price=20000, seller=seller), now)

    stars = seller_result(Seller("s", num_ratings=120, star_rating=4.9, breakdown_known=False))
    assert "出品者の評価 ★4.9（120件）" in stars.reasons
    low = seller_result(Seller("s", num_ratings=40, star_rating=3.5, breakdown_known=False))
    assert any("評価が低め" in w for w in low.warnings)
    unknown = seller_result(Seller("s", breakdown_known=False))
    assert not any("出品者" in w for w in unknown.warnings)  # 形式が分からないときに「0件」と言わない
    assert stars.score > unknown.score > low.score


def test_auction_shows_bids_time_left_and_max_bid(now):
    from datetime import timedelta

    auction = make_listing(
        "リザードンex SAR 201/165", 6000, is_auction=True, auction_bids=3, auction_end=now + timedelta(hours=5)
    )
    result = resale_ev(auction, now)
    assert "オークション形式（入札3件・残り5時間）：表示は現在の入札額で、終了までに上がる可能性が高い" in result.warnings
    # 売値の目安 10,000円 × 0.9 − 送料 230円 − 最低利益 1,000円
    assert "入札上限の目安 ¥7,770（ここまでで落札できれば利益¥1,000以上）" in result.reasons


def test_auctions_rank_after_buy_now_listings(now):
    auction = resale_ev(make_listing("リザードンex SAR 201/165", 3000, is_auction=True), now)
    buy_now = resale_ev(make_listing("リザードンex SAR 201/165", 7500), now)
    assert auction.score > buy_now.score
    assert rank([auction, buy_now]) == [buy_now, auction]


def test_heavy_damage_in_description_is_penalized(now):
    clean = ev(make_listing(price=20000, description="目立った傷や汚れはありません"), now)
    broken = ev(make_listing(price=20000, description="角に破損があります"), now)
    mild = ev(make_listing(price=20000, description="動作未確認です"), now)
    assert not any("説明文" in w for w in clean.warnings)
    assert "説明文に「破損」" in broken.warnings
    assert clean.score - broken.score == 15
    assert "説明文に「動作未確認」" in mild.warnings and mild.score == clean.score  # 軽い注意は減点しない


def test_damaged_cards_are_valued_lower_for_resale(now):
    # 実際に出た例: 相場 5,090円の SAR が「やや傷や汚れあり」で 2,700円、説明文に「折れ」「水濡れ」
    market = card_market(median=5090, recent=None, sales=6, trend=None, q1=4900, q3=5300)
    listing = make_listing(
        "パラソルおねえさん SAR SV3a レイジングサーフ 089/062",
        2700,
        condition=4,
        description="折れと水濡れがあります",
    )
    result = resale_ev(listing, now, market)
    assert result.expected_sale == round(5090 * 0.7)
    assert "×70%・やや傷や汚れありのため" in result.reasons[0]
    assert result.excluded == "利益が少ない"
    assert "説明文に「折れ」" in result.warnings and "説明文に「水濡れ」" in result.warnings
