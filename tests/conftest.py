from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from furima_finder.models import Listing, SearchQuery

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
_ids = itertools.count(1)


def make_listing(
    title: str = "Nintendo Switch 有機EL",
    price: int = 30000,
    *,
    sold: bool = False,
    condition: int | None = 3,
    shipping_included: bool | None = True,
    age: timedelta | None = timedelta(days=2),
    source: str = "mercari",
    **extra,
) -> Listing:
    item_id = extra.pop("id", f"m{next(_ids):011d}")
    return Listing(
        source=source,
        id=item_id,
        title=title,
        price=price,
        url=f"https://jp.mercari.com/item/{item_id}",
        sold=sold,
        condition=condition,
        shipping_included=shipping_included,
        created_at=NOW - age if age is not None else None,
        **extra,
    )


class FakeSource:
    """検索条件を記録し、用意した出品を返すだけの取得元。"""

    supports_sold = True
    supports_details = True

    def __init__(self, name="mercari", label="メルカリ", sold=(), on_sale=(), details=None, error=None):
        self.name = name
        self.label = label
        self.sold = list(sold)
        self.on_sale = list(on_sale)
        self.details = details or {}
        self.error = error
        self.queries: list[SearchQuery] = []
        self.detail_calls: list[str] = []

    def search(self, query: SearchQuery) -> list[Listing]:
        self.queries.append(query)
        if self.error:
            raise self.error
        pool = self.sold if query.sold else self.on_sale
        return [
            listing
            for listing in pool
            if (not query.price_min or listing.price >= query.price_min)
            and (not query.price_max or listing.price <= query.price_max)
        ]

    def fetch_details(self, listing: Listing) -> None:
        self.detail_calls.append(listing.id)
        listing.detail_checked = True
        update = self.details.get(listing.id)
        if update:
            update(listing)


@pytest.fixture
def now() -> datetime:
    return NOW
