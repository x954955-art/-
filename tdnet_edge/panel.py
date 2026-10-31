"""銘柄×営業日の価格行列。"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


def normalize_code(code) -> str:
    """証券コードを4桁（英数字コード含む）に揃える。TDnet/J-Quants の5桁（末尾0）や '7203.T' に対応。"""
    s = str(code).strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():  # 数値として読まれた場合
        s = s[:-2]
    if len(s) == 5 and s.endswith("0"):
        s = s[:4]
    return s


@dataclass
class PricePanel:
    """価格は分割調整済み（同じ基準で比較できる）であること。売買不成立日は NaN。

    dates       : 市場の営業日カレンダー（昇順, numpy datetime64[D]）
    codes       : 銘柄コード（4桁に正規化済み）
    open, close : (T, N) 調整後の始値・終値
    volume      : (T, N) 出来高。列がない場合は全て NaN（価格の有無で約定を判定）
    topix_open  : (T,) TOPIX 始値（無ければ NaN → 前日終値で代用しフラグを立てる）
    topix_close : (T,) TOPIX 終値
    mcap        : (T, N) その日の時価総額（円）。不明なら None
    """

    dates: np.ndarray
    codes: pd.Index
    open: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    topix_open: np.ndarray
    topix_close: np.ndarray
    mcap: np.ndarray | None = None
    traded: np.ndarray = field(init=False, repr=False)
    close_ffill: np.ndarray = field(init=False, repr=False)
    last_traded: np.ndarray = field(init=False, repr=False)
    code_pos: dict = field(init=False, repr=False)

    def __post_init__(self):
        vol_ok = np.where(np.isnan(self.volume), True, self.volume > 0)
        self.traded = vol_ok & ~np.isnan(self.close)
        close_t = np.where(self.traded, self.close, np.nan)
        self.close_ffill = pd.DataFrame(close_t).ffill().to_numpy()
        any_trade = self.traded.any(axis=0)
        last = len(self.dates) - 1 - np.argmax(self.traded[::-1], axis=0)
        self.last_traded = np.where(any_trade, last, -1)
        self.code_pos = {c: i for i, c in enumerate(self.codes)}
        if self.mcap is not None:
            self.mcap = pd.DataFrame(self.mcap).ffill().to_numpy()

    @property
    def n_days(self) -> int:
        return len(self.dates)

    @classmethod
    def from_long(cls, prices: pd.DataFrame, topix: pd.DataFrame) -> "PricePanel":
        """prices: code, date, open, close, [volume], [mcap] / topix: date, close, [open]

        営業日カレンダーは TOPIX の日付を使う。
        """
        tp = topix.copy()
        tp["date"] = pd.to_datetime(tp["date"]).dt.normalize()
        tp = tp.dropna(subset=["close"]).sort_values("date").drop_duplicates("date", keep="last")
        cal = pd.DatetimeIndex(tp["date"])

        px = prices.copy()
        px["code"] = px["code"].map(normalize_code)
        px["date"] = pd.to_datetime(px["date"]).dt.normalize()
        px = px[px["date"].isin(cal)]
        px = px.drop_duplicates(["code", "date"], keep="last")
        for col in ("open", "close"):
            px[col] = pd.to_numeric(px[col], errors="coerce")
            px.loc[px[col] <= 0, col] = np.nan  # 売買不成立を0で埋めているDBへの対策

        def wide(col: str) -> pd.DataFrame:
            return px.pivot(index="date", columns="code", values=col).reindex(cal)

        close_w = wide("close")
        codes = close_w.columns
        open_w = wide("open").reindex(columns=codes)
        if "volume" in px:
            vol = wide("volume").reindex(columns=codes).to_numpy(dtype=float)
            # 行が存在しない日は出来高0（売買不成立）とみなす
            vol = np.where(np.isnan(vol) & np.isnan(close_w.to_numpy(dtype=float)), 0.0, vol)
        else:
            vol = np.full(close_w.shape, np.nan)
        mcap = wide("mcap").reindex(columns=codes).to_numpy(dtype=float) if "mcap" in px else None
        t_open = tp["open"].to_numpy(dtype=float) if "open" in tp else np.full(len(cal), np.nan)
        return cls(
            dates=cal.values.astype("datetime64[D]"),
            codes=pd.Index(codes),
            open=open_w.to_numpy(dtype=float),
            close=close_w.to_numpy(dtype=float),
            volume=vol,
            topix_open=t_open,
            topix_close=tp["close"].to_numpy(dtype=float),
            mcap=mcap,
        )
