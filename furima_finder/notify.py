"""監視モードで見つかった新着候補を Discord / Slack の Webhook に送る。"""

from __future__ import annotations

from urllib.parse import urlsplit

import requests

from .scoring import Evaluation, yen

DISCORD_LIMIT = 1900


def format_message(keyword: str, evaluations: list[Evaluation], *, title: str = "【フリマおすすめ】") -> str:
    lines = [f"{title}{keyword}"]
    for evaluation in evaluations:
        listing = evaluation.listing
        buy = "現在の入札" if listing.is_auction else "仕入れ"
        if evaluation.profit is not None:
            summary = f"{buy} {yen(listing.price)} → 利益 {yen(evaluation.profit)}（利益率 {evaluation.roi:.0%}）"
        else:
            summary = f"{yen(listing.price)}（相場比 -{max(evaluation.discount, 0):.0%}）"
        details = [listing.condition_label or "", "送料込み" if listing.shipping_included else ""]
        detail = " / ".join(d for d in details if d)
        lines.append(
            f"・{evaluation.score}点 {summary}"
            f"{' ' + detail if detail else ''} [{listing.source_label}]\n  {listing.title}\n  {listing.url}"
        )
    return "\n".join(lines)


def _chunks(message: str, limit: int) -> list[str]:
    chunks, current = [], ""
    for line in message.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_webhook(url: str, message: str, *, post=requests.post) -> None:
    host = urlsplit(url).netloc
    if host.endswith("slack.com"):
        payloads = [{"text": message}]
    elif "discord" in host:
        payloads = [{"content": chunk} for chunk in _chunks(message, DISCORD_LIMIT)]
    else:
        payloads = [{"content": message, "text": message}]
    for payload in payloads:
        response = post(url, json=payload, timeout=15)
        response.raise_for_status()
