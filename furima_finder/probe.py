"""各サイトから実際に取得できるかを調べる診断（--probe）。

このツールを作った環境からは各サイトに接続できなかったため、ページの形を確かめる
手段として使う。結果（とくに「配列 …」の行）を見れば、読み取り方を直せる。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TextIO

from .http import FetchError, HttpClient
from .models import Listing, SearchQuery
from .scoring import yen
from .sources import MercariSource, RakumaSource, SnkrdunkSource, YahooFleaSource
from .sources.base import HTML_HEADERS
from .sources.embedded import describe_candidates, find_item_lists, parse_json_text

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.IGNORECASE | re.DOTALL)
ANY_STATUS = tuple(range(400, 600))


def _samples(out: TextIO, listings: list[Listing], indent: str) -> None:
    for listing in listings[:3]:
        out.write(f"{indent}・{listing.title[:40]} … {yen(listing.price)}  {listing.url}\n")


def _page_summary(body: str) -> list[str]:
    title = _TITLE.search(body)
    visible = " ".join(_TAGS.sub(" ", body).split())
    lines = []
    if title:
        lines.append(f"ページのタイトル: {' '.join(title.group(1).split())[:60]}")
    lines.append(f"本文の冒頭: {visible[:120]}")
    return lines


def run_probe(keyword: str, http: HttpClient, out: TextIO, save_dir: Path) -> None:
    query = SearchQuery(keyword=keyword, pages=1)
    save_dir.mkdir(parents=True, exist_ok=True)
    out.write(f"■ 診断：「{keyword}」で各サイトを1回ずつ検索します（取得したページは {save_dir} に保存）\n\n")

    out.write("■ メルカリ\n")
    try:
        listings = MercariSource(http).search(query)
    except FetchError as exc:
        out.write(f"  失敗: {exc}\n\n")
    else:
        out.write(f"  OK: {len(listings)}件取得\n")
        _samples(out, listings, "    ")
        out.write("\n")

    for source in (RakumaSource(http), YahooFleaSource(http), SnkrdunkSource(http)):
        out.write(f"■ {source.label}\n")
        for number, (url, params) in enumerate(source.requests_for(query), 1):
            try:
                response = http.request("GET", url, params=params, headers=HTML_HEADERS, allow_status=ANY_STATUS)
            except FetchError as exc:
                out.write(f"  [{number}] {url}\n      失敗: {exc}\n")
                continue
            body = response.text
            saved = save_dir / f"{source.name}_{number}.html"
            saved.write_text(body, encoding="utf-8")
            kind = response.headers.get("Content-Type", "").split(";")[0]
            out.write(f"  [{number}] {getattr(response, 'url', url)}\n")
            out.write(f"      HTTP {response.status_code}・{len(body) // 1024}KB・{kind}（保存: {saved.name}）\n")
            listings = source.parse(body, query)
            out.write(f"      商品として読めた件数: {len(listings)}件\n")
            _samples(out, listings, "        ")
            blobs = parse_json_text(body)
            if source.name != "rakuma":
                next_data = "あり" if "__NEXT_DATA__" in body else "なし"
                out.write(f"      埋め込みJSON: {len(blobs)}個（__NEXT_DATA__ {next_data}）\n")
                for line in describe_candidates(find_item_lists(blobs)):
                    out.write(f"      {line}\n")
            if not listings:
                for line in _page_summary(body):
                    out.write(f"      {line}\n")
        out.write("\n")
    out.write("この表示をすべてコピーして送ってください。読み取れていないサイトの直し方が分かります。\n")
