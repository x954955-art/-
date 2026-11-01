"""fetch_jquants.py の整形・ページング・再開と、保存した DB を sources.py で読めること（通信は差し替え）。"""
import gzip
import importlib.util
import json
import sqlite3
import sys
import urllib.parse
from pathlib import Path

import pandas as pd
import pytest

from tdnet_edge import sources

ROOT = Path(__file__).resolve().parents[1]
DATES = pd.bdate_range("2024-01-04", periods=60)


def load_module():
    spec = importlib.util.spec_from_file_location("fetch_jq", ROOT / "scripts" / "fetch_jquants.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_bars_frame_api_and_v1_names():
    m = load_module()
    api = pd.DataFrame([{"Date": "2024-01-04", "Code": "130A0", "O": 100, "H": 110, "L": 95, "C": 105, "Vo": 1000,
                         "Va": 1e5, "AdjFactor": 1.0, "MktCap": 1234.0, "UL": "0"}])
    v1 = pd.DataFrame({"Date": ["20240104"], "Code": ["130A0"], "Open": ["100"], "Close": ["105"], "Volume": [""],
                       "AdjustmentFactor": ["1.0"]})
    a, b = m.bars_frame(api), m.bars_frame(v1)
    assert a.loc[0, "Date"] == b.loc[0, "Date"] == "2024-01-04" and a.loc[0, "Code"] == b.loc[0, "Code"] == "130A0"
    assert a.loc[0, "MarketCapYen"] == 1234.0 * 1e6  # 百万円 → 円
    assert pd.isna(b.loc[0, "Vo"]) and pd.isna(b.loc[0, "MarketCapYen"])  # 空欄・列なしは欠損
    with pytest.raises(ValueError, match="AdjFactor"):
        m.bars_frame(api.drop(columns=["AdjFactor"]))


def test_client_paging_pacing_and_429(monkeypatch):
    m = load_module()
    pages = {None: {"data": [{"x": 1}], "pagination_key": "k1"}, "k1": {"data": [{"x": 2}]}}
    urls, slept, fail = [], [], {"n": 1}

    def fake_get(url, headers=None, timeout=120):
        urls.append(url)
        assert headers == {"x-api-key": "secret"}
        if fail["n"]:
            fail["n"] -= 1
            raise m.ApiError(429, "Too Many Requests")
        pk = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("pagination_key", [None])[0]
        return json.dumps(pages[pk]).encode()

    monkeypatch.setattr(m, "http_get", fake_get)
    now = {"t": 0.0}
    c = m.Client("secret", rpm=60, sleep=lambda s: (slept.append(s), now.__setitem__("t", now["t"] + s)), clock=lambda: now["t"])
    assert c.get_all(m.BARS, {"date": "2024-01-04"}) == [{"x": 1}, {"x": 2}]
    assert m.WAIT_429 in slept  # 429 のあとは待ってから再試行
    assert "pagination_key=k1" in urls[-1] and "date=2024-01-04" in urls[-1]  # 条件は変えずにキーだけ足す
    assert all(s >= 0 for s in slept) and c.calls == 3
    with pytest.raises(m.ApiError):  # 403 などは再試行しない
        monkeypatch.setattr(m, "http_get", lambda *a, **k: (_ for _ in ()).throw(m.ApiError(403, "plan")))
        c.get_json(m.BARS, {"date": "2024-01-04"})


def make_files(split_code="10010", delist_code="10020"):
    """月ごとの株価 CSV（gzip）と TOPIX の CSV。分割（AdjFactor=0.5）・約定なしの日・上場廃止を含む。"""
    rows = []
    for code in ("10010", "10020", "10030"):
        px = 1000.0
        for t, d in enumerate(DATES):
            if code == delist_code and t > 40:
                break
            factor = "1.0"
            if code == split_code and t == 30:  # 1:2 分割の権利落ち日
                px /= 2
                factor = "0.5"
            px *= 1.001
            no_trade = code == "10030" and t == 10
            v = "" if no_trade else None
            rows.append({"Date": d.strftime("%Y-%m-%d"), "Code": code, "O": v if no_trade else f"{px:.1f}", "H": v if no_trade else f"{px:.1f}",
                         "L": v if no_trade else f"{px:.1f}", "C": v if no_trade else f"{px:.1f}", "Vo": v if no_trade else "1000",
                         "Va": v if no_trade else "1000000", "AdjFactor": factor, "MktCap": "" if no_trade else "5000"})
    df = pd.DataFrame(rows)
    files = {}
    for ym, g in df.groupby(df["Date"].str.slice(0, 7)):
        if ym == "2024-03":
            g = g.drop(columns=["MktCap"])  # 時価総額の列がないファイルも読めること
        files[f"equities/bars/daily/historical/2024/equities_bars_daily_{ym.replace('-', '')}.csv.gz"] = g
    tp = pd.DataFrame({"Date": DATES.strftime("%Y-%m-%d"), "O": 2000.0, "H": 2010.0, "L": 1990.0, "C": [2000.0 + i for i in range(len(DATES))]})
    files["indices/bars/daily/topix/historical/2024/indices_bars_daily_topix_2024.csv.gz"] = tp
    return {k: gzip.compress(v.to_csv(index=False).encode()) for k, v in files.items()}


def test_bulk_resume_and_read_back(tmp_path, monkeypatch):
    m = load_module()
    blobs = make_files()
    modified = {k: "2025-01-01T00:00:00+00:00" for k in blobs}
    gets = []

    def fake_get(url, headers=None, timeout=120):
        u = urllib.parse.urlparse(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path.endswith("/bulk/list"):
            prefix = q["endpoint"].strip("/") + "/"
            return json.dumps({"data": [{"Key": k, "LastModified": modified[k], "Size": len(b)} for k, b in blobs.items() if k.startswith(prefix)]}).encode()
        if u.path.endswith("/bulk/get"):
            gets.append(q["key"])
            return json.dumps({"url": "https://signed.example/" + q["key"]}).encode()
        assert headers is None or "x-api-key" not in headers  # 署名付き URL には API キーを送らない
        return blobs[u.path.lstrip("/")]

    monkeypatch.setattr(m, "http_get", fake_get)
    monkeypatch.setenv("JQUANTS_API_KEY", "secret")
    db = tmp_path / "jq.sqlite"
    monkeypatch.setattr(sys, "argv", ["x", "--bulk", "--rpm", "0", "--db", str(db)])
    assert m.main() == 0
    assert len(gets) == len(blobs)
    # 再実行: 更新されていないファイルは取り直さない。LastModified が変わったファイルだけ取り直す
    gets.clear()
    assert m.main() == 0 and gets == []
    k = next(iter(blobs))
    modified[k] = "2025-02-01T00:00:00+00:00"
    assert m.main() == 0 and gets == [k]

    con = sqlite3.connect(db)
    n_rows, n_null = con.execute("SELECT COUNT(*), SUM(C IS NULL) FROM daily_quotes").fetchone()
    assert n_rows == 60 * 2 + 41 and n_null == 1  # 約定なしの日も行は残す（値は NULL）
    assert "| 2024 | 161 | 3 | 60 | 1 |" in m.report(con)
    con.close()

    cfg = {
        "prices": {"db": str(db), "table": "daily_quotes", "code": "Code", "date": "Date", "open": "O", "close": "C",
                   "volume": "Vo", "adjustment": "factor", "factor": "AdjFactor", "mcap": "MarketCapYen"},
        "topix": {"source": "table", "table": "topix", "date": "Date", "open": "O", "close": "C"},
    }
    px, tp = sources.load_prices_sqlite(cfg, {"1001", "1002", "1003"}, DATES[0], DATES[-1])
    assert len(tp) == len(DATES) and tp["open"].notna().all()
    s = px[px["code"] == "1001"].set_index("date")["close"]
    assert abs(s.loc[DATES[30]] / s.loc[DATES[29]] - 1) < 0.01  # 分割の前後で調整後の価格がつながる
    mc = px.loc[(px["code"] == "1002") & (px["date"] == DATES[5]), "mcap"].iloc[0]
    assert mc == 5000 * 1e6  # 時価総額は円
    assert px.loc[px["code"] == "1002", "date"].max() == DATES[40]  # 上場廃止銘柄は途中で終わる


def test_fins_bulk_keeps_text_and_amounts(tmp_path, monkeypatch):
    m = load_module()
    base = {"Code": "10010", "CurPerType": "FY", "CurPerSt": "2023-04-01", "CurPerEn": "2024-03-31", "CurFYSt": "2023-04-01", "CurFYEn": "2024-03-31"}
    fins = pd.DataFrame([
        {**base, "DiscDate": "2024-05-10", "DiscTime": "15:30:00", "DiscNo": "20240510000001", "DocType": "FYFinancialStatements_Consolidated_JP",
         "NxtFYSt": "2024-04-01", "NxtFYEn": "2025-03-31", "OP": "-500000000", "FOP": "", "NxFOP": "1200000000"},
        {**base, "DiscDate": "2024-08-09", "DiscTime": "15:00:00", "DiscNo": "20240809000002", "DocType": "EarnForecastRevision",
         "CurFYSt": "2024-04-01", "CurFYEn": "2025-03-31", "NxtFYSt": "", "NxtFYEn": "", "OP": "", "FOP": "900000000", "NxFOP": ""},
    ])
    key = "fins/summary/historical/2024/fins_summary_202405.csv.gz"
    blob = gzip.compress(fins.to_csv(index=False).encode())

    def fake_get(url, headers=None, timeout=120):
        u = urllib.parse.urlparse(url)
        if u.path.endswith("/bulk/list"):
            return json.dumps({"data": [{"Key": key, "LastModified": "2025-01-01T00:00:00+00:00", "Size": len(blob)}]}).encode()
        if u.path.endswith("/bulk/get"):
            return json.dumps({"url": "https://signed.example/" + key}).encode()
        return blob

    monkeypatch.setattr(m, "http_get", fake_get)
    monkeypatch.setenv("JQUANTS_API_KEY", "secret")
    db = tmp_path / "jq.sqlite"
    monkeypatch.setattr(sys, "argv", ["x", "--bulk", "--datasets", "fins", "--rpm", "0", "--db", str(db)])
    assert m.main() == 0
    con = sqlite3.connect(db)
    rows = con.execute("SELECT DocType, OP, FOP, NxFOP, NxtFYEn, Sales FROM fins_summary ORDER BY DiscDate").fetchall()
    con.close()
    assert rows[0] == ("FYFinancialStatements_Consolidated_JP", -5e8, None, 1.2e9, "2025-03-31", None)  # 空欄・列なしは NULL
    assert rows[1][:3] == ("EarnForecastRevision", None, 9e8)


def test_load_api_key_from_env_file(tmp_path, monkeypatch):
    m = load_module()
    monkeypatch.delenv("JQUANTS_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text('﻿OTHER=1\nJQUANTS_API_KEY = "abc123"\n', encoding="utf-8")
    assert m.load_api_key(env) == "abc123"
    with pytest.raises(SystemExit) as e:
        m.load_api_key(tmp_path / "none.env")
    assert "abc123" not in str(e.value)
