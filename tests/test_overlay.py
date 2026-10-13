"""The daily overlay decision makes the backtest's decision every day, and sizes orders as intended."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from investtree.portfolio import overlay, risk

FIXTURE = Path(__file__).parent / "fixtures" / "v1_risk_spy.json"


def spy() -> tuple[risk.Dates, risk.Floats]:
    v1 = json.loads(FIXTURE.read_text(encoding="utf-8"))
    dates = np.array(["2006-10-03", *v1["dates"]], dtype="datetime64[D]")
    rets = np.array(v1["daily"]["buy_and_hold"], dtype=float)
    return dates, (100.0 * np.r_[1.0, np.cumprod(1.0 + rets)])[:, None]


@pytest.mark.parametrize("policy", ["vol_target", "trend", "rebalance"])
def test_daily_decisions_reproduce_the_backtest(policy: overlay.OverlayPolicy) -> None:
    """Replay 20 years of SPY one morning at a time: the exposure held each day equals the backtest's."""
    dates, closes = spy()
    sim = risk.simulate(dates, closes, np.array([1.0]), policy)
    days = [d.item() for d in dates]
    target = np.array([1.0])
    held, scale = target.copy(), 1.0
    for t in range(1, len(days)):
        d = overlay.decide(policy, days[:t], closes[:t], target, days[t], held, scale)
        scale = d.scale
        want = held if d.want is None else d.want
        assert want.sum() == pytest.approx(sim.exposure[t - 1], abs=1e-12), days[t]
        r = closes[t] / closes[t - 1] - 1.0
        grown = want * (1.0 + r)
        held = grown / (1.0 + float(want @ r))


def test_first_run_builds_the_portfolio_at_once() -> None:
    days = [date(2026, 10, 6), date(2026, 10, 7)]           # Tuesday, Wednesday: same week and month
    closes = np.array([[100.0], [101.0]])
    d = overlay.decide("rebalance", days, closes, np.array([1.0]), date(2026, 10, 8), np.array([0.0]), first=True)
    assert d.want is not None and d.want[0] == 1.0


def test_no_trade_between_adjustment_days() -> None:
    days = [date(2026, 10, 6), date(2026, 10, 7)]
    closes = np.array([[100.0], [101.0]])
    d = overlay.decide("vol_target", days, closes, np.array([1.0]), date(2026, 10, 8), np.array([0.9]), 0.9)
    # Thursday 2026-10-08 starts a numpy week, so the volatility target adjusts (keeping its scale: too
    # little history to measure)
    assert d.want is not None and d.want[0] == pytest.approx(0.9)
    d = overlay.decide("vol_target", [*days, date(2026, 10, 8)], np.array([[100.0], [101.0], [102.0]]),
                       np.array([1.0]), date(2026, 10, 9), np.array([0.9]), 0.9)
    assert d.want is None and d.why == "not an adjustment day"


def test_plan_sells_first_and_skips_small_trades() -> None:
    orders = overlay.plan(np.array([0.5, 0.3, 0.2]), ["SPY", "TLT", "GLD"], np.array([500.0, 100.0, 200.0]),
                          np.array([0.0, 500.0, 100.0]), capital=100_000, min_trade=0.005)
    # SPY 0 -> 100 shares; TLT 500 -> 300 (sell 200); GLD 100 -> 100 (nothing)
    assert orders == [("TLT", -200), ("SPY", 100)]
    # one more share at $400 is 0.4% of capital, under the 0.5% minimum
    assert overlay.plan(np.array([1.0]), ["SPY"], np.array([400.0]), np.array([249.0]), 100_000, 0.005) == []


def test_cost_in_basis_points_is_positive_when_worse() -> None:
    assert overlay.cost_bp(1, 100.1, 100.0) == pytest.approx(10.0)      # bought above the reference
    assert overlay.cost_bp(-1, 99.9, 100.0) == pytest.approx(10.0)      # sold below it
    assert overlay.cost_bp(1, None, 100.0) is None
