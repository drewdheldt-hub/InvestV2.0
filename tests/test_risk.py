"""The overlay replicates its study exactly: v2's port of the risk study reproduces v1's stored SPY result.

v1 re-downloads its dividend-adjusted data every night, so its files no longer hold the study's input. The
stored study does: buy and hold has no turnover, so its daily returns are SPY's own, and compounding them
rebuilds the price path the study saw (up to a constant, which no policy depends on).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from investtree.portfolio import risk

FIXTURE = Path(__file__).parent / "fixtures" / "v1_risk_spy.json"


@pytest.fixture(scope="module")
def v1() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def spy_path(v1: dict[str, Any]) -> tuple[risk.Dates, risk.Floats]:
    """The study's dates and SPY closes. The first price day (2006-10-03, the session before the first return)
    only anchors the path."""
    dates = np.array(["2006-10-03", *v1["dates"]], dtype="datetime64[D]")
    rets = np.array(v1["daily"]["buy_and_hold"], dtype=float)
    closes = 100.0 * np.r_[1.0, np.cumprod(1.0 + rets)]
    return dates, closes[:, None]


@pytest.mark.parametrize("policy", ["buy_and_hold", "vol_target", "trend"])
def test_daily_returns_match_v1(v1: dict[str, Any], policy: risk.Policy) -> None:
    dates, closes = spy_path(v1)
    sim = risk.simulate(dates, closes, np.array([1.0]), policy)
    # v1 stored returns rounded to 7 decimals; the rebuilt prices carry that rounding, nothing more
    np.testing.assert_allclose(sim.returns, v1["daily"][policy], rtol=0, atol=1e-6)


@pytest.mark.parametrize("policy", list(risk.POLICIES))
def test_whole_period_metrics_match_v1(v1: dict[str, Any], policy: risk.Policy) -> None:
    dates, closes = spy_path(v1)
    sim = risk.simulate(dates, closes, np.array([1.0]), policy)
    got = risk.metrics(sim.returns, dates[1:], sim.turnover, sim.exposure)
    want = v1["metrics"][policy]
    for name in ("cagr", "vol", "sharpe", "max_dd", "calmar", "worst_month", "total_return",
                 "turnover_per_year", "time_invested"):
        assert getattr(got, name) == pytest.approx(want[name], abs=1e-5), name


def test_success_measures_hold_for_the_volatility_target(v1: dict[str, Any]) -> None:
    """The Blueprint's targets: worst drawdown at least a third smaller, Sharpe at least buy and hold's, and a
    smaller drawdown in at least 90% of bootstrap resamples."""
    dates, closes = spy_path(v1)
    s = risk.study(dates, closes, {"SPY": 1.0}, bootstrap_runs=200)
    vt, bh = s.policies["vol_target"], s.policies["buy_and_hold"]
    assert vt.whole.max_dd <= bh.whole.max_dd * 2 / 3
    assert vt.whole.sharpe >= bh.whole.sharpe
    assert vt.vs_buy_and_hold is not None and vt.vs_buy_and_hold.dd_better_share >= 0.9


def test_bootstrap_matches_v1_with_the_same_seed(v1: dict[str, Any]) -> None:
    dates, closes = spy_path(v1)
    sims = {p: risk.simulate(dates, closes, np.array([1.0]), p) for p in ("buy_and_hold", "vol_target")}
    want = v1["bootstrap"]["vol_target"]
    got = risk.block_bootstrap(sims["vol_target"].returns, sims["buy_and_hold"].returns,
                               runs=want["runs"], block=want["block"], seed=0)
    assert got.dd_better_share == pytest.approx(want["dd_better_share"], abs=1e-9)
    assert got.sharpe_better_share == pytest.approx(want["sharpe_better_share"], abs=0.005)


def test_weekly_and_monthly_starts() -> None:
    days = np.array(["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"], dtype="datetime64[D]")
    assert list(risk.period_starts(days, "M")) == [True, False, True, False]
    # numpy weeks run Thursday to Wednesday: Thursday 2026-10-01 starts a new one
    assert list(risk.period_starts(days, "W")) == [True, False, True, False]


def test_trend_holds_cash_below_the_200_day_average() -> None:
    n = 260
    dates = np.datetime64("2025-01-01") + np.arange(n).astype("timedelta64[D]")
    falling = np.linspace(200.0, 100.0, n)[:, None]
    sim = risk.simulate(dates, falling, np.array([1.0]), "trend")
    assert sim.exposure[-1] == 0.0


def test_parse_weights() -> None:
    assert risk.parse_weights("spy:0.6, LQD:0.4") == {"SPY": 0.6, "LQD": 0.4}
    assert risk.parse_weights("SPY") == {"SPY": 1.0}
    with pytest.raises(ValueError):
        risk.parse_weights("SPY:0.6,LQD:0.6")


def test_the_band_rebalances_only_after_a_drift_beyond_five_points() -> None:
    n = 40
    dates = np.datetime64("2026-01-05") + np.arange(n).astype("timedelta64[D]")
    # A rises 3% a day, B is flat: A's weight passes 55% on the 7th day after each reset (1.03^7 > 55/45)
    closes = np.c_[100 * 1.03 ** np.arange(n), np.full(n, 100.0)]
    sim = risk.simulate(dates, closes, np.array([0.5, 0.5]), "band")
    trades = np.flatnonzero(sim.turnover > 0)
    assert len(trades) >= 4 and np.all(np.diff(trades) == 7)         # every 7 days, not daily
    w = 1.03 ** 7 / (1.03 ** 7 + 1)                                    # A's weight when the band is crossed
    assert sim.turnover[trades[0]] == pytest.approx(2 * (w - 0.5))


def test_a_single_asset_never_drifts_so_the_band_never_trades(v1: dict[str, Any]) -> None:
    dates, closes = spy_path(v1)
    assert risk.simulate(dates, closes, np.array([1.0]), "band").turnover.sum() == 0


def test_band_verdict_applies_the_rule_fixed_in_advance() -> None:
    rng = np.random.default_rng(3)
    n = 1500
    dates = np.datetime64("2018-01-01") + np.arange(n).astype("timedelta64[D]")
    closes = 100 * np.cumprod(1 + rng.normal(0.0003, [0.012, 0.005], (n, 2)), axis=0)
    v = risk.band_vs_monthly(dates, closes, np.array([0.6, 0.4]), runs=100)
    assert len(v.reasons) == 3 and v.turnover_band < v.turnover_monthly
    assert v.adopt == all(r.startswith("met") for r in v.reasons)
