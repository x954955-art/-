"""メルカリの取得クライアント。

Web版メルカリが使っている api.mercari.jp を、Web版と同じ形式のリクエストで呼ぶ。
リクエスト形式は mercapi 0.5.0（2026年9月公開）に合わせている。
メルカリ側の仕様変更で動かなくなることがある。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from ..http import FetchError, HttpClient
from ..models import Listing, SearchQuery, Seller

SEARCH_URL = "https://api.mercari.jp/v2/entities:search"
ITEM_URL = "https://api.mercari.jp/items/get"
PAGE_SIZE = 120

SORTS = {
    "new": ("SORT_CREATED_TIME", "ORDER_DESC"),
    "price_asc": ("SORT_PRICE", "ORDER_ASC"),
    "score": ("SORT_SCORE", "ORDER_DESC"),
}
SOLD_STATUSES = {"ITEM_STATUS_SOLD_OUT", "ITEM_STATUS_TRADING"}
# shippingPayerId: 1 = 着払い（購入者負担）, 2 = 送料込み（出品者負担）
SHIPPING_INCLUDED = {"1": False, "2": True}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_json(value: dict[str, Any]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


class DPoPSigner:
    """リクエストごとに DPoP（ES256 で署名した JWT）を作る。鍵は起動ごとに作り直す。"""

    def __init__(
        self,
        *,
        private_key: ec.EllipticCurvePrivateKey | None = None,
        device_uuid: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = private_key or ec.generate_private_key(ec.SECP256R1())
        numbers = self._key.public_key().public_numbers()
        self.jwk = {
            "crv": "P-256",
            "kty": "EC",
            "x": _b64url(numbers.x.to_bytes(32, "big")),
            "y": _b64url(numbers.y.to_bytes(32, "big")),
        }
        self.device_uuid = device_uuid or str(uuid.uuid4())
        self._clock = clock

    def sign(self, method: str, url: str) -> str:
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self.jwk}
        payload = {
            "iat": int(self._clock()),
            "jti": str(uuid.uuid4()),
            "htu": url,
            "htm": method.upper(),
            "uuid": self.device_uuid,
        }
        signing_input = f"{_b64url_json(header)}.{_b64url_json(payload)}"
        der = self._key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return f"{signing_input}.{_b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def build_search_body(
    query: SearchQuery, *, page_token: str = "", session_id: str = "", device_uuid: str = ""
) -> dict[str, Any]:
    sort, order = SORTS[query.sort]
    status = ["STATUS_SOLD_OUT", "STATUS_TRADING"] if query.sold else ["STATUS_ON_SALE"]
    return {
        "userId": "",
        "config": {"responseToggles": ["QUERY_SUGGESTION_WEB_1"]},
        "pageSize": PAGE_SIZE,
        "pageToken": page_token,
        "searchSessionId": session_id or uuid.uuid4().hex,
        "source": "BaseSerp",
        "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
        "thumbnailTypes": [],
        "searchCondition": {
            "keyword": query.keyword,
            "excludeKeyword": " ".join(query.exclude),
            "sort": sort,
            "order": order,
            "status": status,
            "sizeId": [],
            "categoryId": [],
            "brandId": [],
            "sellerId": [],
            "priceMin": query.price_min or 0,
            "priceMax": query.price_max or 0,
            "itemConditionId": list(query.conditions),
            "shippingPayerId": [],
            "shippingFromArea": [],
            "shippingMethod": [],
            "colorId": [],
            "hasCoupon": False,
            "attributes": [],
            "itemTypes": [],
            "skuIds": [],
            "shopIds": [],
            "excludeShippingMethodIds": [],
        },
        "serviceFrom": "suruga",
        "withItemBrand": True,
        "withItemSize": False,
        "withItemPromotions": True,
        "withItemSizes": True,
        "withShopname": False,
        "useDynamicAttribute": True,
        "withSuggestedItems": True,
        "withOfferPricePromotion": True,
        "withProductSuggest": True,
        "withParentProducts": False,
        "withProductArticles": True,
        "withSearchConditionId": False,
        "withAuction": True,
        "laplaceDeviceUuid": device_uuid or uuid.uuid4().hex,
    }


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    seconds = _int(value)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds else None


def _datetime(value: Any) -> datetime | None:
    """"2026-10-25T12:00:00Z" のような日時か、UNIX時刻の文字列を読む。"""
    if not isinstance(value, str) or not value:
        return None
    if value.isdigit():
        return _timestamp(value)
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def parse_search_item(raw: dict[str, Any]) -> Listing | None:
    item_id = raw.get("id")
    price = _int(raw.get("price"))
    if not item_id or price is None or price <= 0 or raw.get("isNoPrice"):
        return None
    is_shop = raw.get("itemType") == "ITEM_TYPE_BEYOND"
    url = f"https://jp.mercari.com/{'shops/product' if is_shop else 'item'}/{item_id}"
    condition = _int(raw.get("itemConditionId"))
    thumbnails = raw.get("thumbnails") or []
    auction = raw.get("auction")
    is_auction = isinstance(auction, dict) and bool(auction.get("id") or auction.get("bidDeadline"))
    return Listing(
        source="mercari",
        id=str(item_id),
        title=str(raw.get("name") or ""),
        price=price,
        url=url,
        sold=raw.get("status") in SOLD_STATUSES,
        condition=condition if condition in range(1, 7) else None,
        shipping_included=SHIPPING_INCLUDED.get(str(raw.get("shippingPayerId"))),
        created_at=_timestamp(raw.get("created")),
        updated_at=_timestamp(raw.get("updated")),
        thumbnail=thumbnails[0] if thumbnails and isinstance(thumbnails[0], str) else None,
        seller_id=str(raw["sellerId"]) if raw.get("sellerId") else None,
        is_shop=is_shop,
        is_auction=is_auction,
        auction_end=_datetime(auction.get("bidDeadline")) if is_auction else None,
        auction_bids=_int(auction.get("totalBid")) if is_auction else None,
    )


def apply_item_detail(listing: Listing, data: dict[str, Any]) -> None:
    """items/get の data 部分から、検索結果にはない情報を書き足す。"""
    listing.detail_checked = True
    # 販売中は "on_sale"。取引中・売り切れ・公開停止になっていたら買えない
    status = str(data.get("status") or "").lower()
    if any(word in status for word in ("trading", "sold", "stop", "cancel")):
        listing.sold = True
    if isinstance(data.get("description"), str):
        listing.description = data["description"]
    likes = _int(data.get("num_likes"))
    if likes is not None:
        listing.likes = likes
    condition = data.get("item_condition")
    if listing.condition is None and isinstance(condition, dict):
        condition_id = _int(condition.get("id"))
        listing.condition = condition_id if condition_id in range(1, 7) else None
    payer = data.get("shipping_payer")
    if listing.shipping_included is None and isinstance(payer, dict):
        listing.shipping_included = SHIPPING_INCLUDED.get(str(payer.get("id")))
    seller = data.get("seller")
    if isinstance(seller, dict):
        ratings = seller.get("ratings")
        # 内訳（良い/普通/悪い）が無い形式でも「0件」と誤解しないよう、分かる範囲だけ使う
        breakdown = isinstance(ratings, dict) and any(key in ratings for key in ("good", "normal", "bad"))
        ratings = ratings if breakdown else {}
        listing.seller = Seller(
            id=str(seller.get("id") or listing.seller_id or ""),
            name=seller.get("name"),
            good=_int(ratings.get("good")) or 0,
            normal=_int(ratings.get("normal")) or 0,
            bad=_int(ratings.get("bad")) or 0,
            num_sell_items=_int(seller.get("num_sell_items")),
            num_ratings=_int(seller.get("num_ratings")),
            star_rating=_float(seller.get("star_rating_score")),
            breakdown_known=breakdown,
        )


def _json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise FetchError("メルカリの応答がJSONではありませんでした") from exc
    if not isinstance(data, dict):
        raise FetchError("メルカリの応答の形式が想定と違います")
    return data


class MercariSource:
    name = "mercari"
    label = "メルカリ"
    supports_sold = True
    supports_details = True

    def __init__(self, http: HttpClient, signer: DPoPSigner | None = None) -> None:
        self.http = http
        self.signer = signer or DPoPSigner()
        self._device_uuid = uuid.uuid4().hex

    def _headers(self, method: str, url: str) -> dict[str, str]:
        headers = {
            "DPoP": self.signer.sign(method, url),
            "X-Platform": "web",
            "Accept": "application/json, text/plain, */*",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
        return headers

    def search(self, query: SearchQuery) -> list[Listing]:
        listings: list[Listing] = []
        seen: set[str] = set()
        page_token = ""
        session_id = uuid.uuid4().hex
        for _ in range(max(1, query.pages)):
            body = build_search_body(
                query, page_token=page_token, session_id=session_id, device_uuid=self._device_uuid
            )
            response = self.http.request(
                "POST", SEARCH_URL, json=body, headers=lambda: self._headers("POST", SEARCH_URL)
            )
            data = _json(response)
            for raw in data.get("items") or []:
                listing = parse_search_item(raw) if isinstance(raw, dict) else None
                if listing and listing.key not in seen:
                    seen.add(listing.key)
                    listings.append(listing)
            page_token = (data.get("meta") or {}).get("nextPageToken") or ""
            if not page_token:
                break
        return listings

    def fetch_details(self, listing: Listing) -> None:
        if listing.is_shop:
            return
        params = {
            "id": listing.id,
            "include_item_attributes": "true",
            "include_product_page_component": "true",
            "include_non_ui_item_attributes": "true",
            "include_donation": "true",
            "include_item_attributes_sections": "true",
            "include_auction": "true",
            "country_code": "JP",
        }
        # DPoP の htu と実際のURLを一致させるため、クエリ込みのURLを自分で組み立てる
        url = f"{ITEM_URL}?{urlencode(params)}"
        response = self.http.request(
            "GET", url, headers=lambda: self._headers("GET", url), allow_status=(404,)
        )
        if response.status_code == 404:
            listing.detail_checked = True
            listing.sold = True  # 削除・非公開
            return
        data = _json(response).get("data")
        if isinstance(data, dict):
            apply_item_detail(listing, data)
