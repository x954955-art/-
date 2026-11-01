"""財務情報からの会社予想（先読みなし）・減益/赤字の判定・割安度の区分・比べる相手との差と、Phase 2 スクリプト全体（合成データ）。"""
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tdnet_edge import fundamentals as fd
from tdnet_edge import study
from tdnet_edge.panel import PricePanel

ROOT = Path(__file__).resolve().parents[1]


def fins_rows(rows):
    """fins_summary と同じ形（文字列の日付、空欄は NaN）の DataFrame → prepare_fins。"""
    cols = ["DiscNo", "Code", "DiscDate", "DiscTime", "DocType", "CurPerType", "CurPerEn", "CurFYEn", "NxtFYEn", "OP", "FOP", "NxFOP"]
    df = pd.DataFrame([{c: r.get(c, "") for c in cols} for r in rows])
    for c in ("OP", "FOP", "NxFOP"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return fd.prepare_fins(df)


FY = "FYFinancialStatements_Consolidated_JP"
Q1 = "1QFinancialStatements_Consolidated_JP"
REV = "EarnForecastRevision"


def test_forecast_asof_latest_record_wins_and_stale():
    fins = fins_rows([
        {"DiscNo": "1", "Code": "10010", "DiscDate": "2024-05-10", "DiscTime": "15:30:00", "DocType": FY, "CurPerType": "FY",
         "CurPerEn": "2024-03-31", "CurFYEn": "2024-03-31", "NxtFYEn": "2025-03-31", "OP": 80e8, "NxFOP": 100e8},
        {"DiscNo": "2", "Code": "10010", "DiscDate": "2024-05-14", "DiscTime": "10:00:00", "DocType": REV, "CurPerType": "FY",
         "CurFYEn": "2025-03-31", "FOP": 150e8},
        {"DiscNo": "3", "Code": "10010", "DiscDate": "2024-05-16", "DiscTime": "15:00:00", "DocType": Q1, "CurPerType": "1Q",
         "CurPerEn": "2024-06-30", "CurFYEn": "2025-03-31", "OP": 5e8},  # 予想の記載なし
        {"DiscNo": "4", "Code": "10020", "DiscDate": "2024-05-10", "DiscTime": "15:30:00", "DocType": FY, "CurPerType": "FY",
         "CurPerEn": "2024-03-31", "CurFYEn": "2024-03-31", "NxtFYEn": "2025-03-31", "OP": 60e8, "NxFOP": 50e8},
    ])
    ev = pd.DataFrame({
        "code": ["1001", "1001", "1001", "1002", "1002", "1003"],
        "disclosed_at": pd.to_datetime(["2024-05-13 09:00", "2024-05-14 10:00", "2024-05-17 09:00", "2025-04-30 15:00", "2025-07-15 15:00", "2024-06-01 00:00"]),
    }, index=[10, 11, 12, 13, 14, 15])
    a = fd.forecast_asof(ev, fins)
    assert list(a.index) == [10, 11, 12, 13, 14, 15]
    assert a.loc[10, "f_op"] == 100e8 and a.loc[10, "growth"]  # 通期決算短信は翌期予想。80 → 100 の増益予想
    assert a.loc[11, "f_op"] == 150e8  # 同じ時刻の業績予想の修正は使える（エントリーは開示より後）
    assert np.isnan(a.loc[12, "f_op"])  # 最も新しい開示に予想がなければ不明（古い予想にさかのぼらない）
    assert a.loc[13, "f_op"] == 50e8 and not a.loc[13, "growth"]  # 60 → 50 は減益予想
    assert np.isnan(a.loc[14, "f_op"])  # 事業年度の終了日から92日超
    assert np.isnan(a.loc[15, "f_op"])  # 財務情報なし


def test_earnings_events_decline_loss_and_corrections():
    fins = fins_rows([
        {"DiscNo": "1", "Code": "20010", "DiscDate": "2023-05-12", "DiscTime": "15:00:00", "DocType": FY, "CurPerType": "FY", "CurPerEn": "2023-03-31", "OP": 100},
        {"DiscNo": "2", "Code": "20010", "DiscDate": "2024-05-10", "DiscTime": "15:00:00", "DocType": FY, "CurPerType": "FY", "CurPerEn": "2024-03-31", "OP": 80},
        {"DiscNo": "3", "Code": "20010", "DiscDate": "2024-06-01", "DiscTime": "15:00:00", "DocType": FY, "CurPerType": "FY", "CurPerEn": "2024-03-31", "OP": 70},  # 訂正
        {"DiscNo": "4", "Code": "20020", "DiscDate": "2024-08-09", "DiscTime": "15:00:00", "DocType": Q1, "CurPerType": "1Q", "CurPerEn": "2024-06-30", "OP": -5},
        {"DiscNo": "5", "Code": "20030", "DiscDate": "2023-05-12", "DiscTime": "15:00:00", "DocType": FY, "CurPerType": "FY", "CurPerEn": "2023-03-31", "OP": 100},
        {"DiscNo": "6", "Code": "20030", "DiscDate": "2024-05-10", "DiscTime": "15:00:00", "DocType": FY, "CurPerType": "FY", "CurPerEn": "2024-03-31", "OP": 120},
        {"DiscNo": "7", "Code": "20040", "DiscDate": "2023-11-10", "DiscTime": "15:00:00", "DocType": Q1.replace("1Q", "2Q"), "CurPerType": "2Q", "CurPerEn": "2023-09-30", "OP": 50},
        {"DiscNo": "8", "Code": "20040", "DiscDate": "2024-11-08", "DiscTime": "15:00:00", "DocType": Q1.replace("1Q", "2Q"), "CurPerType": "2Q", "CurPerEn": "2024-09-30", "OP": 40},
        {"DiscNo": "9", "Code": "20040", "DiscDate": "2024-12-01", "DiscTime": "15:00:00", "DocType": REV, "CurPerType": "FY", "FOP": 1},  # 修正はイベントにしない
    ])
    e = fd.earnings_events(fins)
    got = {(c, str(t)) for c, t in zip(e["code"], e["disclosed_at"])}
    assert got == {("2001", "2024-05-10 15:00:00"), ("2002", "2024-08-09 15:00:00"), ("2004", "2024-11-08 15:00:00")}
    assert all(t == (fd.E2_TAG,) for t in e["tags"])


def make_panel(dates, n_codes, mcap=50e8, open_ratio=1.0, drift=None):
    T = len(dates)
    close = np.cumprod(np.full((T, n_codes), 1.002), axis=0) * 100
    if drift is not None:  # drift[n] = 1日あたりの上乗せ（日付の index 5 以降）
        extra = np.where(np.arange(T)[:, None] >= 5, np.cumprod(np.ones((T, 1)) * (1 + np.asarray(drift))[None, :], axis=0), 1.0)
        close = close * extra
    return PricePanel(
        dates=dates.values.astype("datetime64[D]"),
        codes=pd.Index([f"{1001 + k}" for k in range(n_codes)]),
        open=close * open_ratio,
        close=close,
        volume=np.full((T, n_codes), 1000.0),
        topix_open=np.full(T, 2000.0),
        topix_close=np.full(T, 2000.0),
        mcap=np.full((T, n_codes), mcap),
    )


def test_forecast_matrix_uses_only_disclosures_before_each_close():
    dates = pd.bdate_range("2024-05-08", periods=10)  # 5/10 は金曜
    panel = make_panel(dates, 1)
    fins = fins_rows([
        {"DiscNo": "1", "Code": "10010", "DiscDate": "2024-05-10", "DiscTime": "15:30:00", "DocType": FY, "CurPerType": "FY",
         "CurPerEn": "2024-03-31", "CurFYEn": "2024-03-31", "NxtFYEn": "2025-03-31", "OP": 1, "NxFOP": 100e8},  # 引け後 → 翌営業日から
        {"DiscNo": "2", "Code": "10010", "DiscDate": "2024-05-14", "DiscTime": "10:00:00", "DocType": REV, "CurPerType": "FY",
         "CurFYEn": "2025-03-31", "FOP": 150e8},  # 立会中 → その日の終値から
        {"DiscNo": "3", "Code": "10010", "DiscDate": "2024-05-16", "DiscTime": "15:00:00", "DocType": Q1, "CurPerType": "1Q",
         "CurPerEn": "2024-06-30", "CurFYEn": "2025-03-31", "OP": 1},  # 15:00 は引け後（2024-11-05 より前）。予想なし
    ])
    f = fd.forecast_matrix(fins, panel)[:, 0]
    day = {d.strftime("%m-%d"): i for i, d in enumerate(dates)}
    assert np.isnan(f[day["05-10"]]) and f[day["05-13"]] == 100e8
    assert f[day["05-14"]] == 150e8 and f[day["05-16"]] == 150e8
    assert np.isnan(f[day["05-17"]])


def event_on_first_code(panel, f_op, h=(5, 10)):
    ev = pd.DataFrame({"code": [panel.codes[0]], "disclosed_at": [pd.Timestamp(panel.dates[5]) + pd.Timedelta(hours=8)],
                       "title": ["x"], "company_name": [""], "tags": [("特別損失",)]})
    ent = study.build_entries(ev, panel)
    assert ent.loc[0, "status"] == "ok" and ent.loc[0, "entry_kind"] == "open" and ent.loc[0, "pre_idx"] == 4
    fwd = study.forward_excess(study.merge_same_entry(ent), panel, h)
    return fwd.assign(f_op=f_op)


@pytest.mark.parametrize("open_ratio", [1.0, 0.99])
def test_adjusted_return_is_zero_when_event_moves_with_its_bucket(open_ratio):
    dates = pd.bdate_range("2024-01-04", periods=30)
    panel = make_panel(dates, 10, open_ratio=open_ratio)
    vs = np.arange(1, 11, dtype=float)  # V = 1..10 → 1,2 が第1五分位
    f = np.tile(50e8 / vs, (len(dates), 1))
    v, band, cut = fd.value_cutoffs(panel, f)
    B = fd.bucket_matrix(v, band, cut)
    assert list(B[3]) == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]
    R_cc, R_oc = fd.bucket_returns(panel, B)
    ev = fd.event_buckets(event_on_first_code(panel, 50e8), cut)
    assert ev.loc[0, "quintile"] == 1 and ev.loc[0, "bucket"] == 0
    adj = fd.adjusted_forward(ev, R_cc, R_oc, (5, 10))
    assert adj.loc[0, "adj_5"] == pytest.approx(0, abs=1e-12) and adj.loc[0, "adj_10"] == pytest.approx(0, abs=1e-12)
    daily = fd.calendar_time_daily_adj(adj, panel, 10, R_cc, R_oc)
    assert np.nanmax(np.abs(daily)) < 1e-12


def test_adjusted_return_positive_when_event_beats_its_bucket():
    dates = pd.bdate_range("2024-01-04", periods=30)
    panel = make_panel(dates, 10, drift=[0.01] + [0.0] * 9)  # 1銘柄目だけ5日目以降に毎日 +1%
    f = np.tile(50e8 / np.arange(1, 11, dtype=float), (len(dates), 1))
    v, band, cut = fd.value_cutoffs(panel, f)
    R_cc, R_oc = fd.bucket_returns(panel, fd.bucket_matrix(v, band, cut))
    adj = fd.adjusted_forward(fd.event_buckets(event_on_first_code(panel, 50e8), cut), R_cc, R_oc, (10,))
    assert adj.loc[0, "adj_10"] > 0.03  # 同じ区分のもう1銘柄（上乗せなし）と半々の比較相手に勝つ
    assert np.nanmean(fd.calendar_time_daily_adj(adj, panel, 10, R_cc, R_oc)) > 0


def test_phase2_end_to_end(tmp_path):
    spec = importlib.util.spec_from_file_location("fj", ROOT / "scripts" / "fetch_jquants.py")
    fj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fj)
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2023-01-04", periods=320)
    db = tmp_path / "jq.sqlite"
    con = fj.open_db(db)
    codes = [f"{1001 + k}0" for k in range(40)]
    rows = []
    for k, code in enumerate(codes):
        px, mc = 500.0, (20 + 25 * k) * 1e8  # 20億〜995億
        for d in dates:
            px *= 1 + rng.normal(0.0003, 0.02)
            rows.append((d.strftime("%Y-%m-%d"), code, px, px, px, px, 1000.0, 1e6, 1.0, mc / 1e6, mc))
    con.executemany("INSERT INTO daily_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO topix VALUES (?,?,?,?,?)", [(d.strftime("%Y-%m-%d"), 2000.0, 2000.0, 2000.0, 2000.0 * (1 + 0.0002 * i)) for i, d in enumerate(dates)])
    fins = []
    for k, code in enumerate(codes):
        for y in (2022, 2023):
            fins.append((f"{y}05{k:04d}", code, f"{y}-05-12", "15:00:00", FY, "FY", f"{y - 1}-04-01", f"{y}-03-31", f"{y - 1}-04-01", f"{y}-03-31",
                         f"{y}-04-01", f"{y + 1}-03-31", None, (10 + k) * 1e8 * (1.2 if y == 2022 else 1.0), None, None, None, None, None, None,
                         None, (5 + k % 7) * 1e8, None, None, None, None))
    con.executemany(f"INSERT INTO fins_summary VALUES ({','.join('?' * 26)})", fins)
    con.commit()
    con.close()
    (tmp_path / "ev").mkdir()
    ev = [{"Code": codes[rng.integers(0, len(codes))], "Date": dates[rng.integers(100, 250)].strftime("%Y-%m-%d"), "Time": "15:30",
           "Title": "特別損失の計上に関するお知らせ"} for _ in range(200)]
    pd.DataFrame(ev).to_csv(tmp_path / "ev" / "td.csv", index=False)
    cfg = f"""[events]
paths = [{json.dumps(str(tmp_path / "ev" / "*.csv"))}]
[prices]
db = {json.dumps(str(db))}
table = "daily_quotes"
code = "Code"
date = "Date"
open = "O"
close = "C"
volume = "Vo"
adjustment = "factor"
factor = "AdjFactor"
mcap = "MarketCapYen"
[topix]
source = "table"
table = "topix"
date = "Date"
open = "O"
close = "C"
[study]
cooldown_days = 20
max_wait_days = 5
"""
    (tmp_path / "c.toml").write_text(cfg, encoding="utf-8")
    state = tmp_path / "holdout.json"
    state.write_text(json.dumps({"holdout_start": dates[280].strftime("%Y-%m-%d")}), encoding="utf-8")
    cmd = [sys.executable, str(ROOT / "scripts" / "phase2.py"), "--config", str(tmp_path / "c.toml"), "--out", str(tmp_path / "out"), "--state", str(state)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "## 判定（主仮説 H2-1、60営業日）" in r.stdout and "一致を確認" in r.stdout
    assert "| E2: 減益・赤字の決算短信 | 40 |" in r.stdout  # 2023年の通期が全銘柄で前年より減益
    det = pd.read_csv(tmp_path / "out" / "events_detail.csv.gz", parse_dates=["disclosed_at"])
    assert det["disclosed_at"].max() < pd.Timestamp(dates[280])
    # 検証期間の開始日が無ければ止まる（ここでは決めない）
    r2 = subprocess.run(cmd[:-1] + [str(tmp_path / "none.json")], capture_output=True, text=True, encoding="utf-8")
    assert r2.returncode != 0 and "phase1.py" in (r2.stdout + r2.stderr)
