"""スニダン（SNKRDUNK）の取得クライアント【実験的】。

スニダンは「商品（カード）ごとに、出品されている中の最安値で買える」形式のマーケット。
検索結果ページに埋め込まれた JSON から、商品名と価格を読む。ページの細かい形は
確認できていないので、うまく取れないときは --probe で状況を確認する。
表示価格は状態（PSA10・ランクなど）を問わない最安値のことがあり、購入時の手数料・送料も
別にかかるため、必ず商品ページで確認する。
"""

from __future__ import annotations

from urllib.parse import quote

from ..models import SearchQuery
from .embedded import EmbeddedJsonSource


class SnkrdunkSource(EmbeddedJsonSource):
    name = "snkrdunk"
    label = "スニダン"
    site = "https://snkrdunk.com"
    caution = "スニダンの価格は最安値の目安：状態（PSA・ランク）と購入時の手数料・送料を商品ページで確認"

    def requests_for(self, query: SearchQuery) -> list[tuple[str, dict[str, str]]]:
        return [
            (f"{self.site}/search", {"keywords": query.keyword}),
            (f"{self.site}/search/result", {"keyword": query.keyword}),
        ]

    def fallback_url(self, title: str) -> str:
        # 商品ページのURLが分からないときは、その名前で検索したページを開く
        return f"{self.site}/search?keywords={quote(title)}"
