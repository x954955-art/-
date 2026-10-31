"""開示時刻から「開示後に初めて約定できる価格」を決める（日足データでの近似）。

東証の立会時間:
    前場 9:00-11:30 / 後場 12:30-15:00（2024-11-05 から大引けが 15:30 に延長）

日足しかないので、エントリーは次のルールで決める（いずれも開示時刻より後に成立する価格）:
    - 寄付前（9:00より前）の開示 ............ 当日の始値
    - 立会時間中（昼休み含む）の開示 ........ 当日の終値（引けの板寄せは開示後に成立する）
    - 大引け以降の開示 ...................... 翌営業日の始値
    - 休日の開示 ............................ 次の営業日の始値
    - 時刻が不明 ............................ 大引け後とみなす（保守的）

「開示前の最後の終値」（時価総額と開示直後の反応の基準）も同時に決める。
"""
from __future__ import annotations

from datetime import date, time

import numpy as np
import pandas as pd

SESSION_OPEN = time(9, 0)
CLOSE_EXTENSION_DATE = date(2024, 11, 5)
OPEN, CLOSE = "open", "close"


def session_close(d: date) -> time:
    return time(15, 30) if d >= CLOSE_EXTENSION_DATE else time(15, 0)


def _locate(dates: np.ndarray, d: date) -> tuple[int, bool]:
    """d 以降で最初の営業日の index と、d 自体が営業日かどうか。"""
    i = int(np.searchsorted(dates, np.datetime64(d, "D"), side="left"))
    return i, (i < len(dates) and dates[i] == np.datetime64(d, "D"))


def first_executable(disclosed_at: pd.Timestamp, dates: np.ndarray) -> tuple[int, str, int]:
    """(エントリー日のindex, 'open'|'close', 開示前の最後の終値の日のindex) を返す。

    dates は昇順の営業日カレンダー（numpy datetime64[D]）。index が len(dates) 以上なら
    データ範囲外。開示前の終値の index が -1 ならデータ範囲より前。
    """
    d = disclosed_at.date()
    i, trading = _locate(dates, d)
    has_time = not (disclosed_at.hour == 0 and disclosed_at.minute == 0 and disclosed_at.second == 0)
    if not trading:
        return i, OPEN, i - 1
    if not has_time:
        return i + 1, OPEN, i
    t = disclosed_at.time()
    if t < SESSION_OPEN:
        return i, OPEN, i - 1
    if t < session_close(d):
        return i, CLOSE, i - 1
    return i + 1, OPEN, i


def scan_next_open(disclosed_at: pd.Timestamp, dates: np.ndarray, scan_time: time = time(20, 0)) -> tuple[int, str, int]:
    """毎営業日 scan_time に当日の開示をスキャンし、翌営業日の寄付で買う運用でのエントリー。

    Phase 3（20時スキャン）の実運用に合わせた感応度分析用。
    戻り値の3つ目はスキャン日の終値の index（その時点で見えている最後の終値）。
    """
    d = disclosed_at.date()
    i, trading = _locate(dates, d)
    if trading and disclosed_at.time() < scan_time:
        scan_idx = i
    else:
        scan_idx = i + 1 if trading else i
    return scan_idx + 1, OPEN, scan_idx
