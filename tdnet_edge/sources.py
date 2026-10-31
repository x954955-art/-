"""データの読み込み。

イベント（TDnet開示）:
    - J-Quants TDnet アドオンの一括ダウンロード（CSV）。列名は自動判定し、設定で上書き可能
    - tdnet ライブラリ（やのしんWEB-API）の Filing / dict
株価: 手元の SQLite（列名は設定ファイルで指定）
"""
from __future__ import annotations

import glob
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from .panel import normalize_code

_EV_COLS = {
    "code": ["code", "localcode", "local_code", "company_code", "companycode", "コード", "証券コード", "銘柄コード"],
    "title": ["title", "表題", "タイトル", "件名", "subject", "documenttitle"],
    "datetime": ["pubdate", "disclosed_at", "discloseddatetime", "disclosuredatetime", "datetime", "開示日時", "公開日時"],
    "date": ["date", "discloseddate", "disclosed_date", "disclosuredate", "開示日", "公開日"],
    "time": ["time", "disclosedtime", "disclosed_time", "disclosuretime", "開示時刻", "時刻"],
    "name": ["companyname", "company_name", "name", "会社名", "銘柄名"],
}


def _find(cols, key: str, override: dict | None) -> str | None:
    if override and override.get(key):
        return override[key]
    low = {str(c).lower().replace(" ", ""): c for c in cols}
    return next((low[k] for k in _EV_COLS[key] if k in low), None)


def read_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() == ".parquet":
        return pd.read_parquet(p)
    for enc in ("utf-8-sig", "cp932"):
        try:
            return pd.read_csv(p, dtype=str, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"文字コードを判定できません: {p}")


def standardize_events(df: pd.DataFrame, columns: dict | None = None) -> pd.DataFrame:
    """任意の列名の開示一覧 → code, disclosed_at, title, company_name"""
    c_code, c_title = _find(df.columns, "code", columns), _find(df.columns, "title", columns)
    c_dt, c_date, c_time = (_find(df.columns, k, columns) for k in ("datetime", "date", "time"))
    c_name = _find(df.columns, "name", columns)
    if not c_code or not c_title or not (c_dt or c_date):
        raise ValueError(
            f"列を判定できません（見つかった列: {list(df.columns)}）。設定の [events.columns] で code/title/date/time を指定してください"
        )
    if c_dt:
        dt = pd.to_datetime(df[c_dt], errors="coerce")
    else:
        s = df[c_date].astype(str).str.strip()
        if c_time:
            s = s + " " + df[c_time].fillna("").astype(str).str.strip()
        dt = pd.to_datetime(s, errors="coerce")
    if getattr(dt.dt, "tz", None) is not None:
        dt = dt.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
    out = pd.DataFrame(
        {
            "code": df[c_code].map(normalize_code),
            "disclosed_at": dt,
            "title": df[c_title].fillna("").astype(str),
            "company_name": df[c_name].astype(str) if c_name else "",
        }
    )
    return out.dropna(subset=["disclosed_at"]).reset_index(drop=True)


def load_events_files(patterns: list[str], columns: dict | None = None) -> pd.DataFrame:
    files = sorted({f for p in patterns for f in glob.glob(str(Path(p).expanduser()))})
    if not files:
        raise FileNotFoundError(f"開示データが見つかりません: {patterns}")
    ev = pd.concat([standardize_events(read_table(f), columns) for f in files], ignore_index=True)
    return ev.drop_duplicates(["code", "disclosed_at", "title"]).sort_values("disclosed_at").reset_index(drop=True)


def events_from_tdnet(items) -> pd.DataFrame:
    """tdnet ライブラリの Filing、またはやのしんWEB-APIの dict（{"Tdnet": {...}} 形式も可）から変換。"""
    rows = []
    for it in items:
        if isinstance(it, dict):
            d = it.get("Tdnet", it)
            rows.append({"code": d.get("company_code"), "pubdate": d.get("pubdate"), "title": d.get("title"), "name": d.get("company_name")})
        else:
            rows.append({"code": it.company_code, "pubdate": it.pubdate, "title": it.title, "name": getattr(it, "company_name", "")})
    return standardize_events(pd.DataFrame(rows, columns=["code", "pubdate", "title", "name"]))


# ---------------- 株価（SQLite） ----------------

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _date_param(sample, d: pd.Timestamp):
    """DB の日付形式に合わせて比較用の値を作る。"""
    if isinstance(sample, (int, np.integer)):
        return int(d.strftime("%Y%m%d"))
    s = str(sample)
    if "/" in s[:10]:
        return d.strftime("%Y/%m/%d")
    if len(s) >= 10 and s[4] == "-":
        return d.strftime("%Y-%m-%d")
    return d.strftime("%Y%m%d")


def _to_datetime(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_datetime(s.astype("int64").astype(str), format="%Y%m%d", errors="coerce")
    return pd.to_datetime(s.astype(str).str.slice(0, 10).str.replace("/", "-"), errors="coerce")


def adjustment_multiplier(df: pd.DataFrame) -> pd.Series:
    """J-Quants 形式の調整係数（権利落ち日に 0.5 など）→ 各日の価格に掛ける累積係数。

    adjusted[t] = raw[t] × Π_{s>t} factor[s]（読み込んだ期間内で計算。リターンは比なので期間内で整合すれば十分）
    """
    f = df["factor"].fillna(1.0).astype(float).where(lambda x: x > 0, 1.0)

    def per_code(x: pd.Series) -> pd.Series:
        rev = x.iloc[::-1].cumprod().iloc[::-1]
        return rev.shift(-1).fillna(1.0)

    return f.groupby(df["code"], sort=False).transform(per_code)


def load_prices_sqlite(cfg: dict, codes: set[str], start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    """設定 [prices] [topix] に従って株価と TOPIX を読み込む。end より後の日付は読まない。"""
    p, t = cfg["prices"], cfg["topix"]
    con = sqlite3.connect(f"file:{Path(p['db']).expanduser()}?mode=ro", uri=True)
    table, c_date = p["table"], p["date"]
    sample = con.execute(f"SELECT {_q(c_date)} FROM {_q(table)} WHERE {_q(c_date)} IS NOT NULL LIMIT 1").fetchone()[0]
    lo, hi = _date_param(sample, start), _date_param(sample, end)

    mode = p.get("adjustment", "none")
    sel = {"code": p["code"], "date": c_date}
    if mode == "adjusted":
        sel.update(open=p["adj_open"], close=p["adj_close"])
        if p.get("raw_close"):
            sel["raw_close"] = p["raw_close"]
    else:
        sel.update(open=p["open"], close=p["close"])
        if mode == "factor":
            sel["factor"] = p["factor"]
    for k in ("volume", "mcap", "shares"):
        if p.get(k):
            sel[k] = p[k]
    cols = ", ".join(f"{_q(v)} AS {_q(k)}" for k, v in sel.items())
    sql = f"SELECT {cols} FROM {_q(table)} WHERE {_q(c_date)} >= ? AND {_q(c_date)} <= ?"

    tcode = normalize_code(t["code"]) if t.get("source", "code") == "code" else None
    want = set(codes) | ({tcode} if tcode else set())
    parts = []
    for chunk in pd.read_sql_query(sql, con, params=(lo, hi), chunksize=500_000):
        chunk["code"] = chunk["code"].map(normalize_code)
        parts.append(chunk[chunk["code"].isin(want)])
    px = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=list(sel))
    px["date"] = _to_datetime(px["date"])
    px = px.dropna(subset=["date"]).sort_values(["code", "date"]).reset_index(drop=True)
    for c in ("open", "close", "volume", "factor", "raw_close", "mcap", "shares"):
        if c in px:
            px[c] = pd.to_numeric(px[c], errors="coerce")

    raw_close = px["close"].copy() if mode != "adjusted" else px.get("raw_close")
    if mode == "factor":
        mult = adjustment_multiplier(px)
        px["open"] = px["open"] * mult
        px["close"] = px["close"] * mult
    if "mcap" not in px and "shares" in px and raw_close is not None:
        px["mcap"] = raw_close * px["shares"]  # 未調整の終値 × その時点の株数

    if tcode:
        tp = px[px["code"] == tcode][["date", "open", "close"]].copy()
        if tcode not in codes:
            px = px[px["code"] != tcode]
    else:
        tsel = {"date": t["date"], "close": t["close"]}
        if t.get("open"):
            tsel["open"] = t["open"]
        where = f" AND {t['where']}" if t.get("where") else ""
        tsample = con.execute(f"SELECT {_q(t['date'])} FROM {_q(t['table'])} LIMIT 1").fetchone()[0]
        tsql = (
            f"SELECT {', '.join(f'{_q(v)} AS {_q(k)}' for k, v in tsel.items())} FROM {_q(t['table'])} "
            f"WHERE {_q(t['date'])} >= ? AND {_q(t['date'])} <= ?{where}"
        )
        tp = pd.read_sql_query(tsql, con, params=(_date_param(tsample, start), _date_param(tsample, end)))
        tp["date"] = _to_datetime(tp["date"])
    con.close()
    keep = [c for c in ("code", "date", "open", "close", "volume", "mcap") if c in px]
    return px[keep], tp.dropna(subset=["date"]).reset_index(drop=True)
