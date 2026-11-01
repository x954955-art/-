"""財務情報（J-Quants /fins/summary）から、各時点で分かっていた会社予想・割安度と、減益・赤字の決算短信を作る（先読みなし）。

定義は docs/phase2_plan.md（事前登録）に従う:
    - 会社予想の営業利益: 通期決算短信は翌期予想（NxFOP）、四半期決算短信・業績予想の修正は当期予想（FOP）
    - ある時点の予想は、その時点までに開示されたその銘柄の財務情報のうち最も新しいものの値。
      最も新しい開示に値がなければ「不明」（古い予想にさかのぼらない）。予想の対象の事業年度の終了日が92日より前なら「不明」
    - 割安度 V = 時価総額 ÷ 会社予想の営業利益（予想が0以下・不明なら対象外）。毎営業日、時価総額帯ごとに五分位（第1が最も割安）
    - 比べる相手: 前営業日の終値の時点で同じ時価総額帯・同じ五分位に入る全銘柄の等ウェイト日次リターン（毎日入れ替え）
    - 減益・赤字の決算短信: 初回の開示で、営業利益 < 0、または前年同期（期間の終了日が約1年前）の初回の値より小さい
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .panel import PricePanel, normalize_code
from .sources import connect_ro
from .timing import first_executable

E2_TAG = "減益・赤字の決算"
STALE_DAYS = 92
PRIOR_TOL_DAYS = 20
MCAP_EDGES = [0, 100e8, 300e8, 1000e8]  # 〜100億・100〜300億・300〜1000億（1000億以上は対象外）
BAND_LABELS = ["〜100億", "100〜300億", "300〜1000億"]
N_Q = 5
N_BUCKETS = (len(MCAP_EDGES) - 1) * N_Q


# ---------------- 財務情報 ----------------

def prepare_fins(df: pd.DataFrame) -> pd.DataFrame:
    """fins_summary の行 → 予想の更新に使う種類（FY/Q/REV）だけ、開示時刻順。

    kind: FY=通期決算短信 / Q=四半期決算短信 / REV=業績予想の修正（REIT・配当予想の修正は使わない）
    f_op, f_fy_end: その開示で示された会社予想の営業利益と、その対象の事業年度の終了日
    """
    doc = df["DocType"].fillna("").astype(str)
    kind = np.select(
        [doc.str.startswith("FYFinancialStatements") & ~doc.str.contains("REIT"),
         doc.str.match(r"^(1Q|2Q|3Q|OtherPeriod)FinancialStatements"),
         doc == "EarnForecastRevision"],
        ["FY", "Q", "REV"],
        "",
    )
    out = df.assign(kind=kind)
    out = out[out["kind"] != ""].copy()
    out["code"] = out["Code"].map(normalize_code)
    t = out["DiscTime"].fillna("").astype(str).str.strip().replace("", "00:00:00")  # 時刻なしは引け後扱いになる
    out["disclosed_at"] = pd.to_datetime(out["DiscDate"].astype(str).str.strip() + " " + t, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    for c in ("CurPerEn", "CurFYEn", "NxtFYEn"):
        out[c] = pd.to_datetime(out[c], format="%Y-%m-%d", errors="coerce")  # 空文字列は NaT
    is_fy = (out["kind"] == "FY").to_numpy()
    out["f_op"] = np.where(is_fy, out["NxFOP"].astype(float), out["FOP"].astype(float))
    out["f_fy_end"] = out["NxtFYEn"].where(is_fy, out["CurFYEn"])
    out = out.dropna(subset=["disclosed_at"])
    return out.sort_values(["disclosed_at", "DiscNo"], kind="stable").reset_index(drop=True)


def load_fins(db, end: pd.Timestamp) -> pd.DataFrame:
    """end より前の日に開示された財務情報（検証期間の開示は読まない）。"""
    con = connect_ro(db)
    df = pd.read_sql_query("SELECT * FROM fins_summary WHERE DiscDate < ?", con, params=(end.strftime("%Y-%m-%d"),))
    con.close()
    return prepare_fins(df)


def earnings_events(fins: pd.DataFrame) -> pd.DataFrame:
    """減益・赤字の決算短信（同じ銘柄・同じ期間の初回の開示だけ）→ イベント（code, disclosed_at, title, company_name, tags）。"""
    rep = fins[fins["kind"].isin(["FY", "Q"])].dropna(subset=["CurPerEn"])
    first = rep.drop_duplicates(["code", "CurPerType", "CurPerEn"], keep="first")  # fins は開示時刻順
    prior = first[["code", "CurPerType", "CurPerEn", "OP"]].rename(columns={"CurPerEn": "prior_en", "OP": "prior_op"})
    cur = first.assign(target=first["CurPerEn"] - pd.DateOffset(years=1))
    m = pd.merge_asof(
        cur.sort_values("target"), prior.dropna(subset=["prior_en"]).sort_values("prior_en"),
        left_on="target", right_on="prior_en", by=["code", "CurPerType"],
        direction="nearest", tolerance=pd.Timedelta(days=PRIOR_TOL_DAYS),
    )
    loss = m["OP"] < 0
    decline = m["OP"] < m["prior_op"]  # 前年同期が見つからなければ False（赤字だけで判定）
    e = m[loss | decline]
    return pd.DataFrame({
        "code": e["code"].to_numpy(),
        "disclosed_at": e["disclosed_at"].to_numpy(),
        "title": "",
        "company_name": "",
        "tags": [(E2_TAG,)] * len(e),
    }).sort_values("disclosed_at", kind="stable").reset_index(drop=True)


def forecast_asof(events: pd.DataFrame, fins: pd.DataFrame) -> pd.DataFrame:
    """各イベントの開示時刻までに分かっていた会社予想と直前の通期実績（events と同じ index で返す）。

    f_op: 会社予想の営業利益（不明は NaN） / growth: 予想が直前の通期実績より大きい（増益予想）
    """
    ns = "datetime64[ns]"  # merge_asof はキーの精度が揃っている必要がある
    ev = events[["code", "disclosed_at"]].reset_index().astype({"disclosed_at": ns}).sort_values("disclosed_at", kind="stable")
    rec = fins[["code", "disclosed_at", "f_op", "f_fy_end"]].astype({"disclosed_at": ns})
    a = pd.merge_asof(ev, rec, on="disclosed_at", by="code", direction="backward")
    fy = fins.loc[fins["kind"] == "FY", ["code", "disclosed_at", "OP", "CurFYEn"]].astype({"disclosed_at": ns})
    a = pd.merge_asof(a, fy.rename(columns={"OP": "last_fy_op", "CurFYEn": "last_fy_end"}), on="disclosed_at", by="code", direction="backward")
    stale = a["f_fy_end"].isna() | (a["f_fy_end"] < a["disclosed_at"].dt.normalize() - pd.Timedelta(days=STALE_DAYS))
    a.loc[stale, "f_op"] = np.nan
    next_year = (a["f_fy_end"] > a["last_fy_end"]) & (a["f_fy_end"] <= a["last_fy_end"] + pd.Timedelta(days=400))
    a["growth"] = (next_year & (a["f_op"] > a["last_fy_op"])).astype(bool)
    return a.set_index("index").sort_index()[["f_op", "f_fy_end", "last_fy_op", "growth"]]


def forecast_matrix(fins: pd.DataFrame, panel: PricePanel) -> np.ndarray:
    """各営業日の終値の時点で分かっていた会社予想の営業利益 (T, N)。不明は NaN。

    開示がその日の終値に間に合うかは timing.first_executable と同じ（寄付前・立会中はその日、引け後は翌営業日から）。
    """
    T, N = panel.n_days, len(panel.codes)
    pos = fins["code"].map(panel.code_pos)
    rec = fins[pos.notna()].assign(pos=pos[pos.notna()].astype(int))
    rec = rec.assign(eff=[first_executable(t, panel.dates)[0] for t in rec["disclosed_at"]])
    rec = rec[rec["eff"] < T].drop_duplicates(["eff", "pos"], keep="last")  # 同じ日に効く開示が複数あれば最後のもの
    fy_days = (rec["f_fy_end"] - pd.Timestamp("1970-01-01")).dt.days.astype(float).to_numpy()
    e, p = rec["eff"].to_numpy(), rec["pos"].to_numpy()
    raw, end = np.full((T, N), np.nan), np.full((T, N), np.nan)
    raw[e, p], end[e, p] = rec["f_op"].to_numpy(float), fy_days
    last = np.full((T, N), -1, dtype=np.int64)
    last[e, p] = e
    last = np.maximum.accumulate(last, axis=0)  # その日までで最も新しい開示の行（値が空でもそれを使う）
    cols = np.arange(N)[None, :]
    known = last >= 0
    f = np.where(known, raw[np.maximum(last, 0), cols], np.nan)
    fy_end = np.where(known, end[np.maximum(last, 0), cols], np.nan)
    today = panel.dates.astype("datetime64[D]").astype(np.int64).astype(float)[:, None]
    f[np.isnan(fy_end) | (fy_end < today - STALE_DAYS)] = np.nan
    return f


# ---------------- 割安度と比べる相手 ----------------

def band_of(mcap: np.ndarray) -> np.ndarray:
    band = np.full(np.shape(mcap), -1, dtype=np.int16)
    for b in range(len(MCAP_EDGES) - 1):
        band[(mcap >= MCAP_EDGES[b]) & (mcap < MCAP_EDGES[b + 1])] = b
    return band


def value_cutoffs(panel: PricePanel, f: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """V (T, N)、時価総額帯 (T, N)、帯ごとの五分位の区切り (T, 帯, 4)。上場廃止後の日は対象外。"""
    T = panel.n_days
    mcap = panel.mcap
    with np.errstate(divide="ignore", invalid="ignore"):
        v = np.where((f > 0) & (mcap > 0), mcap / f, np.nan)
    band = band_of(mcap)
    alive = np.arange(T)[:, None] <= panel.last_traded[None, :]
    v[(band < 0) | ~alive] = np.nan
    nb = len(MCAP_EDGES) - 1
    cut = np.full((T, nb, N_Q - 1), np.nan)
    for b in range(nb):
        vb = np.where(band == b, v, np.nan)
        enough = np.isfinite(vb).sum(axis=1) >= N_Q
        if enough.any():
            cut[enough, b, :] = np.nanpercentile(vb[enough], [20, 40, 60, 80], axis=1).T
    return v, band, cut


def bucket_matrix(v: np.ndarray, band: np.ndarray, cut: np.ndarray) -> np.ndarray:
    """区分 = 帯 × 5 + (五分位 − 1)。求まらなければ −1。"""
    B = np.full(v.shape, -1, dtype=np.int16)
    for b in range(cut.shape[1]):
        q = np.zeros(v.shape, dtype=np.int16)
        for k in range(N_Q - 1):
            q += v > cut[:, b, k][:, None]
        m = (band == b) & np.isfinite(v) & np.isfinite(cut[:, b, 0])[:, None]
        B[m] = (b * N_Q + q)[m]
    return B


def bucket_returns(panel: PricePanel, B: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """各営業日 t の、前営業日の区分ごとの等ウェイト日次リターン (T, 区分数)。終値→終値と、その日の始値→終値。

    区分に銘柄がない日は0（比べる相手なし）。上場廃止後の日は含めない。
    """
    T, N = B.shape
    cf = panel.close_ffill
    with np.errstate(divide="ignore", invalid="ignore"):
        r_cc = np.full((T, N), np.nan)
        r_cc[1:] = cf[1:] / cf[:-1] - 1
        r_oc = np.where(panel.traded, panel.close / panel.open - 1, np.nan)
    alive = np.arange(T)[:, None] <= panel.last_traded[None, :]
    prev = np.full((T, N), -1, dtype=np.int16)
    prev[1:] = B[:-1]
    R_cc, R_oc = np.zeros((T, N_BUCKETS)), np.zeros((T, N_BUCKETS))
    for b in range(N_BUCKETS):
        m = (prev == b) & alive
        for R, r in ((R_cc, r_cc), (R_oc, r_oc)):
            ok = m & np.isfinite(r)
            cnt = ok.sum(axis=1)
            R[:, b] = np.where(cnt > 0, np.where(ok, r, 0.0).sum(axis=1) / np.maximum(cnt, 1), 0.0)
    return R_cc, R_oc


def event_buckets(ev: pd.DataFrame, cut: np.ndarray) -> pd.DataFrame:
    """イベントの V・帯・五分位・区分（開示前の最後の終値の日の区切りで決める）。"""
    mcap = ev["mcap"].to_numpy(float)
    f = ev["f_op"].to_numpy(float)
    pre = ev["pre_idx"].to_numpy(int)
    band = band_of(mcap)
    with np.errstate(divide="ignore", invalid="ignore"):
        v = np.where((f > 0) & (mcap > 0), mcap / f, np.nan)
    q = np.full(len(ev), -1)
    ok = (band >= 0) & np.isfinite(v)
    c = cut[pre[ok], band[ok], :]
    q[ok] = np.where(np.isfinite(c[:, 0]), (v[ok][:, None] > c).sum(axis=1), -1)
    return ev.assign(V=v, band=band, quintile=np.where(q >= 0, q + 1, 0), bucket=np.where(q >= 0, band * N_Q + q, -1))


def adjusted_forward(ev: pd.DataFrame, R_cc: np.ndarray, R_oc: np.ndarray, horizons) -> pd.DataFrame:
    """adj_h = イベント銘柄の買い持ちリターン − 同じ区分の比較相手の同じ期間のリターン（ret_h は study.forward_excess の値）。"""
    T = R_cc.shape[0]
    G = np.cumprod(1 + R_cc, axis=0)  # R_cc[0] = 0
    out = ev.copy()
    b = out["bucket"].to_numpy(int)
    has = b >= 0
    bb = np.maximum(b, 0)
    j = out["entry_idx"].to_numpy(int)
    first = np.where(out["entry_kind"].to_numpy() == "open", 1 + R_oc[j, bb], 1.0)  # 始値で買ったら初日は始値→終値
    for h in horizons:
        k = j + h
        valid = has & (k < T)
        kk = np.where(valid, k, 0)
        bench = first * G[kk, bb] / G[j, bb]
        out[f"adj_{h}"] = np.where(valid, (1 + out[f"ret_{h}"].to_numpy(float)) - bench, np.nan)
    return out


def calendar_time_daily_adj(ev: pd.DataFrame, panel: PricePanel, h: int, R_cc: np.ndarray, R_oc: np.ndarray) -> np.ndarray:
    """保有期間中のイベントを等ウェイトで持ったときの日次の「イベント銘柄 − 比較相手」（保有銘柄がない日は NaN）。

    stats.calendar_time_daily と同じ数え方（始値で買ったら当日の始値→終値から、終値で買ったら翌日から）。
    上場廃止後のイベント銘柄は TOPIX で運用したとみなす。
    """
    T = panel.n_days
    s, n = np.zeros(T), np.zeros(T)
    cf, tc = panel.close_ffill, panel.topix_close
    for pos, j, kind, p0, t0, b in zip(ev["pos"], ev["entry_idx"], ev["entry_kind"], ev["entry_px"], ev["topix_entry"], ev["bucket"]):
        pos, j, b = int(pos), int(j), int(b)
        end = min(j + h, T - 1)
        if kind == "open":
            days = np.arange(j, end + 1)
            prev_s, prev_m = np.r_[p0, cf[j:end, pos]], np.r_[t0, tc[j:end]]
            bench = np.r_[R_oc[j, b], R_cc[j + 1:end + 1, b]]
        else:
            days = np.arange(j + 1, end + 1)
            prev_s, prev_m = cf[j:end, pos], tc[j:end]
            bench = R_cc[j + 1:end + 1, b]
        if len(days) == 0:
            continue
        stock = cf[days, pos] / prev_s - 1
        after = days > panel.last_traded[pos]
        stock[after] = (tc[days] / prev_m - 1)[after]
        x = stock - bench
        ok = ~np.isnan(x)
        np.add.at(s, days[ok], x[ok])
        np.add.at(n, days[ok], 1)
    return np.where(n > 0, s / np.maximum(n, 1), np.nan)
