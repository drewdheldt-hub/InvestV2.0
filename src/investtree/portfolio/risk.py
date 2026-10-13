"""The risk overlays and their study: does a simple, published overlay lower the damage (drawdowns, bad months,
volatility) of a buy-and-hold portfolio without giving up too much return?

Carried over from v1 (investtree/risk.py) with the arithmetic unchanged, so v2 reproduces v1's 20-year study
to the decimal (tests/test_risk.py checks it against v1's stored SPY result). Nothing here is searched or
tuned: each policy uses its textbook setting, fixed in advance, and every decision uses only prices up to
the previous close.

  buy_and_hold    start at the target weights and never trade again (weights drift)
  rebalance       reset to the target weights on the first trading day of each month
  vol_target      the monthly-rebalanced portfolio, scaled each week to a 10% annual volatility from the
                  previous 20 days' realised volatility, never above 100% invested (Moreira & Muir 2017;
                  the rest sits in cash)
  trend           monthly: hold each asset only while its price is above its 200-day average, otherwise
                  that slice sits in cash (Faber 2007)

Cash earns nothing here, which slightly understates the overlays that hold cash. Costs: 2 bp per side on
every unit of turnover. Weeks are numpy's datetime64[W] weeks (Thursday to Wednesday), as in v1's study.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

Policy = Literal["buy_and_hold", "rebalance", "vol_target", "trend"]
POLICIES: tuple[Policy, ...] = ("buy_and_hold", "rebalance", "vol_target", "trend")
TITLES: dict[Policy, str] = {"buy_and_hold": "Buy and hold", "rebalance": "Monthly rebalance",
                             "vol_target": "Volatility target 10%", "trend": "Trend filter (200-day)"}
COST = 0.0002
VOL_TARGET, VOL_WINDOW, MAX_EXPOSURE = 0.10, 20, 1.0
TREND_WINDOW = 200

Floats = NDArray[np.float64]
Dates = NDArray[np.datetime64]


def period_starts(dates: Dates, unit: Literal["M", "W"]) -> NDArray[np.bool_]:
    """True on the first trading day of each month ('M') or week ('W')."""
    key = dates.astype(f"datetime64[{unit}]")
    return np.asarray(np.r_[True, key[1:] != key[:-1]], dtype=bool)


@dataclass(frozen=True)
class Simulation:
    returns: Floats      # daily net returns, from the second day on
    turnover: Floats     # sum of absolute weight changes each day
    exposure: Floats     # fraction of the portfolio invested each day


def simulate(dates: Dates, closes: Floats, target: Floats, policy: Policy, cost: float = COST) -> Simulation:
    """Daily net returns, exposure and turnover of one policy. Weights for day t are decided with closes up to
    day t-1 and earn day t's return."""
    T, N = closes.shape
    rets = np.zeros_like(closes)
    rets[1:] = closes[1:] / closes[:-1] - 1.0
    month = period_starts(dates, "M")
    week = period_starts(dates, "W")
    held = target.copy()                         # weights at the start of day 1
    out, turn, expo = np.zeros(T), np.zeros(T), np.zeros(T)
    base_hist: list[float] = []                  # the rebalanced portfolio's own returns, for vol targeting
    scale = 1.0
    in_trend = np.ones(N, dtype=bool)
    for t in range(1, T):
        want = held
        if policy == "rebalance" and month[t]:
            want = target.copy()
        elif policy == "vol_target" and (week[t] or month[t]):      # between adjustments the holdings drift
            if week[t] and len(base_hist) >= VOL_WINDOW:
                vol = np.std(base_hist[-VOL_WINDOW:], ddof=1) * np.sqrt(252)
                scale = min(MAX_EXPOSURE, VOL_TARGET / vol) if vol > 0 else MAX_EXPOSURE
            base = target if month[t] or held.sum() <= 0 else held / held.sum()
            want = base * scale
        elif policy == "trend":
            if month[t] and t > TREND_WINDOW:
                sma = closes[t - TREND_WINDOW:t].mean(axis=0)          # up to yesterday's close
                in_trend = closes[t - 1] > sma
            if month[t]:
                want = np.where(in_trend, target, 0.0)
        turn[t] = np.abs(want - held).sum()
        day = float(want @ rets[t])
        out[t] = day - cost * turn[t]
        expo[t] = want.sum()
        if policy == "vol_target":                                     # what the unscaled base earned
            b = want / want.sum() if want.sum() > 0 else target
            base_hist.append(float(b @ rets[t]))
        grown = want * (1.0 + rets[t])                                 # drift into tomorrow
        held = grown / (1.0 + day) if (1.0 + day) > 0 else grown
    return Simulation(out[1:], turn[1:], expo[1:])


@dataclass(frozen=True)
class Metrics:
    cagr: float
    vol: float
    sharpe: float
    max_dd: float
    calmar: float
    worst_month: float
    total_return: float
    turnover_per_year: float | None
    time_invested: float | None


def metrics(daily: Floats, dates: Dates, turnover: Floats | None = None, exposure: Floats | None = None) -> Metrics:
    """Growth, risk and drawdown of a daily return series. Sharpe uses a zero risk-free rate, as in v1."""
    eq = np.cumprod(1.0 + daily)
    years = len(daily) / 252.0
    peak = np.maximum.accumulate(np.r_[1.0, eq])[1:]
    dd = float(np.max(1.0 - eq / peak)) if len(eq) else 0.0
    vol = float(np.std(daily, ddof=1) * np.sqrt(252)) if len(daily) > 1 else 0.0
    cagr = float(eq[-1] ** (1 / years) - 1) if years > 0 and eq[-1] > 0 else -1.0
    months = dates.astype("datetime64[M]")
    starts = np.r_[0, np.flatnonzero(months[1:] != months[:-1]) + 1]
    monthly = np.multiply.reduceat(1.0 + daily, starts) - 1.0
    return Metrics(cagr=cagr, vol=vol, sharpe=float(daily.mean() * 252 / vol) if vol > 0 else 0.0,
                   max_dd=dd, calmar=cagr / dd if dd > 0 else 0.0, worst_month=float(monthly.min()),
                   total_return=float(eq[-1] - 1.0),
                   turnover_per_year=float(turnover.sum() / years) if turnover is not None else None,
                   time_invested=float(exposure.mean()) if exposure is not None else None)


@dataclass(frozen=True)
class Bootstrap:
    sharpe_diff_90: tuple[float, float]   # 5th and 95th percentile of (a's Sharpe - b's)
    sharpe_better_share: float            # share of resamples where a's Sharpe is higher
    dd_reduction_90: tuple[float, float]  # 5th and 95th percentile of (b's worst drawdown - a's)
    dd_better_share: float                # share of resamples where a's worst drawdown is smaller
    runs: int
    block: int


def block_bootstrap(a: Floats, b: Floats, runs: int = 1000, block: int = 21, seed: int = 0) -> Bootstrap:
    """Stationary block bootstrap of paired daily returns: how often, and by how much, does `a` beat `b` on
    Sharpe and on worst drawdown? Blocks of about a month keep volatility clustering and crashes intact."""
    rng = np.random.default_rng(seed)
    n = len(a)
    d_sharpe, d_dd = np.empty(runs), np.empty(runs)
    fake = np.datetime64("2000-01-03") + np.arange(n).astype("timedelta64[D]")
    for k in range(runs):
        idx = np.empty(n, dtype=int)
        i = 0
        while i < n:
            start = rng.integers(n)
            length = min(n - i, 1 + rng.geometric(1.0 / block))
            idx[i:i + length] = (start + np.arange(length)) % n
            i += length
        ma, mb = metrics(a[idx], fake), metrics(b[idx], fake)
        d_sharpe[k] = ma.sharpe - mb.sharpe
        d_dd[k] = mb.max_dd - ma.max_dd                                # > 0: `a` has the smaller drawdown

    def q(x: Floats) -> tuple[float, float]:
        return float(np.percentile(x, 5)), float(np.percentile(x, 95))

    return Bootstrap(q(d_sharpe), float((d_sharpe > 0).mean()), q(d_dd), float((d_dd > 0).mean()), runs, block)


@dataclass(frozen=True)
class PolicyResult:
    policy: Policy
    whole: Metrics
    periods: list[tuple[str, str, Metrics]]       # (from, to, metrics) for each sub-period
    vs_buy_and_hold: Bootstrap | None
    daily: Floats


@dataclass(frozen=True)
class Study:
    weights: dict[str, float]
    dates: Dates                                  # the days the returns are for (the first price day dropped)
    policies: dict[Policy, PolicyResult]


def study(dates: Dates, closes: Floats, weights: dict[str, float], periods: int = 4, bootstrap_runs: int = 1000,
          seed: int = 0) -> Study:
    """Every policy on one portfolio: whole-period metrics, sub-period metrics and the bootstrap against
    buy-and-hold. `closes` [T, N] are dividend-adjusted closes on common dates, columns in `weights` order."""
    if len(dates) < TREND_WINDOW + 60:
        raise ValueError(f"only {len(dates)} common days; the study needs at least {TREND_WINDOW + 60}")
    target = np.array(list(weights.values()), dtype=float)
    d = dates[1:]
    sims = {p: simulate(dates, closes, target, p) for p in POLICIES}
    bh = sims["buy_and_hold"].returns
    chunks = np.array_split(np.arange(len(d)), periods)
    out: dict[Policy, PolicyResult] = {}
    for p, s in sims.items():
        r = s.returns
        out[p] = PolicyResult(
            policy=p,
            whole=metrics(r, d, s.turnover, s.exposure),
            periods=[(str(d[c[0]]), str(d[c[-1]]), metrics(r[c], d[c])) for c in chunks],
            vs_buy_and_hold=None if p == "buy_and_hold" else block_bootstrap(r, bh, bootstrap_runs, seed=seed),
            daily=r)
    return Study(weights=weights, dates=d, policies=out)


def parse_weights(text: str) -> dict[str, float]:
    """'SPY:0.6,LQD:0.4' -> {'SPY': 0.6, 'LQD': 0.4}; 'SPY' -> {'SPY': 1.0}. Weights must add up to 1."""
    out: dict[str, float] = {}
    for part in text.split(","):
        sym, _, w = part.strip().partition(":")
        out[sym.strip().upper()] = float(w) if w else 1.0
    if abs(sum(out.values()) - 1.0) > 1e-9:
        raise ValueError(f"weights must add up to 1, got {sum(out.values())}")
    return out
