"""fetch_tdnet_history.py のキャッシュ・再開・上限検出（通信は差し替え）。"""
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location("fetch_hist", ROOT / "scripts" / "fetch_tdnet_history.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_fetch_cache_resume_and_truncation(tmp_path, monkeypatch, capsys):
    m = load_module()
    calls = []
    fail_once = {date(2024, 5, 15)}

    def fake_fetch(d, limit):
        calls.append(d)
        if d in fail_once:
            fail_once.discard(d)
            raise ConnectionError("temporary")
        n = limit if d == date(2024, 5, 14) else 2  # 5/14 は上限到達
        return [
            {"pubdate": f"{d:%Y-%m-%d} 15:30:00", "company_code": f"{1000 + i}0", "company_name": "X", "title": "減損損失の計上"}
            for i in range(n)
        ]

    monkeypatch.setattr(m, "fetch_day", fake_fetch)
    monkeypatch.setattr(m, "RETRY_WAITS", (0, 0, 0))
    args = ["x", "--start", "2024-05-13", "--end", "2024-05-19", "--limit", "5", "--interval", "0",
            "--raw-dir", str(tmp_path / "raw"), "--out-csv", str(tmp_path / "ev.csv")]
    monkeypatch.setattr(sys, "argv", args)
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "2024-05-13 〜 2024-05-17" in out  # 平日5日
    assert "| 2024 | 5 | 5 | 13 |" in out  # 2+5+2+2+2 件
    assert "上限 5 件に達した日があります" in out
    ev = pd.read_csv(tmp_path / "ev.csv", dtype={"code": str})
    assert len(ev) == 13 and set(ev["code"]) <= {f"{1000 + i}" for i in range(5)}

    # 再実行: 同じ上限ならキャッシュを使い API は呼ばない
    calls.clear()
    assert m.main() == 0
    assert calls == []
    # 上限を上げると、上限到達日だけ取り直す
    monkeypatch.setattr(sys, "argv", [a if a != "5" else "50" for a in args])
    assert m.main() == 0
    assert calls == [date(2024, 5, 14)]


def test_probe_days_cover_each_year():
    m = load_module()
    days = m.probe_days(date(2020, 1, 1), date(2022, 12, 31))
    assert {d.year for d in days} == {2020, 2021, 2022}
    assert all(d.weekday() < 5 for d in days) and len(days) == 12
