"""イベントスタディ本体: エントリー決定 → 重複整理 → TOPIX比超過リターン → ホールドアウト分離。

先読みを防ぐための約束:
    - エントリー価格は開示時刻より後に成立する価格（timing.py）
    - 時価総額と「開示直後の反応」の基準は開示前の最後の終値
    - 反応は「開示前の終値 → エントリー価格」で測る（エントリー時点で見えている情報だけ）
    - 超過リターンはエントリー価格から測るので、反応の期間とは重ならない
    - 同一銘柄の後続イベントを間引くときも、後の開示の情報を前のイベントに混ぜない
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .keywords import KEYWORDS, match_keywords
from .panel import PricePanel, normalize_code
from .timing import CLOSE, OPEN, first_executable, scan_next_open

HORIZONS = (20, 60, 120, 250)
MCAP_BINS = [0, 100e8, 300e8, 1000e8, np.inf]
MCAP_LABELS = ["〜100億", "100〜300億", "300〜1000億", "(参考)1000億超"]
MCAP_UNKNOWN = "不明"


def classify(events: pd.DataFrame) -> pd.DataFrame:
    """events(code, disclosed_at, title) → キーワード該当分だけ tags 列付きで返す。"""
    ev = events.copy()
    ev["code"] = ev["code"].map(normalize_code)
    ev["disclosed_at"] = pd.to_datetime(ev["disclosed_at"])
    ev["tags"] = ev["title"].map(match_keywords)
    return ev[ev["tags"].map(len) > 0].reset_index(drop=True)


def build_entries(events: pd.DataFrame, panel: PricePanel, mode: str = "first", max_wait: int = 5) -> pd.DataFrame:
    """各開示のエントリーを決める。

    mode="first": 開示後に初めて約定できる価格（ユーザー定義）
    mode="scan" : 20時スキャン → 翌営業日寄付（Phase 3 の運用に合わせた感応度分析）
    売買不成立ならその後最初に約定した日の始値（max_wait 営業日まで）。
    """
    locate = first_executable if mode == "first" else scan_next_open
    T = panel.n_days
    out = []
    for row in events.itertuples(index=False):
        rec = {"status": "ok"}
        pos = panel.code_pos.get(row.code)
        i, kind, pre_i = locate(row.disclosed_at, panel.dates)
        if pos is None:
            rec["status"] = "株価データなし"
        elif i >= T:
            rec["status"] = "データ期間外"
        elif pre_i < 0:
            rec["status"] = "データ期間外"
        else:
            j = i
            while j < T and j - i <= max_wait and not panel.traded[j, pos]:
                j, kind = j + 1, OPEN
            if j >= T or j - i > max_wait:
                rec["status"] = "約定なし"
            else:
                if kind == OPEN and not np.isnan(panel.open[j, pos]):
                    px = panel.open[j, pos]
                else:
                    kind, px = CLOSE, panel.close[j, pos]
                if kind == OPEN:
                    t0 = panel.topix_open[j]
                    t_fallback = bool(np.isnan(t0))
                    if t_fallback:  # TOPIX始値が無い → 前日終値で代用
                        t0 = panel.topix_close[j - 1] if j > 0 else np.nan
                else:
                    t0, t_fallback = panel.topix_close[j], False
                pre_px = panel.close_ffill[pre_i, pos]
                mcap = panel.mcap[pre_i, pos] if panel.mcap is not None else np.nan
                rec.update(
                    pos=pos,
                    entry_idx=j,
                    entry_kind=kind,
                    entry_date=pd.Timestamp(panel.dates[j]),
                    entry_px=px,
                    topix_entry=t0,
                    topix_open_fallback=t_fallback,
                    pre_px=pre_px,
                    reaction=px / pre_px - 1 if pre_px > 0 else np.nan,
                    mcap=mcap,
                    wait_days=j - i,
                )
        out.append(rec)
    res = pd.concat([events.reset_index(drop=True), pd.DataFrame(out)], axis=1)
    return res


def merge_same_entry(entries: pd.DataFrame) -> pd.DataFrame:
    """同じ銘柄・同じエントリー時点（日＋始値/終値）の開示を1イベントにまとめる。

    タグは和集合、時刻は最も早いもの。どの開示もエントリー前に出ているので先読みにならない。
    寄付前の開示（始値で買う）と同日の立会中の開示（終値で買う）は別イベントのまま残す。
    """
    ok = entries[entries["status"] == "ok"].sort_values("disclosed_at")
    agg = ok.groupby(["code", "entry_idx", "entry_kind"], sort=False).agg(
        first=("disclosed_at", "idxmin"),
        tags=("tags", lambda s: tuple(k for k in KEYWORDS if any(k in t for t in s))),
        n_disclosures=("disclosed_at", "size"),
    )
    base = ok.loc[agg["first"].to_numpy()].drop(columns=["tags"]).reset_index(drop=True)
    base["tags"] = agg["tags"].to_numpy()
    base["n_disclosures"] = agg["n_disclosures"].to_numpy()
    return base.sort_values(["entry_idx", "code"]).reset_index(drop=True)


def apply_cooldown(events: pd.DataFrame, days: int, key: list[str] | None = None) -> pd.DataFrame:
    """同じ key（既定は銘柄）で、直前に残したイベントから days 営業日以内の後続イベントを落とす。"""
    if days <= 0 or events.empty:
        return events
    key = key or ["code"]
    ev = events.sort_values(["entry_idx", "disclosed_at"], kind="stable").reset_index(drop=True)
    keep = np.zeros(len(ev), dtype=bool)
    last: dict = {}
    for r, (kv, e) in enumerate(zip(ev[key].itertuples(index=False, name=None), ev["entry_idx"])):
        if kv not in last or e - last[kv] >= days:
            keep[r] = True
            last[kv] = e
    return ev[keep].reset_index(drop=True)


def forward_excess(events: pd.DataFrame, panel: PricePanel, horizons=HORIZONS) -> pd.DataFrame:
    """エントリーから h 営業日後の引けまでの TOPIX 比超過リターン（買い持ち同士の差）。

    - 途中で上場廃止（最後の約定日 < 出口日）: 最後の約定値で売却し、残り期間は TOPIX で運用したとみなす
    - 出口日がデータ範囲外: 未確定として NaN（打ち切り）
    """
    ev = events.reset_index(drop=True).copy()
    T = panel.n_days
    pos = ev["pos"].to_numpy(dtype=int)
    j = ev["entry_idx"].to_numpy(dtype=int)
    p0 = ev["entry_px"].to_numpy(dtype=float)
    t0 = ev["topix_entry"].to_numpy(dtype=float)
    last = panel.last_traded[pos]
    for h in horizons:
        k = j + h
        valid = k < T
        kk = np.where(valid, k, 0)
        delisted = valid & (last < kk)
        exit_idx = np.where(delisted, last, kk)
        p1 = panel.close_ffill[exit_idx, pos]
        tk = panel.topix_close[kk]
        t_exit = panel.topix_close[exit_idx]
        stock_val = (p1 / p0) * (tk / t_exit)  # 廃止後は TOPIX で運用（廃止がなければ tk/t_exit=1）
        mkt_val = tk / t0
        ex = np.where(valid, stock_val - mkt_val, np.nan)
        ev[f"ret_{h}"] = np.where(valid, stock_val - 1, np.nan)
        ev[f"mkt_{h}"] = np.where(valid, mkt_val - 1, np.nan)
        ev[f"ex_{h}"] = ex
        ev[f"delisted_{h}"] = delisted
        ev[f"exit_date_{h}"] = pd.to_datetime(np.where(valid, panel.dates[kk], np.datetime64("NaT", "D")))
    return ev


def purge_insample(events: pd.DataFrame, holdout_start: pd.Timestamp, horizons=HORIZONS) -> pd.DataFrame:
    """出口日が検証期間に入る保有期間は NaN にする（検証期間の値動きを学習側に混ぜない）。"""
    ev = events.copy()
    for h in horizons:
        leak = ev[f"exit_date_{h}"] >= holdout_start
        for col in (f"ret_{h}", f"mkt_{h}", f"ex_{h}"):
            ev.loc[leak, col] = np.nan
    return ev


def add_buckets(events: pd.DataFrame) -> pd.DataFrame:
    ev = events.copy()
    ev["mcap_band"] = pd.cut(ev["mcap"], MCAP_BINS, labels=MCAP_LABELS, right=False).astype(object)
    ev["mcap_band"] = ev["mcap_band"].where(ev["mcap"].notna(), MCAP_UNKNOWN)
    r = ev["reaction"].round(10)
    ev["reaction_dir"] = np.select([r > 0, r < 0], ["上げた", "下げた"], "変わらず")
    ev.loc[r.isna(), "reaction_dir"] = "不明"
    ev["year"] = ev["entry_date"].dt.year
    return ev
