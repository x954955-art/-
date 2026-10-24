"""楽天ラクマ（fril.jp）の取得クライアント【実験的】。

ラクマには公開APIがないため、検索結果ページのHTMLを読む。
HTMLの読み方は postalservice 0.1.8（2025年7月公開）が使っているものに合わせている。
一覧ページからは商品の状態・送料・出品日時が取れないため、それらは「不明」として扱う。
サイトのデザインが変わると取れなくなる。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from ..http import HttpClient
from ..models import Listing, SearchQuery
from .base import HTML_HEADERS, matches_query

SEARCH_URL = "https://fril.jp/s"
_PRICE = re.compile(r"\d[\d,]*")


def _parse_price(value: str) -> int | None:
    match = _PRICE.search(value)
    return int(match.group().replace(",", "")) if match else None


def parse_search_html(html: str, *, sold: bool) -> list[Listing]:
    from bs4 import BeautifulSoup  # ラクマを使うときだけ必要

    soup = BeautifulSoup(html, "html.parser")
    listings: list[Listing] = []
    for item in soup.select(".item"):
        link = item.select_one("a.link_search_image") or item.select_one("a[href*='item.fril.jp']")
        href = (link.get("href") or "").strip() if link else ""
        if not href or ("fril.jp" not in href and not href.startswith("/")):
            continue
        url = urljoin("https://item.fril.jp/", href)
        item_id = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        name = item.select_one(".item-box__item-name")
        title = (link.get("title") or "").strip() or (name.get_text(" ", strip=True) if name else "")
        price_el = item.select_one(".item-box__item-price")
        price = _parse_price(price_el.get_text(" ", strip=True)) if price_el else None
        if not item_id or not title or not price:
            continue
        image = link.select_one("img") or item.select_one("img")
        thumbnail = None
        if image is not None:
            thumbnail = image.get("data-original") or image.get("data-src") or image.get("src")
        listings.append(
            Listing(
                source="rakuma",
                id=item_id,
                title=title,
                price=price,
                url=url,
                sold=sold,
                thumbnail=thumbnail if thumbnail and thumbnail.startswith("http") else None,
            )
        )
    return listings


class RakumaSource:
    name = "rakuma"
    label = "ラクマ"
    supports_sold = True
    supports_details = False

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def requests_for(self, query: SearchQuery, page: int = 1) -> list[tuple[str, dict[str, str]]]:
        params = {
            "query": query.keyword,
            "order": "desc",
            "sort": "created_at",
            "transaction": "soldout" if query.sold else "selling",
        }
        if page > 1:
            params["page"] = str(page)
        return [(SEARCH_URL, params)]

    def parse(self, body: str, query: SearchQuery) -> list[Listing]:
        return parse_search_html(body, sold=query.sold)

    def search(self, query: SearchQuery) -> list[Listing]:
        listings: list[Listing] = []
        seen: set[str] = set()
        for page in range(1, max(1, query.pages) + 1):
            ((url, params),) = self.requests_for(query, page)
            response = self.http.request("GET", url, params=params, headers=HTML_HEADERS)
            fresh = [item for item in self.parse(response.text, query) if item.key not in seen]
            if not fresh:
                break
            seen.update(item.key for item in fresh)
            listings.extend(item for item in fresh if matches_query(item, query))
        return listings

    def fetch_details(self, listing: Listing) -> None:
        return None
