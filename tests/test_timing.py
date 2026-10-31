import numpy as np
import pandas as pd
import pytest

from tdnet_edge.timing import CLOSE, OPEN, first_executable, scan_next_open

# 2024-11-04 は振替休日。2024-11-05 から大引けが 15:30 に延長
DAYS = ["2024-10-31", "2024-11-01", "2024-11-05", "2024-11-06", "2024-11-07"]
CAL = np.array(DAYS, dtype="datetime64[D]")
IDX = {d: i for i, d in enumerate(DAYS)}


@pytest.mark.parametrize(
    "ts,entry_day,kind,pre_day",
    [
        ("2024-11-05 08:30", "2024-11-05", OPEN, "2024-11-01"),  # 寄付前 → 当日始値
        ("2024-11-05 09:00", "2024-11-05", CLOSE, "2024-11-01"),  # 寄付と同時 → 寄付には間に合わない
        ("2024-11-05 12:00", "2024-11-05", CLOSE, "2024-11-01"),  # 昼休み → 当日終値
        ("2024-11-01 14:59", "2024-11-01", CLOSE, "2024-10-31"),  # 延長前の立会中
        ("2024-11-01 15:00", "2024-11-05", OPEN, "2024-11-01"),  # 延長前: 15:00 は引け後
        ("2024-11-05 15:00", "2024-11-05", CLOSE, "2024-11-01"),  # 延長後: 15:00 は立会中
        ("2024-11-05 15:30", "2024-11-06", OPEN, "2024-11-05"),  # 延長後の引け後
        ("2024-11-02 10:00", "2024-11-05", OPEN, "2024-11-01"),  # 土曜
        ("2024-11-04 10:00", "2024-11-05", OPEN, "2024-11-01"),  # 祝日
        ("2024-11-05 00:00", "2024-11-06", OPEN, "2024-11-05"),  # 時刻不明 → 引け後扱い
    ],
)
def test_first_executable(ts, entry_day, kind, pre_day):
    i, k, pre = first_executable(pd.Timestamp(ts), CAL)
    assert (i, k, pre) == (IDX[entry_day], kind, IDX[pre_day])


def test_pre_close_is_always_before_disclosure():
    """開示前の終値の日付は、開示が立会中・寄付前なら前営業日、引け後なら当日。"""
    for ts in ["2024-11-05 08:00", "2024-11-05 10:00", "2024-11-05 15:29", "2024-11-05 15:30", "2024-11-05 19:00"]:
        t = pd.Timestamp(ts)
        _, _, pre = first_executable(t, CAL)
        close_dt = pd.Timestamp(CAL[pre]) + pd.Timedelta(hours=15, minutes=30)
        assert close_dt <= t


@pytest.mark.parametrize(
    "ts,entry_day,pre_day",
    [
        ("2024-11-05 15:30", "2024-11-06", "2024-11-05"),  # 当日20時のスキャンで拾う
        ("2024-11-05 10:00", "2024-11-06", "2024-11-05"),  # 立会中でも翌寄付
        ("2024-11-05 20:30", "2024-11-07", "2024-11-06"),  # 20時より後 → 翌日のスキャン
        ("2024-11-02 10:00", "2024-11-06", "2024-11-05"),  # 土曜 → 次の営業日のスキャン
    ],
)
def test_scan_next_open(ts, entry_day, pre_day):
    i, k, pre = scan_next_open(pd.Timestamp(ts), CAL)
    assert (i, k, pre) == (IDX[entry_day], OPEN, IDX[pre_day])
