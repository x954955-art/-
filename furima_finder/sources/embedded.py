"""ページに埋め込まれた JSON から出品を探す共通処理。

Next.js で作られたサイト（__NEXT_DATA__）や、検索エンジン向けの JSON-LD は、
画面に出ている商品データを JSON のまま持っていることが多い。サイトごとの細かい形に
依存しないよう、「名前と価格を持つ辞書が並んだ配列」を探して読む。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

from .. import text
from ..http import FetchError, HttpClient
from ..models import CONDITION_LABELS, Listing, SearchQuery
from .base import HTML_HEADERS, matches_query

TITLE_KEYS = ("title", "name", "itemName", "productName", "itemTitle", "displayName")
PRICE_KEYS = (
    "price", "currentPrice", "salePrice", "sellingPrice", "itemPrice", "minPrice", "lowestPrice",
    "lowPrice", "displayPrice", "minimumPrice",
)
ID_KEYS = ("id", "itemId", "productId", "item_id", "product_id", "code")
IMAGE_KEYS = (
    "thumbnailImageUrl", "thumbnailUrl", "thumbnail", "imageUrl", "image", "img", "imageUrls",
    "images", "thumbnails", "mainImageUrl",
)
URL_KEYS = ("url", "itemUrl", "productUrl", "link", "href", "path", "@id")
TIME_KEYS = ("openTime", "createdAt", "created", "listedAt", "openDate", "startTime", "updatedAt")
STATUS_KEYS = ("status", "itemStatus", "sellStatus", "saleStatus", "availability")
CONDITION_KEYS = ("condition", "itemCondition", "conditionName", "itemConditionName", "conditionText")
SOLD_WORDS = ("sold", "closed", "trading", "売り切れ", "outofstock", "soldout")
_CONDITION_BY_LABEL = {text.normalize(label): number for number, label in CONDITION_LABELS.items()}
_DIGITS = re.compile(r"\d[\d,]*")
_JST = timezone(timedelta(hours=9))


def extract_embedded_json(html: str) -> list[Any]:
    """__NEXT_DATA__ や JSON-LD など、<script> に入っている JSON をすべて取り出す。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    blobs: list[Any] = []
    for script in soup.find_all("script"):
        kind = (script.get("type") or "").lower()
        if script.get("id") != "__NEXT_DATA__" and kind not in ("application/json", "application/ld+json"):
            continue
        content = script.string or script.get_text() or ""
        try:
            blobs.append(json.loads(content))
        except ValueError:
            continue
    return blobs


def parse_json_text(body: str) -> list[Any]:
    """応答そのものが JSON のとき（API）はそれを、HTML のときは埋め込み JSON を返す。"""
    stripped = body.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return [json.loads(stripped)]
        except ValueError:
            pass
    return extract_embedded_json(body)


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = _DIGITS.search(value)
        return int(match.group().replace(",", "")) if match else None
    if isinstance(value, dict):
        return _to_int(_first(value, ("amount", "value", "price", "lowPrice")))
    return None


def _unwrap(item: dict[str, Any]) -> dict[str, Any]:
    """JSON-LD の ListItem（{"item": {...}}）なら中身を返す。"""
    inner = item.get("item")
    return inner if isinstance(inner, dict) else item


def item_price(item: dict[str, Any]) -> int | None:
    price = _to_int(_first(item, PRICE_KEYS))
    if price is None:
        offers = item.get("offers")
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict):
            price = _to_int(_first(offers, ("price", "lowPrice", "highPrice")))
    return price if price and price > 0 else None


def item_title(item: dict[str, Any]) -> str | None:
    title = _first(item, TITLE_KEYS)
    return title.strip() if isinstance(title, str) and 2 <= len(title.strip()) <= 300 else None


def looks_like_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    item = _unwrap(item)
    return item_title(item) is not None and item_price(item) is not None


@dataclass
class Candidate:
    path: str
    items: list[dict[str, Any]]


def find_item_lists(blobs: list[Any], *, min_items: int = 3) -> list[Candidate]:
    """商品らしい辞書が並んだ配列を、JSON 内の場所（パス）と一緒にすべて返す。"""
    found: list[Candidate] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, list):
            dicts = [entry for entry in node if isinstance(entry, dict)]
            items = [_unwrap(entry) for entry in dicts if looks_like_item(entry)]
            if len(items) >= min_items and len(items) >= len(dicts) * 0.6:
                found.append(Candidate(path or "$", items))
            for index, entry in enumerate(node[:200]):
                walk(entry, f"{path}[{index}]")
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))

    for number, blob in enumerate(blobs):
        walk(blob, f"#{number}")
    return found


def best_item_list(candidates: list[Candidate], query_tokens: list[str]) -> list[dict[str, Any]]:
    """検索語に合う商品がいちばん多い配列を選ぶ（おすすめ欄や広告の配列を避けるため）。"""

    def relevance(candidate: Candidate) -> tuple[int, int]:
        hits = sum(
            1 for item in candidate.items if text.match_ratio(query_tokens, item_title(item) or "") >= 0.5
        )
        return hits, len(candidate.items)

    return max(candidates, key=relevance).items if candidates else []


def _image(item: dict[str, Any]) -> str | None:
    value = _first(item, IMAGE_KEYS)
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        value = _first(value, ("url", "src", "imageUrl", "thumbnailUrl"))
    return value if isinstance(value, str) and value.startswith("http") else None


def _time(item: dict[str, Any]) -> datetime | None:
    value = _first(item, TIME_KEYS)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        seconds = int(value)
        if seconds > 10**11:  # ミリ秒
            seconds //= 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds > 0 else None
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return moment if moment.tzinfo else moment.replace(tzinfo=_JST)
    return None


def _condition(item: dict[str, Any]) -> int | None:
    value = _first(item, CONDITION_KEYS)
    if isinstance(value, dict):
        value = _first(value, ("name", "label", "text"))
    if isinstance(value, str):
        return _CONDITION_BY_LABEL.get(text.normalize(value.strip()))
    return None


def _sold(item: dict[str, Any]) -> bool:
    value = _first(item, STATUS_KEYS)
    if isinstance(value, str):
        normalized = text.normalize(value).replace("_", "").replace(" ", "")
        return any(word in normalized for word in SOLD_WORDS)
    return bool(item.get("isSold") or item.get("soldOut") or item.get("isSoldOut"))


def to_listing(
    item: dict[str, Any],
    *,
    source: str,
    base_url: str,
    url_for_id: Callable[[str], str] | None = None,
    fallback_url: Callable[[str], str] | None = None,
    shipping_included: bool | None = None,
    caution: str | None = None,
) -> Listing | None:
    item = _unwrap(item)
    title, price = item_title(item), item_price(item)
    if title is None or price is None:
        return None
    raw_id = _first(item, ID_KEYS)
    item_id = str(raw_id) if raw_id is not None else None
    raw_url = _first(item, URL_KEYS)
    if isinstance(raw_url, str) and (raw_url.startswith("http") or raw_url.startswith("/")):
        url = urljoin(base_url, raw_url)
    elif item_id and url_for_id is not None:
        url = url_for_id(item_id)
    elif fallback_url is not None:
        url = fallback_url(title)
    else:
        return None
    return Listing(
        source=source,
        id=item_id or url,
        title=title,
        price=price,
        url=url,
        sold=_sold(item),
        condition=_condition(item),
        shipping_included=shipping_included,
        created_at=_time(item),
        thumbnail=_image(item),
        caution=caution,
    )


# ---------------------------------------------------------------- 診断用

_SAMPLE_KEYS = TITLE_KEYS + PRICE_KEYS + ID_KEYS + STATUS_KEYS + CONDITION_KEYS + TIME_KEYS + URL_KEYS


def describe_candidates(candidates: list[Candidate], limit: int = 5) -> Iterator[str]:
    """見つかった配列の場所と、最初の要素の中身（キーと主な値）を短く書き出す。"""
    for candidate in sorted(candidates, key=lambda c: -len(c.items))[:limit]:
        first = candidate.items[0]
        yield f"配列 {candidate.path}（{len(candidate.items)}件）"
        yield f"  キー: {', '.join(list(first)[:25])}"
        for key in _SAMPLE_KEYS:
            if key in first:
                value = json.dumps(first[key], ensure_ascii=False)
                yield f"  {key} = {value[:80]}"


class EmbeddedJsonSource:
    """検索ページ（または API）の JSON から出品を読むサイトの共通部分【実験的】。

    販売中の出品を集めるためだけに使う（相場はメルカリ・ラクマの売り切れから出す）。
    """

    name = ""
    label = ""
    site = ""
    supports_sold = False
    supports_details = False
    shipping_included: bool | None = None
    caution: str | None = None

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def requests_for(self, query: SearchQuery) -> list[tuple[str, dict[str, str]]]:
        """試す URL と検索パラメータ（上から順に、商品が読めるまで）。"""
        raise NotImplementedError

    def url_for_id(self, item_id: str) -> str | None:
        return None

    def fallback_url(self, title: str) -> str | None:
        return None

    def parse(self, body: str, query: SearchQuery) -> list[Listing]:
        items = best_item_list(find_item_lists(parse_json_text(body)), text.tokens(query.keyword))
        listings = []
        for item in items:
            listing = to_listing(
                item,
                source=self.name,
                base_url=self.site,
                url_for_id=self.url_for_id,
                fallback_url=self.fallback_url,
                shipping_included=self.shipping_included,
                caution=self.caution,
            )
            if listing is not None and not listing.sold:
                listings.append(listing)
        return listings

    def search(self, query: SearchQuery) -> list[Listing]:
        if query.sold:
            return []
        last_error: FetchError | None = None
        for url, params in self.requests_for(query):
            try:
                response = self.http.request("GET", url, params=params, headers=HTML_HEADERS, allow_status=(404,))
            except FetchError as exc:
                last_error = exc
                continue
            if response.status_code == 404:
                continue
            parsed = self.parse(response.text, query)
            if parsed:
                unique = {listing.key: listing for listing in parsed}
                return [listing for listing in unique.values() if matches_query(listing, query)]
        if last_error is not None:
            raise last_error
        return []

    def fetch_details(self, listing: Listing) -> None:
        return None
