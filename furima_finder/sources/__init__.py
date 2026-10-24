"""取得元（サイト）の一覧。"""

from __future__ import annotations

from ..http import HttpClient
from .base import Source
from .mercari import MercariSource
from .rakuma import RakumaSource

AVAILABLE = {
    "mercari": MercariSource,
    "rakuma": RakumaSource,
}


def create_sources(names: list[str], http: HttpClient) -> dict[str, Source]:
    unknown = [name for name in names if name not in AVAILABLE]
    if unknown:
        raise ValueError(f"未対応のサイトです: {', '.join(unknown)}（対応: {', '.join(AVAILABLE)}）")
    return {name: AVAILABLE[name](http) for name in dict.fromkeys(names)}


__all__ = ["AVAILABLE", "MercariSource", "RakumaSource", "Source", "create_sources"]
