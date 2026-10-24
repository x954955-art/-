"""Yahoo!フリマ（旧PayPayフリマ）の取得クライアント【実験的】。

公開APIはないため、検索結果ページに埋め込まれた JSON（Next.js の __NEXT_DATA__）から
出品を読む。ページの細かい形は確認できていないので、商品らしいデータの並びを自動で
探している（embedded.py）。うまく取れないときは --probe で状況を確認する。
Yahoo!フリマの出品は送料込み。
"""

from __future__ import annotations

from urllib.parse import quote

from ..models import SearchQuery
from .embedded import EmbeddedJsonSource


class YahooFleaSource(EmbeddedJsonSource):
    name = "yahoo"
    label = "Yahoo!フリマ"
    site = "https://paypayfleamarket.yahoo.co.jp"
    shipping_included = True

    def requests_for(self, query: SearchQuery) -> list[tuple[str, dict[str, str]]]:
        page_params = {"open": "1", "sort": "openTime", "order": "desc"}
        api_params = {
            "query": query.keyword,
            "results": "100",
            "imageShape": "square",
            "sort": "openTime",
            "order": "desc",
            "itemStatus": "open",
            "offset": "0",
        }
        for params in (page_params, api_params):
            if query.price_min:
                params["minPrice"] = str(query.price_min)
            if query.price_max:
                params["maxPrice"] = str(query.price_max)
        return [
            (f"{self.site}/search/{quote(query.keyword, safe='')}", page_params),
            (f"{self.site}/api/v1/search", api_params),
        ]

    def url_for_id(self, item_id: str) -> str:
        return f"{self.site}/item/{item_id}"
