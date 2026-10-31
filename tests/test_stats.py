import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tdnet_edge import stats

sys.path.insert(0, str(Path(__file__).parent))


def test_clustered_t_two_clusters():
    # 平均2.5、クラスタ和 -2 と +2 → var = 2/1 * 8 / 16 = 1
    assert stats.clustered_t(np.array([1, 2, 3, 4.0]), np.array([0, 0, 1, 1])) == pytest.approx(2.5)


def test_clustered_t_singletons_equals_ordinary_t():
    x = np.array([1, 2, 3, 4.0])
    ordinary = x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))
    assert stats.clustered_t(x, np.arange(4)) == pytest.approx(ordinary)


def test_block_months():
    assert [stats.block_months(h) for h in (20, 60, 120, 250)] == [1, 3, 6, 12]


def test_summarize_hides_small_groups():
    ev = pd.DataFrame(
        {"code": list("ABCDEFGHIJKL"), "g": ["x"] * 11 + ["y"], "ex_20": np.linspace(-0.05, 0.05, 12),
         "delisted_20": False, "entry_date": pd.date_range("2024-01-01", periods=12, freq="MS")}
    )
    s = stats.summarize(ev, "g", (20,)).set_index("区分")
    assert s.loc["x", "N"] == 11 and not np.isnan(s.loc["x", "平均"])
    assert s.loc["y", "N"] == 1 and np.isnan(s.loc["y"].get("平均", np.nan))


def test_newey_west_without_lags_matches_formula():
    x = np.random.default_rng(0).normal(0.001, 0.01, 500)
    u = x - x.mean()
    expect = x.mean() / np.sqrt((u @ u / len(x)) / len(x))
    assert stats.newey_west_t(x, lags=0) == pytest.approx(expect)


def test_newey_west_penalizes_overlap():
    """重なりのある系列（移動和）では、ラグなしより t値が小さくなる。"""
    e = np.random.default_rng(1).normal(0.0005, 0.01, 2000)
    overlapped = np.convolve(e, np.ones(20), mode="valid")
    assert abs(stats.newey_west_t(overlapped, lags=20)) < abs(stats.newey_west_t(overlapped, lags=0))


def test_calendar_time_daily_single_event():
    from test_study import DATES, _series, make_long, make_topix  # noqa: F401
    from tdnet_edge.panel import PricePanel

    panel = PricePanel.from_long(pd.concat([make_long(code="1111"), make_long(code="3333", until=8)]), make_topix())
    o, c = _series()
    tp = make_topix()
    # 始値エントリー（6日目）、3日保有
    ev = pd.DataFrame({"pos": [panel.code_pos["1111"]], "entry_idx": [6], "entry_kind": ["open"], "entry_px": [o[6]], "topix_entry": [tp["open"][6]]})
    d = stats.calendar_time_daily(ev, panel, 3)
    assert np.isnan(d[5]) and np.isnan(d[10])
    assert d[6] == pytest.approx((c[6] / o[6] - 1) - (tp["close"][6] / tp["open"][6] - 1))
    assert d[7] == pytest.approx((c[7] / c[6] - 1) - (tp["close"][7] / tp["close"][6] - 1))
    # 終値エントリーは翌日から数える
    ev2 = ev.assign(entry_kind="close", entry_px=c[6], topix_entry=tp["close"][6])
    d2 = stats.calendar_time_daily(ev2, panel, 3)
    assert np.isnan(d2[6]) and not np.isnan(d2[7])
    # 上場廃止（8日目が最後）後は超過0
    ev3 = ev.assign(pos=panel.code_pos["3333"])
    d3 = stats.calendar_time_daily(ev3, panel, 5)
    assert d3[9] == 0.0 and d3[11] == 0.0
