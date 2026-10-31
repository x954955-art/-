"""株価SQLite DBの構造を調べる（メタ情報のみ表示。個別の株価・銘柄コードは表示しない）。

使い方:
    python scripts/inspect_db.py /path/to/prices.db

表示するもの:
    - テーブル/ビュー、列名と型、行数、インデックス
    - 銘柄コード列・日付列の推定、日付形式、期間、銘柄数
    - 上場廃止銘柄が含まれているかの判定材料（途中でデータが終わる銘柄の数を年別に集計）
    - 分割調整・TOPIX・発行済株式数/時価総額・出来高に相当しそうな列の有無
DBは読み取り専用で開く。
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CODE_NAMES = re.compile(r"^(code|localcode|local_code|ticker|symbol|sc|stock_code|meigara|銘柄コード|コード|証券コード)$", re.I)
DATE_NAMES = re.compile(r"^(date|trade_date|tradedate|datetime|day|dt|日付|年月日|取引日)$", re.I)
HINTS = {
    "分割調整": re.compile(r"adj|factor|調整", re.I),
    "発行済株式数/時価総額": re.compile(r"share|issued|outstanding|mcap|market_?cap|marketcap|時価総額|発行済", re.I),
    "出来高": re.compile(r"^(volume|vol|vo|出来高)$", re.I),
    "始値": re.compile(r"^(open|o|始値|adj_?open|adjustmentopen)$", re.I),
    "終値": re.compile(r"^(close|c|終値|adj_?close|adjustmentclose)$", re.I),
    "指数/TOPIX": re.compile(r"topix|index|指数", re.I),
    "市場/業種": re.compile(r"market|sector|segment|市場|業種|section", re.I),
}


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def parse_date(v) -> date | None:
    """DBの日付値を date に変換（形式を推定）。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        iv = int(v)
        if 19000101 <= iv <= 21001231:
            try:
                return datetime.strptime(str(iv), "%Y%m%d").date()
            except ValueError:
                return None
        if 10**9 <= iv < 10**10:  # unix秒
            return datetime.fromtimestamp(iv, tz=timezone.utc).date()
        if 10**12 <= iv < 10**13:  # unixミリ秒
            return datetime.fromtimestamp(iv / 1000, tz=timezone.utc).date()
        return None
    s = str(v).strip()
    for fmt, n in (("%Y-%m-%d", 10), ("%Y/%m/%d", 10), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(s[:n], fmt).date()
        except ValueError:
            continue
    return None


def date_format(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, int):
        if 19000101 <= v <= 21001231:
            return "INTEGER(YYYYMMDD)"
        if 10**9 <= v < 10**10:
            return "INTEGER(unix秒)"
        if 10**12 <= v < 10**13:
            return "INTEGER(unixミリ秒)"
        return "INTEGER(不明)"
    if isinstance(v, float):
        return "REAL(不明)"
    s = str(v)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return "TEXT(YYYY-MM-DD)"
    if re.match(r"^\d{4}-\d{2}-\d{2}[ T]", s):
        return "TEXT(YYYY-MM-DD hh:mm:ss)"
    if re.match(r"^\d{4}/\d{2}/\d{2}", s):
        return "TEXT(YYYY/MM/DD)"
    if re.match(r"^\d{8}$", s):
        return "TEXT(YYYYMMDD)"
    return "TEXT(不明)"


def code_shape(v) -> str:
    s = str(v)
    if re.fullmatch(r"\d{4}", s):
        return "数字4桁"
    if re.fullmatch(r"\d{5}", s):
        return "数字5桁"
    if re.fullmatch(r"[0-9A-Z]{4}", s):
        return "英数4桁"
    if re.fullmatch(r"[0-9A-Z]{5}", s):
        return "英数5桁"
    if re.fullmatch(r"\d{4}\.T", s):
        return "数字4桁+.T"
    return "その他"


def inspect_code_date_table(con, table, code_col, date_col, out):
    cur = con.cursor()
    sample = cur.execute(f"SELECT {q(date_col)} FROM {q(table)} WHERE {q(date_col)} IS NOT NULL LIMIT 1").fetchone()
    fmt = date_format(sample[0]) if sample else "NULL"
    out.append(f"  - 日付列 `{date_col}` の形式: {fmt}")

    rows = cur.execute(
        f"SELECT {q(code_col)}, MIN({q(date_col)}), MAX({q(date_col)}), COUNT(*) FROM {q(table)} GROUP BY {q(code_col)}"
    ).fetchall()
    if not rows:
        out.append("  - データなし")
        return
    shapes = Counter(code_shape(r[0]) for r in rows)
    out.append(f"  - 銘柄コード列 `{code_col}`: 銘柄数 {len(rows):,}（形式: " + ", ".join(f"{k} {v:,}" for k, v in shapes.most_common()) + "）")
    firsts = [parse_date(r[1]) for r in rows]
    lasts = [parse_date(r[2]) for r in rows]
    valid_last = [d for d in lasts if d]
    valid_first = [d for d in firsts if d]
    if not valid_last:
        out.append("  - 日付を解釈できませんでした")
        return
    gmin, gmax = min(valid_first), max(valid_last)
    out.append(f"  - 期間: {gmin} 〜 {gmax}")
    n_rows = sum(r[3] for r in rows)
    n_dates = cur.execute(f"SELECT COUNT(DISTINCT {q(date_col)}) FROM {q(table)}").fetchone()[0]
    out.append(f"  - 行数 {n_rows:,} / 日付数 {n_dates:,}（1日あたり平均 {n_rows / max(n_dates, 1):,.0f} 銘柄）")

    cutoff = gmax - timedelta(days=30)
    ended = [d for d in valid_last if d < cutoff]
    out.append(
        f"  - 最終日より30日以上前にデータが終わる銘柄: {len(ended):,} / {len(valid_last):,}"
        "（多ければ上場廃止銘柄を含む。ほぼ0なら生存銘柄のみ＝生存バイアスあり）"
    )
    if ended:
        by_year = Counter(d.year for d in ended)
        out.append("    - データ終了年の内訳: " + ", ".join(f"{y}: {by_year[y]:,}" for y in sorted(by_year)))
    started = [d for d in valid_first if d > gmin + timedelta(days=30)]
    out.append(f"  - 期間の途中からデータが始まる銘柄（新規上場など）: {len(started):,}")

    # 年ごとのアクティブ銘柄数（上場廃止を含むなら過去ほど「その後消える銘柄」が混ざる）
    per_year = defaultdict(int)
    for f, l in zip(firsts, lasts):
        if f and l:
            for y in range(f.year, l.year + 1):
                per_year[y] += 1
    ys = sorted(per_year)
    if ys:
        pick = [y for y in ys if (y - ys[0]) % 5 == 0 or y == ys[-1]]
        out.append("  - 各年にデータがある銘柄数: " + ", ".join(f"{y}: {per_year[y]:,}" for y in pick))

    # TOPIX / ETF(1306) の有無（値は表示しない）
    probes = {"1306(TOPIX連動ETF)": ("1306", "13060", "1306.T"), "0000(J-QuantsのTOPIXコード)": ("0000",)}
    for label, cands in probes.items():
        ph = ",".join("?" * len(cands))
        n = cur.execute(f"SELECT COUNT(*) FROM {q(table)} WHERE CAST({q(code_col)} AS TEXT) IN ({ph})", cands).fetchone()[0]
        out.append(f"  - {label} の行: {'あり' if n else 'なし'}{f'（{n:,}行）' if n else ''}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="SQLiteファイルのパス")
    ap.add_argument("--max-tables", type=int, default=30, help="詳しく調べるテーブル数の上限")
    args = ap.parse_args()

    path = Path(args.db).expanduser()
    if not path.exists():
        print(f"ファイルが見つかりません: {path}")
        return 1
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = con.cursor()
    out: list[str] = []
    out.append(f"# DB構造: {path.name}（{path.stat().st_size / 1e9:.2f} GB, SQLite {sqlite3.sqlite_version}）")

    objs = cur.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    out.append(f"\nテーブル/ビュー数: {len(objs)}")

    # 列構成が同じテーブルをまとめる（銘柄ごとにテーブルが分かれている形式の検出）
    sig = defaultdict(list)
    cols_of = {}
    for name, typ in objs:
        cols = cur.execute(f"PRAGMA table_info({q(name)})").fetchall()
        cols_of[name] = [(c[1], c[2]) for c in cols]
        sig[tuple(c[1].lower() for c in cols)].append(name)
    for s, names in sig.items():
        if len(names) > 5:
            out.append(f"- 同じ列構成のテーブルが {len(names):,} 個（銘柄ごとにテーブルが分かれている可能性）。列: {list(s)}")

    detailed = 0
    for name, typ in objs:
        cols = cols_of[name]
        if len(sig[tuple(c[0].lower() for c in cols)]) > 5 and detailed >= 3:
            continue  # 同型テーブルは代表数個だけ
        if detailed >= args.max_tables:
            out.append(f"\n（残り {len(objs) - detailed} 個は省略。--max-tables で増やせます）")
            break
        detailed += 1
        n = cur.execute(f"SELECT COUNT(*) FROM {q(name)}").fetchone()[0]
        out.append(f"\n## {typ} `{name}`（{n:,} 行）")
        if HINTS["指数/TOPIX"].search(name):
            out.append("  - テーブル名から指数/TOPIXのデータと思われます")
        out.append("  - 列: " + ", ".join(f"{c}:{t or '?'}" for c, t in cols))
        if typ == "table":
            idx = cur.execute(f"PRAGMA index_list({q(name)})").fetchall()
            if idx:
                parts = []
                for i in idx:
                    icols = [r[2] for r in cur.execute(f"PRAGMA index_info({q(i[1])})").fetchall()]
                    parts.append(f"{i[1]}({','.join(icols)})")
                out.append("  - インデックス: " + "; ".join(parts))
        for label, pat in HINTS.items():
            hit = [c for c, _ in cols if pat.search(c)]
            if hit:
                out.append(f"  - {label}らしき列: {hit}")
        code_col = next((c for c, _ in cols if CODE_NAMES.match(c)), None)
        date_col = next((c for c, _ in cols if DATE_NAMES.match(c)), None)
        if date_col and n:
            if code_col:
                inspect_code_date_table(con, name, code_col, date_col, out)
            else:
                lo, hi = cur.execute(f"SELECT MIN({q(date_col)}), MAX({q(date_col)}) FROM {q(name)}").fetchone()
                out.append(f"  - 日付列 `{date_col}`: {parse_date(lo)} 〜 {parse_date(hi)}（銘柄コード列は見つからず）")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
