"""TDnet の過去の開示一覧を、やのしんWEB-API（tdnet ライブラリと同じ取得元）から日ごとに取得する。

使い方:
    # まず試し取り: 各年の数日だけ取得して、どこまで遡れるか・1日の上限で切れていないかを確認
    python scripts/fetch_tdnet_history.py --probe --start 2016-01-01

    # 本取得（既定は終了日の5年前から）。途中で止めても、もう一度実行すれば続きから再開
    python scripts/fetch_tdnet_history.py --start 2021-10-01

- 1日ずつ取得して data/tdnet_raw/YYYY/YYYYMMDD.json にキャッシュ
- 無料の個人運営サービスなので、既定で1秒に1リクエストまでに抑える
- 1日の件数が上限（--limit）に達した日は「取りこぼしの可能性あり」として報告
- 最後に data/tdnet/yanoshin_events.csv（code, disclosed_at, title, company_name）にまとめる
- 表示するのは件数などの集計だけ
tdnet ライブラリ（Python 3.12 以上）が入っていればそれを使い、なければ同じ API を直接呼ぶ。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdnet_edge.sources import events_from_tdnet  # noqa: E402

YANOSHIN = "https://webapi.yanoshin.jp/webapi/tdnet/list"
RETRY_WAITS = (5, 20, 60)


def _fetch_direct(ds: str, limit: int) -> list[dict]:
    url = f"{YANOSHIN}/{ds}.json2?" + urllib.parse.urlencode({"limit": limit})
    req = urllib.request.Request(url, headers={"User-Agent": "tdnet-edge/0.1 (personal research)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("items", [])


def fetch_day(d: date, limit: int) -> list[dict]:
    ds = d.strftime("%Y%m%d")
    try:
        from tdnet.api import list_by_date
    except ImportError:
        return _fetch_direct(ds, limit)
    return list_by_date(ds, limit=limit)


def cache_path(raw_dir: Path, d: date) -> Path:
    return raw_dir / f"{d.year}" / f"{d:%Y%m%d}.json"


def load_cache(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def get_day(d: date, raw_dir: Path, limit: int, interval: float, refresh: bool) -> tuple[dict | None, bool]:
    """(キャッシュ内容, 今回APIを呼んだか)。失敗したら (None, True)。"""
    p = cache_path(raw_dir, d)
    c = load_cache(p)
    if c and not refresh and (c["count"] < c["limit"] or c["limit"] >= limit):
        return c, False
    for k, wait in enumerate((0, *RETRY_WAITS)):
        time.sleep(max(wait, interval))
        try:
            items = fetch_day(d, limit)
        except Exception as e:  # noqa: BLE001 — 一時的な失敗は待って再試行
            if k == len(RETRY_WAITS):
                print(f"  {d}: 取得失敗（{type(e).__name__}）。再実行すると続きから再試行します", file=sys.stderr)
                return None, True
            continue
        c = {"date": f"{d:%Y-%m-%d}", "limit": limit, "count": len(items), "fetched_at": datetime.now().isoformat(timespec="seconds"), "items": items}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        return c, True
    return None, True


def weekdays(start: date, end: date) -> list[date]:
    return [start + timedelta(i) for i in range((end - start).days + 1) if (start + timedelta(i)).weekday() < 5]


def probe_days(start: date, end: date) -> list[date]:
    """各年の決算集中日付近（5月中旬）と閑散期（9月中旬）を数日ずつ。"""
    out = []
    for y in range(start.year, end.year + 1):
        for m, d0 in ((5, 13), (9, 15)):
            cands = [date(y, m, d0) + timedelta(i) for i in range(5)]
            out += [d for d in cands if d.weekday() < 5 and start <= d <= end][:2]
    return out


def report(caches: list[dict], failed: list[date], limit: int) -> str:
    by_year = defaultdict(lambda: {"days": 0, "active": 0, "items": 0, "trunc": 0, "max": 0})
    first = last = None
    for c in caches:
        y = c["date"][:4]
        s = by_year[y]
        s["days"] += 1
        s["items"] += c["count"]
        s["max"] = max(s["max"], c["count"])
        if c["count"] > 0:
            s["active"] += 1
            first = min(first or c["date"], c["date"])
            last = max(last or c["date"], c["date"])
        if c["count"] >= c["limit"]:
            s["trunc"] += 1
    lines = [
        f"- 開示が取れた期間: {first or 'なし'} 〜 {last or 'なし'}",
        f"- 取得失敗: {len(failed)} 日" + ("（もう一度実行すると再試行します）" if failed else ""),
        "",
        "| 年 | 取得日数(平日) | 開示あり | 開示件数 | 1日平均 | 1日最大 | 上限到達(取りこぼし疑い) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for y in sorted(by_year):
        s = by_year[y]
        avg = s["items"] / s["active"] if s["active"] else 0
        lines.append(f"| {y} | {s['days']:,} | {s['active']:,} | {s['items']:,} | {avg:,.0f} | {s['max']:,} | {s['trunc']:,} |")
    if any(s["trunc"] for s in by_year.values()):
        lines.append(f"\n※ 上限 {limit:,} 件に達した日があります。--limit を増やして再実行してください（APIが上限を受け付けない場合は別の対策が必要）。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="開始日 YYYY-MM-DD（既定: 終了日の5年前）")
    ap.add_argument("--end", default=None, help="終了日 YYYY-MM-DD（既定: 昨日）")
    ap.add_argument("--limit", type=int, default=10000, help="1日あたりの最大取得件数")
    ap.add_argument("--interval", type=float, default=1.0, help="リクエスト間隔（秒）")
    ap.add_argument("--probe", action="store_true", help="各年の数日だけ取得して取得範囲と上限を確認")
    ap.add_argument("--refresh", action="store_true", help="キャッシュがあっても取り直す")
    ap.add_argument("--raw-dir", default=str(ROOT / "data" / "tdnet_raw"))
    ap.add_argument("--out-csv", default=str(ROOT / "data" / "tdnet" / "yanoshin_events.csv"))
    args = ap.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else date(end.year - 5, end.month, 1)
    raw_dir = Path(args.raw_dir)
    days = probe_days(start, end) if args.probe else weekdays(start, end)
    print(f"{'試し取り' if args.probe else '取得'}: {start} 〜 {end}（{len(days):,} 日、約{len(days) * args.interval / 60:,.0f}分〜）", file=sys.stderr)

    caches, failed, calls = [], [], 0
    for n, d in enumerate(days, 1):
        c, called = get_day(d, raw_dir, args.limit, args.interval, args.refresh)
        calls += called
        if c is None:
            failed.append(d)
        else:
            caches.append(c)
        if n % 50 == 0:
            print(f"  {n:,}/{len(days):,} 日（API呼び出し {calls:,} 回）", file=sys.stderr)

    print(f"# TDnet 開示一覧の取得結果（{'試し取り' if args.probe else '本取得'}）")
    print(report(caches, failed, args.limit))

    if not args.probe:
        frames = [events_from_tdnet(c["items"]) for c in caches if c["count"]]
        ev = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["code", "disclosed_at", "title", "company_name"])
        ev = ev.drop_duplicates(["code", "disclosed_at", "title"]).sort_values("disclosed_at")
        out = Path(args.out_csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        ev.to_csv(out, index=False)
        print(f"\n保存: {out}（{len(ev):,} 件、銘柄 {ev['code'].nunique():,}）")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
