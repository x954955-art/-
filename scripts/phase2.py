"""Phase 2: 割安度で絞り、同じ規模・同じ割安度の銘柄と比べる（学習期間のみ。条件は docs/phase2_plan.md で事前登録）。

使い方:
    python scripts/phase2.py --config config/local.toml

- 事前登録の文書が固定時から変わっていたら止まる（SHA-256 で確認）
- 検証期間の開始日は Phase 1 が固定した state/holdout.json を使う（無ければ止まる。ここでは決めない）
- 株価・財務情報・開示は検証期間の前日までしか読み込まない
- 判定は主仮説 H2-1 の60営業日だけ（平均 ≥ +2.0% かつ t値 ≥ 3.0）。副仮説・参考の表は判定に使わない
- 会話/画面に出すのは集計値だけ。イベント単位の明細は output/phase2/ に保存（git管理外）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdnet_edge import fundamentals as fd  # noqa: E402
from tdnet_edge import keywords, sources, stats, study  # noqa: E402
from tdnet_edge.panel import PricePanel, normalize_code  # noqa: E402

PLAN = ROOT / "docs" / "phase2_plan.md"
PLAN_SHA256 = "89e35cecaaa057d7d2415e3d3fa87e29de930e8acdaedac71038bca123cfa025"
HORIZONS = (20, 60, 120)
DECIDE_H = 60
PASS_MEAN, PASS_T = 0.02, 3.0


def plan_hash() -> str:
    return hashlib.sha256(PLAN.read_bytes().replace(b"\r\n", b"\n")).hexdigest()  # 改行コードの違いは無視する


def describe(name: str, d: pd.DataFrame, horizons, t_func) -> list[dict]:
    """調整後の超過リターン（adj_h）の集計。参考として TOPIX 比（ex_h）の平均も付ける。"""
    rows = []
    for h in horizons:
        x = d[f"adj_{h}"].to_numpy(float)
        m = ~np.isnan(x)
        row = {"区分": name, "h": h, "N": int(m.sum()), "銘柄数": int(d.loc[m, "code"].nunique())}
        if row["N"] >= stats.MIN_N:
            xs = x[m]
            row.update(平均=float(xs.mean()), 中央値=float(np.median(xs)), 勝率=float((xs > 0).mean()),
                       t値=float(t_func(d[m], h)), TOPIX比=float(np.nanmean(d.loc[m, f"ex_{h}"])))
        rows.append(row)
    return rows


def table(rows: list[dict], first_col: str) -> str:
    out = [f"| {first_col} | 保有 | N | 銘柄数 | 平均 | 中央値 | 勝率 | t値 | 参考: TOPIX比の平均 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        out.append(
            f"| {r['区分']} | {r['h']}日 | {r['N']:,} | {r['銘柄数']:,} | {stats._pct(r.get('平均'))} | {stats._pct(r.get('中央値'))} | "
            f"{stats._pct(r.get('勝率'), signed=False)} | {stats._num(r.get('t値'))} | {stats._pct(r.get('TOPIX比'))} |"
        )
    return "\n".join(out)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows で出力をパイプしても日本語が化けないように
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "local.toml"))
    ap.add_argument("--out", default=str(ROOT / "output" / "phase2"))
    ap.add_argument("--state", default=str(ROOT / "state" / "holdout.json"), help="Phase 1 が固定した検証期間の開始日")
    args = ap.parse_args()

    if plan_hash() != PLAN_SHA256:
        sys.exit("docs/phase2_plan.md が固定したときと違います（SHA-256 不一致）。条件を変えるなら新しい仮説として扱ってください。")
    state = Path(args.state)
    if not state.exists():
        sys.exit(f"{state} がありません。先に scripts/phase1.py を実行して検証期間を固定してください。")
    hs = pd.Timestamp(json.loads(state.read_text(encoding="utf-8"))["holdout_start"])
    end = hs - pd.Timedelta(days=1)
    cfg = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    sc = cfg.get("study", {})
    cooldown, max_wait = int(sc.get("cooldown_days", 20)), int(sc.get("max_wait_days", 5))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- イベント: E1（キーワード開示）+ E2（減益・赤字の決算短信）。学習期間だけ
    raw = sources.load_events_files(cfg["events"]["paths"], cfg["events"].get("columns"))
    e1 = study.classify(raw[raw["disclosed_at"] < hs])
    fins = fd.load_fins(cfg["prices"]["db"], hs)
    e2 = fd.earnings_events(fins)
    cols = ["code", "disclosed_at", "title", "company_name", "tags"]
    ev = pd.concat([e1[cols], e2[cols]], ignore_index=True).sort_values("disclosed_at", kind="stable").reset_index(drop=True)
    funnel = [("E1: キーワード開示", len(e1)), ("E2: 減益・赤字の決算短信", len(e2))]

    # ---- 株価: 比べる相手を作るため全銘柄（検証期間の前日まで）
    con = sources.connect_ro(cfg["prices"]["db"])
    codes = {normalize_code(c) for (c,) in con.execute(f'SELECT DISTINCT "{cfg["prices"]["code"]}" FROM "{cfg["prices"]["table"]}"')}
    con.close()
    prices, topix = sources.load_prices_sqlite(cfg, codes, ev["disclosed_at"].min().normalize() - pd.Timedelta(days=60), end)
    panel = PricePanel.from_long(prices, topix)
    del prices

    ent = study.build_entries(ev, panel, mode="first", max_wait=max_wait)
    for s in ("株価データなし", "約定なし", "データ期間外"):
        funnel.append((f"除外: {s}", int((ent["status"] == s).sum())))
    merged = study.merge_same_entry(ent, keywords=keywords.KEYWORDS + [fd.E2_TAG])
    fwd = study.purge_insample(study.forward_excess(merged, panel, HORIZONS), hs, HORIZONS)
    base = study.apply_cooldown(fwd, cooldown, ["code"])
    funnel += [("同一銘柄・同一エントリーを統合", len(merged)), (f"同一銘柄{cooldown}営業日以内の後続イベントを間引き", len(base))]

    # ---- 割安度（各時点で分かっていた会社予想だけ）と比べる相手
    f = fd.forecast_matrix(fins, panel)
    v, band, cut = fd.value_cutoffs(panel, f)
    B = fd.bucket_matrix(v, band, cut)
    del f, v, band
    R_cc, R_oc = fd.bucket_returns(panel, B)
    del B
    base = fd.event_buckets(base.join(fd.forecast_asof(base, fins)), cut)
    base = fd.adjusted_forward(base, R_cc, R_oc, HORIZONS)
    base["has_e2"] = base["tags"].map(lambda t: fd.E2_TAG in t)
    base["has_e1"] = base["tags"].map(lambda t: any(k in t for k in keywords.KEYWORDS))

    uni = base[base["quintile"] >= 1]  # 時価総額1000億円未満で割安度が求まる
    h21 = uni[uni["quintile"] == 1]
    h22 = h21[h21["growth"]]
    h23 = h21[h21["has_e2"]]
    funnel += [
        ("うち時価総額1000億円未満", int((base["band"] >= 0).sum())),
        ("うち割安度が求まる（会社予想の営業利益 > 0）", len(uni)),
        ("H2-1: 第1五分位（最も割安）", len(h21)),
        ("H2-2: H2-1 のうち増益予想", len(h22)),
        ("H2-3: H2-1 のうち減益・赤字の決算を含む", len(h23)),
    ]

    tf = lambda d, h: stats.newey_west_t(fd.calendar_time_daily_adj(d, panel, h, R_cc, R_oc))  # noqa: E731
    hyp = describe("H2-1 主仮説", h21, HORIZONS, tf) + describe("H2-2 増益予想", h22, HORIZONS, tf) + describe("H2-3 決算を含む", h23, HORIZONS, tf)
    quint = sum((describe(f"第{q}五分位", uni[uni["quintile"] == q], [DECIDE_H], tf) for q in range(1, fd.N_Q + 1)), [])
    bands = sum((describe(fd.BAND_LABELS[b], h21[h21["band"] == b], [DECIDE_H], tf) for b in range(len(fd.BAND_LABELS))), [])
    by_year = [
        {"年": y, **{k: v for k, v in r.items() if k != "区分"}}
        for y, g in h21.groupby(h21["entry_date"].dt.year) for r in describe(str(y), g, [DECIDE_H], lambda d, h: np.nan)
    ]

    main_row = next(r for r in hyp if r["区分"].startswith("H2-1") and r["h"] == DECIDE_H)
    mean, t = main_row.get("平均", np.nan), main_row.get("t値", np.nan)
    passed = bool(np.isfinite(mean) and np.isfinite(t) and mean >= PASS_MEAN and t >= PASS_T)
    n_t = sum(np.isfinite(r.get("t値", np.nan)) for r in hyp + quint + bands)

    lines = [
        "# Phase 2 イベントスタディ（学習期間のみ）",
        f"- 事前登録: docs/phase2_plan.md（SHA-256 {PLAN_SHA256[:12]}… 一致を確認）",
        f"- 学習期間: {end.date()} までの開示・財務情報・株価（検証期間 {hs.date()} 以降は未使用）",
        "- 調整後の超過リターン = イベント銘柄の買い持ちリターン − 同じ時価総額帯・同じ割安度五分位の全銘柄（等ウェイト・毎日入れ替え）",
        "- 割安度 = 時価総額 ÷ 会社予想の営業利益（イベントの開示時刻までに分かっていたもの）。第1五分位が最も割安",
        "",
        "## 判定（主仮説 H2-1、60営業日）",
        f"- 平均 {stats._pct(mean)}（基準 +{PASS_MEAN * 100:.1f}% 以上） / t値 {stats._num(t)}（基準 {PASS_T:.1f} 以上） → **{'合格' if passed else '不合格'}**",
        "",
        "## 件数の流れ",
        "| 段階 | 件数 |",
        "|---|---:|",
        *[f"| {k} | {v:,} |" for k, v in funnel],
        "",
        "## 仮説ごとの調整後の超過リターン",
        table(hyp, "仮説"),
        "",
        "## 参考: 割安度の五分位別（E1∪E2、時価総額1000億円未満、60営業日）",
        table(quint, "五分位"),
        "",
        "## 参考: H2-1 の時価総額帯別（60営業日）",
        table(bands, "時価総額帯"),
        "",
        "## 参考: H2-1 の年別（60営業日）: 平均 / 中央値 (N)",
        "| 年 | 60日 |",
        "|---|---:|",
        *[f"| {r['年']} | {stats._pct(r.get('平均'))} / {stats._pct(r.get('中央値'))} (n={r['N']:,}) |" for r in by_year],
        "",
        f"t値を表示したセル: {n_t} 個（Phase 1 の68個に足して試行数として記録する）。",
        "t値: カレンダータイム法（毎営業日、保有中のイベントの「イベント銘柄 − 比較相手」の日次リターンを等ウェイトで平均した系列、Newey-West 補正）。",
    ]
    report = "\n".join(lines)
    (out_dir / "phase2_report.md").write_text(report, encoding="utf-8")
    pd.DataFrame(hyp + quint + bands).to_csv(out_dir / "phase2_summary.csv", index=False)
    detail = base.drop(columns=["title", "company_name"]).assign(tags=base["tags"].map("|".join))
    detail.to_csv(out_dir / "events_detail.csv.gz", index=False)  # 明細（表示しない）
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
