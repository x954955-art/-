"""検索結果の出力（ターミナル・HTML・CSV・JSON）。"""

from __future__ import annotations

import csv
import html
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO

from .finder import SearchResult
from .market import CONDITION_GROUPS, MarketStats
from .scoring import Evaluation, yen

JST = timezone(timedelta(hours=9), "JST")


def _jst(moment: datetime | None, fmt: str = "%Y/%m/%d %H:%M") -> str:
    return moment.astimezone(JST).strftime(fmt) if moment else ""


def _pct(discount: float) -> str:
    return f"-{discount:.0%}" if discount >= 0 else f"+{-discount:.0%}"


def market_lines(market: MarketStats, *, fetch_failed: bool = False) -> list[str]:
    if market.overall is None:
        if fetch_failed:
            return ["相場: 出品を取得できなかったため算出できませんでした"]
        return [
            "相場: 算出できませんでした（売り切れの出品が見つからないか少なすぎます）。"
            "--market-price で相場を指定できます"
        ]
    overall = market.overall
    if market.basis == "manual":
        return [f"相場 {yen(overall.median)}（手動指定）"]
    basis = "売り切れ" if market.basis == "sold" else "販売中"
    period = ""
    if market.oldest and market.newest:
        oldest, newest = _jst(market.oldest, "%m/%d"), _jst(market.newest, "%m/%d")
        period = f"・{oldest}" if oldest == newest else f"・{oldest}〜{newest}"
    lines = [
        f"相場 {yen(overall.median)}（{basis} {overall.count}件の中央値{period}・"
        f"中央50%は {yen(overall.q1)}〜{yen(overall.q3)}）"
    ]
    if market.groups:
        parts = [
            f"{CONDITION_GROUPS[key][0]} {yen(stats.median)}（{stats.count}件）"
            for key, stats in market.groups.items()
        ]
        lines.append("状態別: " + " / ".join(parts))
    return lines


def summary_line(result: SearchResult) -> str:
    line = f"販売中 {result.checked}件を確認 → おすすめ {len(result.recommendations)}件"
    if result.excluded:
        parts = [f"{reason} {count}件" for reason, count in result.excluded.most_common()]
        line += f"（除外: {' / '.join(parts)}）"
    return line


def empty_message(result: SearchResult) -> str:
    if result.fetch_failed:
        return "出品を取得できませんでした（上の取得エラーを確認してください）。"
    return "条件に合うおすすめはありませんでした。--min-discount を下げる・キーワードを変えるなどを試してください。"


def item_lines(rank: int, evaluation: Evaluation, *, resale: bool) -> list[str]:
    listing = evaluation.listing
    head = f"{rank:>2}. {evaluation.score}点 {yen(listing.price)}"
    if evaluation.effective_price != listing.price:
        head += f"（送料を足すと約 {yen(evaluation.effective_price)}）"
    head += f"  相場比 {_pct(evaluation.discount)}  [{listing.source_label}]"
    lines = [head, f"    {listing.title}"]
    reasons = [reason for reason in evaluation.reasons if reason]
    if reasons:
        lines.append("    " + " / ".join(reasons))
    if resale and evaluation.profit is not None:
        lines.append(f"    転売の目安: 利益 {yen(evaluation.profit)}（相場で売れた場合・手数料と送料を差し引き）")
    for warning in evaluation.warnings:
        lines.append(f"    注意: {warning}")
    lines.append(f"    {listing.url}")
    return lines


class _Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, value: str, code: str) -> str:
        return f"\033[{code}m{value}\033[0m" if self.enabled else value


def print_result(result: SearchResult, out: TextIO, *, top: int, color: bool, resale: bool) -> None:
    style = _Style(color)
    out.write(style(f"■「{result.plan.keyword}」", "1") + f"  {_jst(result.searched_at)} 時点\n")
    for line in market_lines(result.market, fetch_failed=result.fetch_failed):
        out.write(f"  {line}\n")
    for note in result.market.notes() + result.notes:
        out.write(style(f"  ※ {note}", "33") + "\n")
    for error in result.errors:
        out.write(style(f"  取得エラー: {error}", "31") + "\n")
    out.write(f"  {summary_line(result)}\n\n")
    shown = result.recommendations[:top]
    if not shown:
        out.write(f"  {empty_message(result)}\n\n")
        return
    for rank, evaluation in enumerate(shown, 1):
        lines = item_lines(rank, evaluation, resale=resale)
        score_color = "32" if evaluation.score >= 70 else "33" if evaluation.score >= 50 else "0"
        out.write("  " + style(lines[0], f"1;{score_color}") + "\n")
        for line in lines[1:]:
            if line.lstrip().startswith("注意:"):
                line = style(line, "33")
            elif line.lstrip().startswith("http"):
                line = style(line, "36")
            out.write(f"  {line}\n")
        out.write("\n")
    if len(result.recommendations) > top:
        out.write(f"  ほか {len(result.recommendations) - top}件（--top で表示件数を増やせます）\n\n")


# ---------------------------------------------------------------- CSV / JSON

CSV_COLUMNS = [
    "キーワード", "順位", "スコア", "サイト", "商品名", "価格", "実質価格", "比較した相場", "相場からの割引率(%)",
    "状態", "送料", "出品日時", "理由", "注意", "転売利益の目安", "URL",
]


def write_csv(results: list[SearchResult], path: Path) -> None:
    # Excel で文字化けしないよう BOM 付き UTF-8
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for result in results:
            for rank, evaluation in enumerate(result.recommendations, 1):
                listing = evaluation.listing
                shipping = {True: "送料込み", False: "着払い", None: ""}[listing.shipping_included]
                writer.writerow([
                    result.plan.keyword, rank, evaluation.score, listing.source_label, listing.title,
                    listing.price, evaluation.effective_price, evaluation.reference_price,
                    round(evaluation.discount * 100, 1), listing.condition_label or "", shipping,
                    _jst(listing.created_at), " / ".join(r for r in evaluation.reasons if r),
                    " / ".join(evaluation.warnings), "" if evaluation.profit is None else evaluation.profit,
                    listing.url,
                ])


def _evaluation_json(evaluation: Evaluation) -> dict:
    listing = evaluation.listing
    return {
        "score": evaluation.score,
        "source": listing.source,
        "id": listing.id,
        "title": listing.title,
        "url": listing.url,
        "price": listing.price,
        "effective_price": evaluation.effective_price,
        "reference_price": evaluation.reference_price,
        "reference_label": evaluation.reference_label,
        "discount": round(evaluation.discount, 4),
        "condition": listing.condition,
        "condition_label": listing.condition_label,
        "shipping_included": listing.shipping_included,
        "created_at": listing.created_at.isoformat() if listing.created_at else None,
        "thumbnail": listing.thumbnail,
        "reasons": [r for r in evaluation.reasons if r],
        "warnings": evaluation.warnings,
        "profit": evaluation.profit,
    }


def _market_json(market: MarketStats) -> dict:
    return {
        "basis": market.basis,
        "overall": asdict(market.overall) if market.overall else None,
        "groups": {key: asdict(value) for key, value in market.groups.items()},
        "fetched": market.fetched,
        "excluded_irrelevant": market.excluded_irrelevant,
        "excluded_rules": market.excluded_rules,
        "excluded_outliers": market.excluded_outliers,
        "notes": market.notes(),
    }


def write_json(results: list[SearchResult], path: Path) -> None:
    payload = [
        {
            "keyword": result.plan.keyword,
            "searched_at": result.searched_at.isoformat(),
            "market": _market_json(result.market),
            "checked": result.checked,
            "excluded": dict(result.excluded),
            "errors": result.errors,
            "notes": result.notes,
            "recommendations": [_evaluation_json(e) for e in result.recommendations],
        }
        for result in results
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- HTML

_CSS = """
:root {
  --bg: #f5f6f8; --card: #ffffff; --text: #1c2230; --muted: #5d6777; --line: #e2e5ea;
  --accent: #d93a2f; --good: #1d7f48; --warn: #9a5b00; --warn-bg: #fff4e0; --chip: #eef1f5;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #13161b; --card: #1c2027; --text: #e7eaef; --muted: #9ba4b1; --line: #2b313a;
    --accent: #ff6a5e; --good: #4cc584; --warn: #f2b45a; --warn-bg: #3a2c14; --chip: #262b33;
  }
}
:root[data-theme="dark"] {
  --bg: #13161b; --card: #1c2027; --text: #e7eaef; --muted: #9ba4b1; --line: #2b313a;
  --accent: #ff6a5e; --good: #4cc584; --warn: #f2b45a; --warn-bg: #3a2c14; --chip: #262b33;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.6 system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", "Yu Gothic UI", sans-serif; }
header, section { max-width: 1180px; margin: 0 auto; padding: 0 16px; }
header { padding-top: 28px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 32px 0 6px; }
.muted { color: var(--muted); font-size: 13px; }
.market { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; margin: 10px 0 14px; }
.market p { margin: 2px 0; }
.note { color: var(--warn); }
.error { color: var(--accent); }
.toolbar { display: flex; gap: 8px; align-items: center; margin: 0 0 12px; flex-wrap: wrap; }
.toolbar select { font: inherit; padding: 4px 8px; border-radius: 8px; border: 1px solid var(--line); background: var(--card); color: var(--text); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 14px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; display: flex; flex-direction: column; }
.thumb { position: relative; aspect-ratio: 1 / 1; background: var(--chip); }
.thumb.noimg { aspect-ratio: auto; height: 44px; }
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.badge { position: absolute; left: 8px; top: 8px; background: var(--accent); color: #fff; font-weight: 700; border-radius: 8px; padding: 2px 8px; }
.score { position: absolute; right: 8px; top: 8px; background: rgba(0,0,0,.66); color: #fff; border-radius: 8px; padding: 2px 8px; font-size: 13px; }
.body { padding: 10px 12px 12px; display: flex; flex-direction: column; gap: 6px; flex: 1; }
.title { font-weight: 600; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.price { font-size: 20px; font-weight: 700; }
.price small { font-size: 12px; font-weight: 400; color: var(--muted); }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { background: var(--chip); border-radius: 999px; padding: 1px 8px; font-size: 12px; }
.reasons { font-size: 13px; color: var(--good); margin: 0; padding-left: 18px; }
.warnings { font-size: 13px; color: var(--warn); background: var(--warn-bg); border-radius: 8px; margin: 0; padding: 4px 8px 4px 22px; }
.open { margin-top: auto; text-align: center; background: var(--text); color: var(--bg); border-radius: 8px; padding: 6px; text-decoration: none; font-weight: 600; }
.empty { color: var(--muted); }
footer { max-width: 1180px; margin: 40px auto; padding: 0 16px; color: var(--muted); font-size: 12px; }
"""

_JS = """
document.querySelectorAll('select[data-grid]').forEach(function (select) {
  select.addEventListener('change', function () {
    var grid = document.getElementById(select.dataset.grid);
    var key = select.value;
    var cards = Array.prototype.slice.call(grid.children);
    cards.sort(function (a, b) {
      var x = parseFloat(a.dataset[key]), y = parseFloat(b.dataset[key]);
      return key === 'price' ? x - y : y - x;
    });
    cards.forEach(function (card) { grid.appendChild(card); });
  });
});
"""


def _card(evaluation: Evaluation, *, resale: bool) -> str:
    e = html.escape
    listing = evaluation.listing
    chips = [listing.source_label]
    if listing.condition_label:
        chips.append(listing.condition_label)
    chips.append({True: "送料込み", False: "着払い", None: "送料不明"}[listing.shipping_included])
    image = (
        f'<img src="{e(listing.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer"'
        ' onerror="this.parentNode.classList.add(\'noimg\');this.remove()">'
        if listing.thumbnail
        else ""
    )
    # 状態・送料はチップで出しているので、理由の箇条書きからは省く
    reasons = "".join(f"<li>{e(r)}</li>" for r in evaluation.reasons if r and r not in chips)
    warnings = "".join(f"<li>{e(w)}</li>" for w in evaluation.warnings)
    profit = ""
    if resale and evaluation.profit is not None:
        profit = f'<p class="muted">転売の目安: 利益 {e(yen(evaluation.profit))}</p>'
    effective = ""
    if evaluation.effective_price != listing.price:
        effective = f" <small>送料を足すと約 {e(yen(evaluation.effective_price))}</small>"
    created = listing.created_at.timestamp() if listing.created_at else 0
    return f"""
<article class="card" data-score="{evaluation.score}" data-discount="{evaluation.discount:.4f}"
  data-price="{evaluation.effective_price}" data-created="{created:.0f}">
  <div class="thumb{'' if image else ' noimg'}">{image}<span class="badge">{e(_pct(evaluation.discount))}</span><span class="score">{evaluation.score}点</span></div>
  <div class="body">
    <div class="title">{e(listing.title)}</div>
    <div class="price">{e(yen(listing.price))}{effective}</div>
    <div class="muted">{e(evaluation.reference_label)} {e(yen(evaluation.reference_price or 0))}</div>
    <div class="chips">{''.join(f'<span class="chip">{e(c)}</span>' for c in chips)}</div>
    {f'<ul class="reasons">{reasons}</ul>' if reasons else ''}
    {f'<ul class="warnings">{warnings}</ul>' if warnings else ''}
    {profit}
    <a class="open" href="{e(listing.url)}" target="_blank" rel="noopener noreferrer">商品ページを開く</a>
  </div>
</article>"""


def render_html(results: list[SearchResult], *, top: int, resale: bool, generated_at: datetime) -> str:
    e = html.escape
    sections = []
    for index, result in enumerate(results):
        info = "".join(
            f"<p>{e(line)}</p>" for line in market_lines(result.market, fetch_failed=result.fetch_failed)
        )
        info += "".join(f'<p class="note">※ {e(n)}</p>' for n in result.market.notes() + result.notes)
        info += "".join(f'<p class="error">取得エラー: {e(err)}</p>' for err in result.errors)
        info += f'<p class="muted">{e(summary_line(result))}</p>'
        cards = "".join(_card(ev, resale=resale) for ev in result.recommendations[:top])
        grid_id = f"grid-{index}"
        body = (
            f"""<div class="toolbar"><label class="muted" for="sort-{index}">並び替え</label>
<select id="sort-{index}" data-grid="{grid_id}">
<option value="score">おすすめ順</option><option value="discount">割引率が大きい順</option>
<option value="price">安い順</option><option value="created">新着順</option></select></div>
<div class="grid" id="{grid_id}">{cards}</div>"""
            if cards
            else f'<p class="empty">{e(empty_message(result))}</p>'
        )
        sections.append(
            f"""<section><h2>「{e(result.plan.keyword)}」</h2>
<div class="market">{info}</div>{body}</section>"""
        )
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>フリマおすすめ候補</title>
<style>{_CSS}</style>
</head>
<body>
<header>
<h1>フリマおすすめ候補</h1>
<p class="muted">{e(_jst(generated_at))} 時点の出品から、売り切れ価格（相場）より安いものを点数順に並べています。</p>
</header>
{''.join(sections)}
<footer>価格・在庫は取得した時点のものです。購入前に必ず商品ページで状態・説明文・出品者の評価を確認してください。</footer>
<script>{_JS}</script>
</body>
</html>
"""


def write_html(results: list[SearchResult], path: Path, *, top: int, resale: bool) -> None:
    generated_at = max((r.searched_at for r in results), default=datetime.now(timezone.utc))
    path.write_text(render_html(results, top=top, resale=resale, generated_at=generated_at), encoding="utf-8")
