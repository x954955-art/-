"""サイトをまたいで共通に使うデータ構造。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# メルカリ・ラクマ共通の「商品の状態」。数字が小さいほど状態が良い。
CONDITION_LABELS = {
    1: "新品、未使用",
    2: "未使用に近い",
    3: "目立った傷や汚れなし",
    4: "やや傷や汚れあり",
    5: "傷や汚れあり",
    6: "全体的に状態が悪い",
}

SOURCE_LABELS = {
    "mercari": "メルカリ",
    "rakuma": "ラクマ",
}


@dataclass
class Seller:
    id: str
    name: str | None = None
    good: int = 0
    normal: int = 0
    bad: int = 0
    num_sell_items: int | None = None
    num_ratings: int | None = None  # 評価の総数（内訳が分からないときにも使う）
    star_rating: float | None = None  # 5段階の平均
    breakdown_known: bool = True  # 良い/普通/悪い の内訳が分かっているか

    @property
    def total_ratings(self) -> int | None:
        """評価の件数。分からなければ None。"""
        counts = [self.num_ratings] if self.num_ratings is not None else []
        if self.breakdown_known:
            counts.append(self.good + self.normal + self.bad)
        return max(counts) if counts else None

    @property
    def good_ratio(self) -> float | None:
        total = self.good + self.normal + self.bad
        return self.good / total if self.breakdown_known and total else None


@dataclass
class Listing:
    """1件の出品。販売中・売り切れのどちらにも使う。"""

    source: str
    id: str
    title: str
    price: int
    url: str
    sold: bool = False
    condition: int | None = None
    shipping_included: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    thumbnail: str | None = None
    seller_id: str | None = None
    is_shop: bool = False
    is_auction: bool = False
    auction_end: datetime | None = None
    auction_bids: int | None = None
    # 以下は商品詳細を取得したときだけ埋まる
    description: str | None = None
    likes: int | None = None
    seller: Seller | None = None
    detail_checked: bool = False

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def condition_label(self) -> str | None:
        return CONDITION_LABELS.get(self.condition) if self.condition else None

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)


@dataclass
class SearchQuery:
    """各サイトへの検索条件。"""

    keyword: str
    exclude: list[str] = field(default_factory=list)
    price_min: int | None = None
    price_max: int | None = None
    conditions: list[int] = field(default_factory=list)
    sold: bool = False
    pages: int = 1
    sort: str = "new"  # new / price_asc / score
