"""取得元（サイト）ごとの実装が満たすインターフェース。"""

from __future__ import annotations

from typing import Protocol

from ..models import Listing, SearchQuery


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
