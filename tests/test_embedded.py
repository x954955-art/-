"""ページ埋め込み JSON から出品を読む処理と、Yahoo!フリマ・スニダンのテスト。

fixtures/yahoo_search.html・snkrdunk_search.html は実際のページの保存ではなく、
よくある形（Next.js の __NEXT_DATA__、JSON-LD）を想定して作った例。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from furima_finder import text
from furima_finder.http import FetchError, HttpClient
from furima_finder.models import SearchQuery
from furima_finder.sources.embedded import (
    best_item_list,
    extract_embedded_json,
    find_item_lists,
    parse_json_text,
    to_listing,
)
from furima_finder.sources.snkrdunk import SnkrdunkSource
from furima_finder.sources.yahoo import YahooFleaSource

FIXTURES = Path(__file__).parent / "fixtures"
YAHOO = (FIXTURES / "yahoo_search.html").read_text(encoding="utf-8")
SNKR = (FIXTURES / "snkrdunk_search.html").read_text(encoding="utf-8")


def test_finds_the_search_results_not_the_recommendations():
    blobs = extract_embedded_json(YAHOO)
    candidates = find_item_lists(blobs)
    assert {c.path for c in candidates} == {
        "#0.props.pageProps.initialState.search.items",
        "#0.props.pageProps.initialState.recommend.items",
    }
    best = best_item_list(candidates, text.tokens("ポケモンカード SAR"))
    assert [item["id"] for item in best] == ["z100000001", "z100000002", "z100000003"]


def test_to_listing_reads_common_fields():
    item = {"id": "z1", "title": "リザードンex SAR", "price": "¥9,800", "openTime": 1761339600000,
            "condition": "未使用に近い", "thumbnailImageUrl": "https://img/x.jpg"}
    listing = to_listing(item, source="yahoo", base_url="https://example.jp", url_for_id=lambda i: f"https://example.jp/item/{i}")
    assert (listing.id, listing.price, listing.condition) == ("z1", 9800, 2)
    assert listing.url == "https://example.jp/item/z1"
    assert listing.created_at == datetime.fromtimestamp(1761339600, tz=timezone.utc)
    assert listing.thumbnail == "https://img/x.jpg"
    naive = to_listing({"title": "x SAR", "price": 1, "createdAt": "2026-10-24T10:00:00", "path": "/item/9"},
                       source="yahoo", base_url="https://example.jp")
    assert naive.url == "https://example.jp/item/9"
    assert naive.created_at.utcoffset() == timedelta(hours=9)  # 時差の書かれていない日時は日本時間
    assert to_listing({"title": "x", "price": 0}, source="y", base_url="") is None
    assert to_listing({"title": "x SAR", "price": 5}, source="y", base_url="") is None  # URL が作れない
    sold = to_listing({"id": "z2", "title": "x SAR", "price": 5, "itemStatus": "SOLD_OUT"}, source="y",
                      base_url="", url_for_id=str)
    assert sold.sold


def test_parse_json_text_accepts_api_json_and_html():
    assert parse_json_text('{"items": []}') == [{"items": []}]
    assert parse_json_text(YAHOO)[0]["page"] == "/search/[keyword]"
    assert parse_json_text("<html>no json</html>") == []


class Response:
    def __init__(self, body, status_code=200, url=""):
        self.text = body
        self.status_code = status_code
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.url = url


class Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, params=None, **kwargs):
        self.calls.append((url, params))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def source(cls, outcomes):
    session = Session(outcomes)
    return cls(HttpClient(session=session, sleep=lambda s: None, min_interval=0, max_retries=0)), session


def test_yahoo_reads_open_listings_from_the_search_page():
    yahoo, session = source(YahooFleaSource, [Response(YAHOO)])
    listings = yahoo.search(SearchQuery(keyword="ポケモンカード SAR", price_min=5000))
    assert [listing.id for listing in listings] == ["z100000001", "z100000002"]  # 売り切れと価格帯外は除く
    first = listings[0]
    assert first.source == "yahoo" and first.source_label == "Yahoo!フリマ"
    assert first.url == "https://paypayfleamarket.yahoo.co.jp/item/z100000001"
    assert first.shipping_included is True
    assert first.condition == 2
    assert (first.seller.total_ratings, round(first.seller.good_ratio, 2)) == (150, 0.99)
    assert (listings[1].seller.total_ratings, listings[1].seller.good_ratio) == (10, 0.8)
    assert listings[1].price == 7500
    (url, params), = session.calls
    assert url.startswith("https://paypayfleamarket.yahoo.co.jp/search/%E3%83%9D")
    assert params["open"] == "1" and params["minPrice"] == "5000"
    assert yahoo.search(SearchQuery(keyword="x", sold=True)) == []  # 売り切れの検索はしない


def test_yahoo_falls_back_to_the_api_and_reports_errors():
    api_body = json.dumps({"items": [
        {"id": f"z{i}", "title": f"リザードンex SAR {i}", "price": 1000 + i} for i in range(3)
    ]})
    yahoo, session = source(YahooFleaSource, [Response("<html><body>空</body></html>"), Response(api_body)])
    assert len(yahoo.search(SearchQuery(keyword="リザードンex SAR"))) == 3
    assert session.calls[1][0] == "https://paypayfleamarket.yahoo.co.jp/api/v1/search"

    yahoo, _ = source(YahooFleaSource, [Response("", 403), Response("", 403)])
    with pytest.raises(FetchError, match="HTTP 403"):
        yahoo.search(SearchQuery(keyword="リザードンex SAR"))


def test_snkrdunk_reads_products_and_marks_them():
    snkr, _ = source(SnkrdunkSource, [Response(SNKR)])
    listings = snkr.search(SearchQuery(keyword="SAR"))
    by_price = {listing.price: listing for listing in listings}
    assert set(by_price) == {12800, 9900, 11500}
    assert by_price[12800].url == "https://snkrdunk.com/apparels/100001"
    assert by_price[9900].url == "https://snkrdunk.com/apparels/100002"
    assert by_price[11500].url.startswith("https://snkrdunk.com/search?keywords=")
    assert all(listing.caution and "最安値" in listing.caution for listing in listings)


def test_caution_shows_up_as_a_warning():
    from datetime import datetime as dt

    from furima_finder.market import MarketStats
    from furima_finder.scoring import Settings, evaluate

    snkr, _ = source(SnkrdunkSource, [Response(SNKR)])
    listing = next(item for item in snkr.search(SearchQuery(keyword="SAR")) if item.price == 9900)
    result = evaluate(listing, MarketStats.manual(15000), [], Settings(), dt.now(timezone.utc))
    assert any("スニダンの価格は最安値の目安" in w for w in result.warnings)



@pytest.mark.parametrize(
    "value, expected",
    [("new", 1), ("used10", 2), ("used20", 3), ("USED_40", 4), ("used60", 5), ("used80", 6), ("未使用に近い", 2), ("良好", None)],
)
def test_condition_codes(value, expected):
    listing = to_listing({"id": "z1", "title": "x SAR", "price": 1, "condition": value}, source="yahoo",
                         base_url="", url_for_id=str)
    assert listing.condition == expected


def test_snkrdunk_reads_product_links_from_html():
    from furima_finder.sources.embedded import extract_link_cards

    html = (FIXTURES / "snkrdunk_search_html.html").read_text(encoding="utf-8")
    cards = extract_link_cards(html, "https://snkrdunk.com")
    assert [(c["name"], c["price"], c["url"]) for c in cards] == [
        ("ミュウex SAR [M6a 129/103](拡張パック「30th CELEBRATION」)", 12800, "https://snkrdunk.com/apparels/129103"),
        ("リザードンex SAR [SV2a 201/165]", 15500, "https://snkrdunk.com/apparels/201165"),
        ("ナンジャモ SAR [SV2D 096/071]", 9800, "https://snkrdunk.com/apparels/96071"),
    ]
    assert cards[0]["image"] == "https://cdn.snkrdunk.com/1.jpg"

    snkr, _ = source(SnkrdunkSource, [Response(html)])
    listings = snkr.search(SearchQuery(keyword="ポケモンカード SAR"))
    assert [listing.price for listing in listings] == [12800, 15500, 9800]
    assert listings[0].url == "https://snkrdunk.com/apparels/129103"
    assert listings[0].caution


def test_html_links_are_not_used_for_json_sites():
    yahoo, _ = source(YahooFleaSource, [Response((FIXTURES / "snkrdunk_search_html.html").read_text(encoding="utf-8")), Response("{}")])
    assert yahoo.search(SearchQuery(keyword="SAR")) == []



def test_seller_without_ratio_keeps_the_count_only():
    listing = to_listing({"id": "z1", "title": "x SAR", "price": 100, "seller": {"id": "s", "numRating": 3}},
                         source="yahoo", base_url="", url_for_id=str)
    assert listing.seller.total_ratings == 3 and listing.seller.good_ratio is None
    no_rating = to_listing({"id": "z2", "title": "x SAR", "price": 100, "seller": {"id": "s"}},
                           source="yahoo", base_url="", url_for_id=str)
    assert no_rating.seller is None
