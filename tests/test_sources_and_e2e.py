"""SQLite 読み込みと Phase 1 スクリプト全体の動作確認（合成データ）。"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tdnet_edge import sources

ROOT = Path(__file__).resolve().parents[1]
DATES = pd.bdate_range("2023-01-04", periods=650)  # 約2.5年（直近1年は検証期間に回る）


def make_db(path: Path, n_codes=30, split_code="10010", delist_code="10020"):
    rng = np.random.default_rng(0)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE daily_quotes(Code TEXT, Date TEXT, Open REAL, Close REAL, Volume REAL, AdjustmentFactor REAL, Shares REAL)")
    rows = []
    codes = [f"{1000 + k}0" for k in range(1, n_codes + 1)] + ["13060"]
    for code in codes:
        px = 500.0
        for t, d in enumerate(DATES):
            if code == delist_code and t > 300:
                break
            factor = 1.0
            if code == split_code and t == 100:  # 1:2 分割
                px /= 2
                factor = 0.5
            o = px * (1 + rng.normal(0, 0.005))
            px = px * (1 + rng.normal(0.0005, 0.02))
            rows.append((code, d.strftime("%Y-%m-%d"), o, px, 1000.0, factor, 1e7))
    con.executemany("INSERT INTO daily_quotes VALUES(?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return codes


def cfg_for(db: Path, events_glob: str, horizons=(5, 20)):
    return {
        "events": {"paths": [events_glob]},
        "prices": {"db": str(db), "table": "daily_quotes", "code": "Code", "date": "Date", "open": "Open", "close": "Close",
                   "volume": "Volume", "adjustment": "factor", "factor": "AdjustmentFactor", "shares": "Shares"},
        "topix": {"source": "code", "code": "1306"},
        "study": {"horizons": list(horizons), "cooldown_days": 5, "max_wait_days": 5, "holdout_start": ""},
    }


def test_load_prices_respects_end_date_and_adjusts(tmp_path):
    db = tmp_path / "p.db"
    make_db(db)
    cfg = cfg_for(db, "")
    end = DATES[150]
    px, tp = sources.load_prices_sqlite(cfg, {"1001", "1002"}, DATES[0], end)
    assert px["date"].max() <= end and tp["date"].max() <= end  # 終了日より後は読まない
    assert set(px["code"]) == {"1001", "1002"}  # 4桁に正規化、TOPIX代用ETFは除く
    assert "mcap" in px and len(tp) == 151
    # 分割の前後で調整後の価格が連続している（分割日の前日→当日の変化が半分になっていない）
    s = px[px["code"] == "1001"].set_index("date")["close"]
    assert abs(s.loc[DATES[100]] / s.loc[DATES[99]] - 1) < 0.15


def test_standardize_events_formats():
    a = pd.DataFrame({"Code": ["72030"], "Date": ["2025-05-12"], "Time": ["15:30"], "Title": ["減損損失の計上"]})
    b = pd.DataFrame({"company_code": ["7203"], "pubdate": ["2025-05-12 15:30:00"], "title": ["減損損失の計上"]})
    c = pd.DataFrame({"code": ["7203"], "disclosed_at": ["2025-05-12T06:30:00+00:00"], "title": ["減損損失の計上"]})
    for df in (a, b, c):
        ev = sources.standardize_events(df)
        assert ev.loc[0, "code"] == "7203"
        assert ev.loc[0, "disclosed_at"] == pd.Timestamp("2025-05-12 15:30")


def test_phase1_end_to_end(tmp_path):
    db = tmp_path / "p.db"
    codes = make_db(db)
    rng = np.random.default_rng(1)
    titles = ["減損損失の計上に関するお知らせ", "店舗閉鎖に関するお知らせ", "事業からの撤退について", "製品価格改定のお知らせ", "役員人事のお知らせ"]
    ev = []
    for _ in range(1500):
        d = DATES[rng.integers(0, len(DATES))]
        hhmm = ["08:30", "11:00", "15:30"][rng.integers(0, 3)]
        ev.append({"Code": codes[rng.integers(0, len(codes) - 1)], "Date": d.strftime("%Y-%m-%d"), "Time": hhmm, "Title": titles[rng.integers(0, 5)]})
    (tmp_path / "ev").mkdir()
    pd.DataFrame(ev).to_csv(tmp_path / "ev" / "td.csv", index=False)

    cfg = cfg_for(db, str(tmp_path / "ev" / "*.csv"))
    lines = []
    for sec, kv in cfg.items():
        lines.append(f"[{sec}]")
        for k, v in kv.items():
            lines.append(f"{k} = {json.dumps(v, ensure_ascii=False)}")
    (tmp_path / "c.toml").write_text("\n".join(lines), encoding="utf-8")

    state = tmp_path / "state" / "holdout.json"
    cmd = [sys.executable, str(ROOT / "scripts" / "phase1.py"), "--config", str(tmp_path / "c.toml"), "--out", str(tmp_path / "out"), "--state", str(state)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "## 件数の流れ" in r.stdout and "5営業日後" in r.stdout and "キーワード" in r.stdout
    hs = json.loads(state.read_text())["holdout_start"]
    assert f"検証期間 {hs}" in r.stdout
    # 明細ファイル: 学習期間の開示だけ、出口日はすべて検証期間より前
    det = pd.read_csv(tmp_path / "out" / "events_detail.csv.gz", parse_dates=["disclosed_at", "exit_date_5", "exit_date_20"])
    assert det["disclosed_at"].max() < pd.Timestamp(hs)
    assert det.loc[det["ex_20"].notna(), "exit_date_20"].max() < pd.Timestamp(hs)
    # 2回目は固定済みの検証期間を使い、別の日付を指定するとエラーになる
    cfg["study"]["holdout_start"] = "2023-02-01"
    bad = (tmp_path / "c.toml").read_text().replace('holdout_start = ""', 'holdout_start = "2023-02-01"')
    (tmp_path / "c2.toml").write_text(bad, encoding="utf-8")
    r2 = subprocess.run(cmd[:3] + [str(tmp_path / "c2.toml")] + cmd[4:], capture_output=True, text=True)
    assert r2.returncode != 0 and "固定済み" in (r2.stdout + r2.stderr)
