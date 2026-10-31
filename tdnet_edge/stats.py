"""集計（イベント単位の生データは出さず、集計値だけを返す）。

t値について:
    同時期のイベントは相場環境を共有し、保有期間が長いほど互いに重なる。イベントごとの
    リターンを独立とみなす普通の t値は過大になるので、カレンダータイム・ポートフォリオ法を使う:
      毎営業日、保有期間中のイベントを等ウェイトで持ったとする → その日の TOPIX 比超過リターン
      → 日次系列の平均の t値（Newey-West 補正）
    日次系列は重なりの影響を受けないので、長い保有期間でも t値が過大になりにくい。
    （暦期間でクラスタ化する方法は、250日だとクラスタが数個しかなく不安定なので使わない）
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

MIN_N = 10  # これ未満は統計を表示しない
NW_LAGS = 10


def newey_west_t(x: np.ndarray, lags: int = NW_LAGS) -> float:
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 20:
        return np.nan
    mu = x.mean()
    u = x - mu
    s = float(u @ u) / n
    for lag in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - lag / (lags + 1)) * float(u[lag:] @ u[:-lag]) / n
    return mu / np.sqrt(s / n) if s > 0 else np.nan


def calendar_time_daily(ev: pd.DataFrame, panel, h: int) -> np.ndarray:
    """保有期間中のイベントを等ウェイトで持ったときの日次 TOPIX 比超過リターン（保有銘柄がない日は NaN）。

    始値で買ったイベントは当日の始値→終値から、終値で買ったイベントは翌日から数える。
    上場廃止後は TOPIX で運用（超過0）。
    """
    T = panel.n_days
    s, n = np.zeros(T), np.zeros(T)
    cf, tc = panel.close_ffill, panel.topix_close
    for pos, j, kind, p0, t0 in zip(ev["pos"], ev["entry_idx"], ev["entry_kind"], ev["entry_px"], ev["topix_entry"]):
        pos, j = int(pos), int(j)
        end = min(j + h, T - 1)
        if kind == "open":
            days = np.arange(j, end + 1)
            prev_s, prev_m = np.r_[p0, cf[j:end, pos]], np.r_[t0, tc[j:end]]
        else:
            days = np.arange(j + 1, end + 1)
            prev_s, prev_m = cf[j:end, pos], tc[j:end]
        if len(days) == 0:
            continue
        ex = (cf[days, pos] / prev_s - 1) - (tc[days] / prev_m - 1)
        ex[days > panel.last_traded[pos]] = 0.0
        ok = ~np.isnan(ex)
        np.add.at(s, days[ok], ex[ok])
        np.add.at(n, days[ok], 1)
    return np.where(n > 0, s / np.maximum(n, 1), np.nan)


def calendar_time_t(ev: pd.DataFrame, panel, h: int) -> float:
    return newey_west_t(calendar_time_daily(ev, panel, h))


def block_months(h: int) -> int:
    return max(1, round(h / 21))


def block_id(dates: pd.Series, h: int) -> np.ndarray:
    m = dates.dt.year * 12 + dates.dt.month - 1
    return (m // block_months(h)).to_numpy()


def clustered_t(x: np.ndarray, groups: np.ndarray) -> float:
    n = len(x)
    if n < 2:
        return np.nan
    mu = float(np.mean(x))
    sums = pd.Series(x - mu).groupby(groups).sum().to_numpy()
    g = len(sums)
    if g < 2:
        return np.nan
    var = g / (g - 1) * float(np.sum(sums**2)) / n**2
    return mu / np.sqrt(var) if var > 0 else np.nan


def summarize(
    ev: pd.DataFrame,
    by: str | None,
    horizons,
    order: list | None = None,
    t_func: Callable[[pd.DataFrame, int], float] | None = None,
) -> pd.DataFrame:
    """t_func(イベント部分集合, h) で t値を計算（既定は保有期間ブロックでクラスタ化した t値）。"""
    if by is None:
        groups = [("全体", ev)]
    else:
        g = dict(tuple(ev.groupby(by, sort=True)))
        keys = [k for k in (order or []) if k in g] + [k for k in g if k not in (order or [])]
        groups = [(k, g[k]) for k in keys]
    rows = []
    for name, d in groups:
        for h in horizons:
            x = d[f"ex_{h}"].to_numpy(dtype=float)
            m = ~np.isnan(x)
            xs = x[m]
            n = int(m.sum())
            row = {"区分": name, "h": h, "N": n, "銘柄数": int(d.loc[m, "code"].nunique())}
            if n >= MIN_N:
                t = t_func(d[m], h) if t_func else clustered_t(xs, block_id(d.loc[m, "entry_date"], h))
                row.update(
                    平均=float(xs.mean()),
                    中央値=float(np.median(xs)),
                    勝率=float((xs > 0).mean()),
                    t値=t,
                    廃止=int(d.loc[m, f"delisted_{h}"].sum()),
                )
            rows.append(row)
    return pd.DataFrame(rows)


def by_keyword(ev: pd.DataFrame, horizons, cooldown, keywords, t_func=None) -> pd.DataFrame:
    """キーワード別。1イベントが複数キーワードに該当する場合はそれぞれに数える。
    間引きは (銘柄, キーワード) ごとに行う。"""
    ex = ev.explode("tags").rename(columns={"tags": "tag"})
    ex = cooldown(ex, ["code", "tag"])
    return summarize(ex, "tag", horizons, order=list(keywords), t_func=t_func)


# ---------- 表示 ----------

def _pct(v, signed=True) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    return f"{v * 100:+.1f}%" if signed else f"{v * 100:.0f}%"


def _num(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    return f"{v:.1f}"


def markdown_by_horizon(sections: list[tuple[str, pd.DataFrame]], horizons) -> str:
    """保有期間ごとに1表。行 = 切り口×区分。"""
    out = []
    for h in horizons:
        out.append(f"\n### {h}営業日後の TOPIX 比超過リターン")
        out.append("| 切り口 | 区分 | N | 銘柄数 | 平均 | 中央値 | 勝率 | t値 | うち廃止 |")
        out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for title, df in sections:
            for r in df[df["h"] == h].to_dict("records"):
                out.append(
                    f"| {title} | {r['区分']} | {r['N']:,} | {r['銘柄数']:,} | {_pct(r.get('平均'))} | "
                    f"{_pct(r.get('中央値'))} | {_pct(r.get('勝率'), signed=False)} | {_num(r.get('t値'))} | "
                    f"{int(r['廃止']) if r['N'] >= MIN_N else '-'} |"
                )
    return "\n".join(out)


def markdown_by_year(df: pd.DataFrame, horizons) -> str:
    """年別（全体）。セル = 平均 / 中央値 (N)。"""
    out = ["| 年 | " + " | ".join(f"{h}日" for h in horizons) + " |", "|---|" + "---:|" * len(horizons)]
    for y in sorted(df["区分"].unique()):
        cells = []
        for h in horizons:
            r = df[(df["区分"] == y) & (df["h"] == h)].to_dict("records")
            if not r or r[0]["N"] < MIN_N:
                cells.append(f"- (n={r[0]['N'] if r else 0})")
            else:
                r = r[0]
                cells.append(f"{_pct(r['平均'])} / {_pct(r['中央値'])} (n={r['N']:,})")
        out.append(f"| {y} | " + " | ".join(cells) + " |")
    return "\n".join(out)
