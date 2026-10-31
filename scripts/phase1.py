"""Phase 1: イベントスタディ（学習期間のみ。直近1年の検証期間には触れない）。

使い方:
    python scripts/phase1.py --config config/local.toml

- 検証期間の開始日は state/holdout.json に固定（初回に「開示データ最終日の1年前」で決める）
- 株価は検証期間の前日までしか読み込まない（検証期間の値動きを物理的に見ない）
- 会話/画面に出すのは集計値だけ。イベント単位の明細は output/ に保存（git管理外）
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tdnet_edge import keywords, sources, stats, study  # noqa: E402
from tdnet_edge.panel import PricePanel  # noqa: E402

def resolve_holdout(cfg_value: str | None, data_end: pd.Timestamp, state_path: Path) -> pd.Timestamp:
    """検証期間の開始日。一度決めたら state/holdout.json に固定して動かさない。"""
    if state_path.exists():
        saved = pd.Timestamp(json.loads(state_path.read_text())["holdout_start"])
        if cfg_value and pd.Timestamp(cfg_value) != saved:
            sys.exit(f"設定の holdout_start={cfg_value} が固定済みの {saved.date()} と違います（state/holdout.json）。")
        return saved
    hs = pd.Timestamp(cfg_value) if cfg_value else (data_end - pd.DateOffset(years=1) + pd.Timedelta(days=1)).normalize()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {"holdout_start": str(hs.date()), "data_end": str(data_end.date()), "decided_at": datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return hs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "local.toml"))
    ap.add_argument("--out", default=str(ROOT / "output" / "phase1"))
    ap.add_argument("--state", default=str(ROOT / "state" / "holdout.json"), help="検証期間の開始日を固定するファイル")
    args = ap.parse_args()
    cfg = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    sc = cfg.get("study", {})
    horizons = tuple(sc.get("horizons", study.HORIZONS))
    cooldown = int(sc.get("cooldown_days", 20))
    max_wait = int(sc.get("max_wait_days", 5))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 開示データ（学習期間だけ残す）
    raw = sources.load_events_files(cfg["events"]["paths"], cfg["events"].get("columns"))
    data_end = raw["disclosed_at"].max().normalize()
    hs = resolve_holdout(sc.get("holdout_start") or None, data_end, Path(args.state))
    raw = raw[raw["disclosed_at"] < hs].reset_index(drop=True)
    if raw.empty:
        sys.exit(f"学習期間（{hs.date()} より前）の開示がありません。開示データの期間を確認してください。")

    funnel = [("学習期間の開示（全件）", len(raw))]
    n_any = sum(1 for t in raw["title"] if keywords.match_keywords(t, apply_exclusions=False))
    ev = study.classify(raw)
    if ev.empty:
        sys.exit("学習期間にキーワードに該当する開示がありません。")
    funnel += [("キーワード該当（除外前）", n_any), ("訂正・経過開示・中止などで除外", n_any - len(ev)), ("キーワード該当（除外後）", len(ev))]

    # ---- 株価（検証期間の前日まで）
    start = ev["disclosed_at"].min().normalize() - pd.Timedelta(days=60)
    end = hs - pd.Timedelta(days=1)
    prices, topix = sources.load_prices_sqlite(cfg, set(ev["code"]), start, end)
    panel = PricePanel.from_long(prices, topix)

    ent = study.build_entries(ev, panel, mode="first", max_wait=max_wait)
    for s in ("株価データなし", "約定なし", "データ期間外"):
        funnel.append((f"除外: {s}", int((ent["status"] == s).sum())))
    funnel.append(("エントリー確定", int((ent["status"] == "ok").sum())))
    merged = study.merge_same_entry(ent)
    funnel.append(("同一銘柄・同一エントリーを統合", len(merged)))

    fwd = study.add_buckets(study.purge_insample(study.forward_excess(merged, panel, horizons), hs, horizons))
    cd = lambda df, key: study.apply_cooldown(df, cooldown, key)  # noqa: E731
    base = cd(fwd, ["code"])
    funnel.append((f"同一銘柄{cooldown}営業日以内の後続イベントを間引き", len(base)))
    funnel.append(("うち時価総額が不明", int((base["mcap_band"] == study.MCAP_UNKNOWN).sum())))
    funnel.append(("うちTOPIX始値なし→前日終値で代用", int(base["topix_open_fallback"].sum())))

    # ---- 感応度: 20時スキャン → 翌営業日寄付
    ent_s = study.build_entries(ev, panel, mode="scan", max_wait=max_wait)
    fwd_s = study.purge_insample(study.forward_excess(study.merge_same_entry(ent_s), panel, horizons), hs, horizons)
    base_s = study.add_buckets(cd(fwd_s, ["code"]))

    tf = lambda d, h: stats.calendar_time_t(d, panel, h)  # noqa: E731
    sections = [
        ("全体", stats.summarize(base, None, horizons, t_func=tf)),
        ("全体(20時スキャン→翌寄付)", stats.summarize(base_s, None, horizons, t_func=tf)),
        ("キーワード", stats.by_keyword(fwd, horizons, cd, keywords.KEYWORDS, t_func=tf)),
        ("時価総額", stats.summarize(base, "mcap_band", horizons, order=study.MCAP_LABELS + [study.MCAP_UNKNOWN], t_func=tf)),
        ("開示直後の反応", stats.summarize(base, "reaction_dir", horizons, order=["上げた", "下げた", "変わらず", "不明"], t_func=tf)),
    ]
    by_year = stats.summarize(base, "year", horizons)

    lines = [
        "# Phase 1 イベントスタディ（学習期間のみ）",
        f"- 学習期間: {raw['disclosed_at'].min().date()} 〜 {(hs - pd.Timedelta(days=1)).date()} の開示"
        f"（検証期間 {hs.date()} 以降は未使用。株価も {end.date()} までしか読んでいません）",
        f"- 出口日が検証期間に入る保有期間は除外（パージ）。保有期間が長いほど N が減ります",
        "- エントリー: 寄付前の開示→当日始値 / 立会中→当日終値 / 引け後・休日→翌営業日始値（約定がなければ次の約定日の始値）",
        "- 超過リターン = 銘柄の買い持ちリターン − 同期間の TOPIX リターン（上場廃止は最後の約定値で売却し残りは TOPIX 運用）",
        "- 開示直後の反応 = 開示前の最後の終値 → エントリー価格",
        "",
        "## 件数の流れ",
        "| 段階 | 件数 |",
        "|---|---:|",
        *[f"| {k} | {v:,} |" for k, v in funnel],
        stats.markdown_by_horizon(sections, horizons),
        "\n### 年別（全体）: 平均 / 中央値 (N)",
        stats.markdown_by_year(by_year, horizons),
        "",
        "t値: カレンダータイム・ポートフォリオ法（毎営業日、保有期間中のイベントを等ウェイトで持った場合の"
        "日次 TOPIX 比超過リターンの t値、Newey-West 補正）。保有期間の重なりと同時期の相関で過大にならないようにしています。"
        "|t|≥2 が目安ですが、多くの切り口を同時に見ているので偶然の当たりに注意。",
    ]
    report = "\n".join(lines)
    (out_dir / "phase1_report.md").write_text(report, encoding="utf-8")
    pd.concat([df.assign(切り口=t) for t, df in sections + [("年別", by_year)]]).to_csv(out_dir / "phase1_summary.csv", index=False)
    # 明細（表示しない）
    detail_cols = [c for c in base.columns if c not in ("title", "company_name")]
    base[detail_cols].to_csv(out_dir / "events_detail.csv.gz", index=False)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
