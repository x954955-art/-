"""メルカリ取得クライアントのテスト。

fixtures/mercari_*.json は実際の通信を記録したものではなく、mercapi 0.5.0 が
読んでいる項目名に合わせて作った応答例。通信は一切しない。
"""

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from furima_finder.http import HttpClient
from furima_finder.models import Listing, SearchQuery
from furima_finder.sources.mercari import (
    ITEM_URL,
    SEARCH_URL,
    DPoPSigner,
    MercariSource,
    apply_item_detail,
    build_search_body,
    parse_search_item,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def verify_dpop(token: str) -> tuple[dict, dict]:
    """JWT を分解し、ヘッダーの公開鍵で署名を検証する。"""
    header_b64, payload_b64, signature_b64 = token.split(".")
    header = json.loads(b64decode(header_b64))
    payload = json.loads(b64decode(payload_b64))
    jwk = header["jwk"]
    public_key = ec.EllipticCurvePublicNumbers(
        int.from_bytes(b64decode(jwk["x"]), "big"),
        int.from_bytes(b64decode(jwk["y"]), "big"),
        ec.SECP256R1(),
    ).public_key()
    raw = b64decode(signature_b64)
    assert len(raw) == 64
    signature = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public_key.verify(signature, f"{header_b64}.{payload_b64}".encode(), ec.ECDSA(hashes.SHA256()))
    return header, payload


def test_dpop_token_is_a_valid_es256_jwt():
    signer = DPoPSigner(device_uuid="device-1", clock=lambda: 1761280000.7)
    header, payload = verify_dpop(signer.sign("post", SEARCH_URL))
    assert header["typ"] == "dpop+jwt"
    assert header["alg"] == "ES256"
    assert set(header["jwk"]) == {"crv", "kty", "x", "y"}
    assert payload["htu"] == SEARCH_URL
    assert payload["htm"] == "POST"
    assert payload["iat"] == 1761280000
    assert payload["uuid"] == "device-1"
    assert payload["jti"]


def test_dpop_tokens_are_unique_and_tampering_is_detected():
    signer = DPoPSigner()
    first, second = signer.sign("GET", ITEM_URL), signer.sign("GET", ITEM_URL)
    assert verify_dpop(first)[1]["jti"] != verify_dpop(second)[1]["jti"]
    header_b64, _, signature_b64 = first.split(".")
    forged_payload = base64.urlsafe_b64encode(b'{"htu":"x"}').rstrip(b"=").decode()
    with pytest.raises(InvalidSignature):
        verify_dpop(f"{header_b64}.{forged_payload}.{signature_b64}")


def test_search_body_for_on_sale_and_sold():
    query = SearchQuery(
        keyword="Switch 有機EL", exclude=["ジャンク", "箱のみ"], price_min=9000, price_max=27000, conditions=[1, 2]
    )
    body = build_search_body(query, page_token="v1:1", session_id="sess", device_uuid="dev")
    condition = body["searchCondition"]
    assert body["pageToken"] == "v1:1"
    assert body["pageSize"] == 120
    assert body["searchSessionId"] == "sess"
    assert body["laplaceDeviceUuid"] == "dev"
    assert condition["keyword"] == "Switch 有機EL"
    assert condition["excludeKeyword"] == "ジャンク 箱のみ"
    assert condition["status"] == ["STATUS_ON_SALE"]
    assert (condition["sort"], condition["order"]) == ("SORT_CREATED_TIME", "ORDER_DESC")
    assert (condition["priceMin"], condition["priceMax"]) == (9000, 27000)
    assert condition["itemConditionId"] == [1, 2]

    sold = build_search_body(SearchQuery(keyword="Switch", sold=True, sort="price_asc"))
    assert sold["searchCondition"]["status"] == ["STATUS_SOLD_OUT", "STATUS_TRADING"]
    assert (sold["searchCondition"]["priceMin"], sold["searchCondition"]["priceMax"]) == (0, 0)
    assert (sold["searchCondition"]["sort"], sold["searchCondition"]["order"]) == ("SORT_PRICE", "ORDER_ASC")


def test_parse_search_items():
    items = load("mercari_search_page1.json")["items"] + load("mercari_search_page2.json")["items"]
    normal, shop, no_price, auction, _ = [parse_search_item(raw) for raw in items]

    assert normal.id == "m10000000001"
    assert normal.url == "https://jp.mercari.com/item/m10000000001"
    assert normal.price == 29800
    assert normal.condition == 2
    assert normal.shipping_included is True
    assert normal.created_at.timestamp() == 1761280000
    assert normal.thumbnail.startswith("https://static.mercdn.net/")
    assert not normal.sold and not normal.is_shop and not normal.is_auction

    assert shop.is_shop
    assert shop.url == "https://jp.mercari.com/shops/product/2a1B3c4D5e6F7g8H9iJk"
    assert shop.seller_id is None

    assert no_price is None

    assert auction.sold
    assert auction.is_auction
    assert auction.auction_end == datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    assert auction.auction_bids == 3
    assert auction.shipping_included is False
    assert auction.condition == 4
    assert normal.auction_end is None and normal.auction_bids is None


def test_parse_search_item_rejects_broken_rows():
    assert parse_search_item({}) is None
    assert parse_search_item({"id": "m1", "price": "abc"}) is None
    assert parse_search_item({"id": "m1", "price": "0"}) is None
    odd = parse_search_item({"id": "m1", "name": "x", "price": "100", "itemConditionId": "9", "shippingPayerId": "7"})
    assert odd.condition is None and odd.shipping_included is None


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


def make_source(responses):
    session = RecordingSession(responses)
    http = HttpClient(session=session, sleep=lambda seconds: None, min_interval=0)
    return MercariSource(http), session


def test_search_follows_pages_and_signs_every_request():
    source, session = make_source(
        [FakeResponse(load("mercari_search_page1.json")), FakeResponse(load("mercari_search_page2.json"))]
    )
    listings = source.search(SearchQuery(keyword="Switch 有機EL", pages=5))

    assert [listing.id for listing in listings] == ["m10000000001", "2a1B3c4D5e6F7g8H9iJk", "m10000000004"]
    assert len(session.calls) == 2  # nextPageToken が空になったら止まる
    first, second = session.calls
    assert first["method"] == "POST" and first["url"] == SEARCH_URL
    assert first["json"]["pageToken"] == "" and second["json"]["pageToken"] == "v1:1"
    assert first["json"]["searchSessionId"] == second["json"]["searchSessionId"]
    for call in session.calls:
        headers = call["headers"]
        assert headers["X-Platform"] == "web"
        assert headers["Content-Type"] == "application/json"
        _, payload = verify_dpop(headers["DPoP"])
        assert (payload["htm"], payload["htu"]) == ("POST", SEARCH_URL)
    assert first["headers"]["DPoP"] != second["headers"]["DPoP"]


def test_search_stops_at_page_limit():
    source, session = make_source([FakeResponse(load("mercari_search_page1.json"))])
    source.search(SearchQuery(keyword="Switch", pages=1))
    assert len(session.calls) == 1


def test_fetch_details_signs_full_url_and_reads_seller():
    source, session = make_source([FakeResponse(load("mercari_item.json"))])
    listing = Listing("mercari", "m10000000001", "Switch", 29800, "https://jp.mercari.com/item/m10000000001")
    source.fetch_details(listing)

    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"].startswith(f"{ITEM_URL}?id=m10000000001&")
    _, payload = verify_dpop(call["headers"]["DPoP"])
    assert payload["htu"] == call["url"]
    assert "Content-Type" not in call["headers"]

    assert listing.detail_checked
    assert not listing.sold
    assert listing.description.startswith("半年ほど使用")
    assert listing.likes == 12
    assert listing.condition == 2
    assert listing.shipping_included is True
    assert (listing.seller.good, listing.seller.bad, listing.seller.total_ratings) == (210, 1, 211)
    assert listing.seller.id == "123456789"


def test_fetch_details_marks_removed_items_as_sold():
    source, _ = make_source([FakeResponse({}, status_code=404)])
    listing = Listing("mercari", "m1", "Switch", 100, "https://jp.mercari.com/item/m1")
    source.fetch_details(listing)
    assert listing.sold and listing.detail_checked


def test_fetch_details_skips_mercari_shops():
    source, session = make_source([])
    listing = Listing("mercari", "abc", "Switch", 100, "https://jp.mercari.com/shops/product/abc", is_shop=True)
    source.fetch_details(listing)
    assert session.calls == []


@pytest.mark.parametrize("status", ["trading", "sold_out", "stop", "ITEM_STATUS_SOLD_OUT"])
def test_item_detail_status_marks_unavailable(status):
    listing = Listing("mercari", "m1", "Switch", 100, "u")
    apply_item_detail(listing, {"status": status})
    assert listing.sold


def test_non_json_response_is_a_fetch_error():
    class Broken(FakeResponse):
        def json(self):
            raise ValueError("not json")

    source, _ = make_source([Broken(None)])
    from furima_finder.http import FetchError

    with pytest.raises(FetchError):
        source.search(SearchQuery(keyword="Switch"))


def test_seller_ratings_without_breakdown_are_not_read_as_zero():
    stars_only = Listing("mercari", "m1", "Switch", 100, "u")
    apply_item_detail(stars_only, {"seller": {"id": 1, "num_ratings": 120, "star_rating_score": 4.9}})
    assert stars_only.seller.total_ratings == 120
    assert stars_only.seller.good_ratio is None
    assert stars_only.seller.star_rating == 4.9

    nothing = Listing("mercari", "m2", "Switch", 100, "u")
    apply_item_detail(nothing, {"seller": {"id": 2, "name": "x"}})
    assert nothing.seller.total_ratings is None  # 分からない（0件ではない）

    new_seller = Listing("mercari", "m3", "Switch", 100, "u")
    apply_item_detail(new_seller, {"seller": {"id": 3, "ratings": {"good": 0, "normal": 0, "bad": 0}}})
    assert new_seller.seller.total_ratings == 0
