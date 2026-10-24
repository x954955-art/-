"""取得元（サイト）ごとの実装が満たすインターフェース。"""

from __future__ import annotations

from typing import Protocol

from .. import text
from ..models import Listing, SearchQuery

# HTML のページを取りに行くときのヘッダー（ブラウザと同じ受け入れ形式）
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


class Source(Protocol):
    name: str
    label: str
    supports_sold: bool
    supports_details: bool

    def search(self, query: SearchQuery) -> list[Listing]:
        """検索結果を返す。取得に失敗したら FetchError を送出する。"""
        ...

    def fetch_details(self, listing: Listing) -> None:
        """出品者評価や説明文などを listing に書き足す。"""
        ...


def matches_query(listing: Listing, query: SearchQuery) -> bool:
    """サーバー側で絞り込めない条件（価格帯・除外キーワード）を手元で適用する。"""
    if query.price_min and listing.price < query.price_min:
        return False
    if query.price_max and listing.price > query.price_max:
        return False
    title = text.normalize(listing.title)
    return not any(text.normalize(word) in title for word in query.exclude)
