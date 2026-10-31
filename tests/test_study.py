"""合成データでエンジンの計算と先読み防止を確認する（結果の検証用ではなく、コードの正しさの確認用）。"""
import numpy as np
import pandas as pd
import pytest

from tdnet_edge import study
from tdnet_edge.panel import PricePanel, normalize_code
from tdnet_edge.sources import adjustment_multiplier

DATES = pd.bdate_range("2024-01-04", periods=30)  # 祝日は考えない合成カレンダー
T = len(DATES)
SHARES = 1e6


def _series(base=100.0, g=0.01):
    close = base * (1 + g) ** np.arange(T)
    open_ = np.r_[base, close[:-1] * 1.002]
    return open_, close


def make_long(drop=None, until=None, code="1111", bump_after=None):
    o, c = _series()
    if bump_after is not None:  # bump_after 日目より後の価格だけを書き換える（先読み検査用）
        o, c = o.copy(), c.copy()
        o[bump_after + 1 :] *= 3
        c[bump_after + 1 :] *= 3
    rows = []
    for t in range(T):
        if drop is not None and t in drop:
            continue
        if until is not None and t > until:
            continue
        rows.append({"code": code, "date": DATES[t], "open": o[t], "close": c[t], "volume": 1000.0, "mcap": c[t] * SHARES})
    return pd.DataFrame(rows)


def make_topix():
    close = 1000 * 1.001 ** np.arange(T)
    open_ = np.r_[1000.0, close[:-1] * 1.0005]
    return pd.DataFrame({"date": DATES, "open": open_, "close": close})


def at(day: int, hhmm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{DATES[day].date()} {hhmm}")


@pytest.fixture
def panel():
    prices = pd.concat([make_long(code="1111"), make_long(code="2222", drop={6}), make_long(code="3333", until=8)])
    return PricePanel.from_long(prices, make_topix())


def entries_for(panel, rows, mode="first"):
    ev = pd.DataFrame(rows, columns=["code", "disclosed_at", "title"])
    return study.build_entries(study.classify(ev), panel, mode=mode)


def test_normalize_code():
    assert [normalize_code(c) for c in ["72030", 72030, "7203", "130A0", "7203.T", "72030.0"]] == ["7203"] * 3 + ["130A", "7203", "7203"]


def test_after_close_entry_and_excess_return(panel):
    ent = entries_for(panel, [("1111", at(5, "15:30"), "減損損失の計上に関するお知らせ")])
    fwd = study.forward_excess(ent, panel, horizons=(3,))
    o, c = _series()
    tp = make_topix()
    p0, t0 = o[6], tp["open"][6]
    expect = c[9] / p0 - tp["close"][9] / t0
    assert ent.loc[0, "entry_idx"] == 6 and ent.loc[0, "entry_kind"] == "open"
    assert ent.loc[0, "pre_px"] == pytest.approx(c[5])
    assert ent.loc[0, "mcap"] == pytest.approx(c[5] * SHARES)
    assert fwd.loc[0, "ex_3"] == pytest.approx(expect)
    assert not fwd.loc[0, "delisted_3"]


def test_intraday_entry_uses_close_and_previous_close(panel):
    ent = entries_for(panel, [("1111", at(10, "10:00"), "店舗閉鎖に関するお知らせ")])
    o, c = _series()
    r = ent.iloc[0]
    assert (r.entry_idx, r.entry_kind) == (10, "close")
    assert r.entry_px == pytest.approx(c[10])
    assert r.pre_px == pytest.approx(c[9])
    assert r.reaction == pytest.approx(c[10] / c[9] - 1)
    assert r.mcap == pytest.approx(c[9] * SHARES)


def test_no_lookahead_in_entry_fields():
    """エントリー日より後の価格を書き換えても、エントリー価格・反応・時価総額は変わらない。"""
    row = [("1111", at(10, "10:00"), "減損損失の計上")]
    p1 = PricePanel.from_long(make_long(), make_topix())
    p2 = PricePanel.from_long(make_long(bump_after=10), make_topix())
    e1, e2 = entries_for(p1, row), entries_for(p2, row)
    for col in ("entry_idx", "entry_px", "pre_px", "reaction", "mcap", "topix_entry"):
        assert e1.loc[0, col] == pytest.approx(e2.loc[0, col])


def test_no_trade_day_waits_for_next_open(panel):
    ent = entries_for(panel, [("2222", at(5, "15:30"), "減損損失の計上")])
    o, c = _series()
    r = ent.iloc[0]
    assert (r.entry_idx, r.entry_kind, r.wait_days) == (7, "open", 1)
    assert r.entry_px == pytest.approx(o[7])


def test_delisting_sells_at_last_trade_then_holds_topix(panel):
    ent = entries_for(panel, [("3333", at(5, "15:30"), "減損損失の計上")])
    fwd = study.forward_excess(ent, panel, horizons=(5,))
    o, c = _series()
    tp = make_topix()
    stock_val = c[8] / o[6] * tp["close"][11] / tp["close"][8]
    assert fwd.loc[0, "delisted_5"]
    assert fwd.loc[0, "ex_5"] == pytest.approx(stock_val - tp["close"][11] / tp["open"][6])


def test_censored_horizon_is_nan(panel):
    ent = entries_for(panel, [("1111", at(5, "15:30"), "減損損失の計上")])
    fwd = study.forward_excess(ent, panel, horizons=(40,))
    assert np.isnan(fwd.loc[0, "ex_40"])


def test_status_for_missing_data(panel):
    ent = entries_for(
        panel,
        [("9999", at(5, "15:30"), "減損損失の計上"), ("1111", at(T - 1, "15:30"), "減損損失の計上")],
    )
    assert list(ent["status"]) == ["株価データなし", "データ期間外"]


def test_merge_same_entry_but_not_across_open_and_close(panel):
    rows = [
        ("1111", at(12, "08:00"), "減損損失の計上"),  # 当日始値
        ("1111", at(12, "10:00"), "特別損失の計上"),  # 当日終値 → 別イベント（始値時点では未公表）
        ("1111", at(14, "15:30"), "閉店のお知らせ"),  # 翌日始値
        ("1111", at(15, "08:00"), "減損損失の計上"),  # 同じ翌日始値 → 統合
    ]
    merged = study.merge_same_entry(entries_for(panel, rows))
    assert len(merged) == 3
    m = merged.set_index(["entry_idx", "entry_kind"])
    assert m.loc[(12, "open"), "tags"] == ("減損",)
    assert m.loc[(12, "close"), "tags"] == ("特別損失",)
    assert m.loc[(15, "open"), "tags"] == ("店舗閉鎖", "減損")
    assert m.loc[(15, "open"), "disclosed_at"] == at(14, "15:30")


def test_cooldown_keeps_first_event_per_window():
    ev = pd.DataFrame({"code": ["A"] * 4 + ["B"], "entry_idx": [0, 5, 20, 39, 5], "disclosed_at": pd.date_range("2024-01-01", periods=5)})
    kept = study.apply_cooldown(ev, 20)
    assert sorted(zip(kept["code"], kept["entry_idx"])) == [("A", 0), ("A", 20), ("B", 5)]


def test_purge_removes_windows_ending_in_holdout(panel):
    ent = entries_for(panel, [("1111", at(5, "15:30"), "減損損失の計上")])
    fwd = study.forward_excess(ent, panel, horizons=(3, 5))
    purged = study.purge_insample(fwd, DATES[10], horizons=(3, 5))  # 出口: 3日=9日目, 5日=11日目
    assert not np.isnan(purged.loc[0, "ex_3"])
    assert np.isnan(purged.loc[0, "ex_5"])


def test_buckets():
    ev = pd.DataFrame(
        {"mcap": [50e8, 100e8, 299e8, 999e8, 2000e8, np.nan], "reaction": [0.1, -0.1, 0.0, np.nan, 1e-12, -0.2],
         "entry_date": pd.date_range("2024-01-01", periods=6)}
    )
    b = study.add_buckets(ev)
    assert list(b["mcap_band"]) == ["〜100億", "100〜300億", "100〜300億", "300〜1000億", "(参考)1000億超", "不明"]
    assert list(b["reaction_dir"]) == ["上げた", "下げた", "変わらず", "不明", "変わらず", "下げた"]


def test_adjustment_multiplier_for_split():
    df = pd.DataFrame({"code": ["A"] * 5, "factor": [1, 1, 1, 0.5, 1.0]})
    raw = np.array([100, 100, 100, 50, 50.0])
    assert np.allclose(raw * adjustment_multiplier(df).to_numpy(), [50] * 5)
