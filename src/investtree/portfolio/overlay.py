"""One day's overlay decision for a portfolio: the policies of risk.py, applied to today.

Pure functions with no broker: execution/overlay_runner.py feeds them the account and sends the orders (on
paper), and suggest.py uses them to propose trades for a real account. Each weekday before the open the
decision uses daily closes up to yesterday, the convention of risk.simulate (day t's weights come from
closes up to t-1), except that trades fill at today's open rather than at yesterday's close.

  rebalance    first trading day of each month: back to the target weights
  vol_target   the monthly rebalance, scaled each week to 10% annual volatility from the last 20 days,
               never above fully invested
  trend        first trading day of each month: each asset at its target weight while its close is above
               its 200-day average, otherwise that slice in cash
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np

from .risk import MAX_EXPOSURE, TREND_WINDOW, VOL_TARGET, VOL_WINDOW, Floats

OverlayPolicy = Literal["rebalance", "vol_target", "trend"]
OVERLAY_POLICIES: tuple[OverlayPolicy, ...] = ("rebalance", "vol_target", "trend")


def period_starts(days: list[date], today: date) -> tuple[bool, bool]:
    """(first trading day of the month, first trading day of the week) for `today`, given the trading days
    before it. Weeks as the risk study counts them (numpy's Thursday-to-Wednesday weeks), so the weekly
    adjustment falls on the same days as in the measured backtest."""
    if not days:
        return True, True
    last = days[-1]
    month = (today.year, today.month) != (last.year, last.month)
    week = np.datetime64(today, "W") != np.datetime64(last, "W")
    return month, bool(week)


@dataclass(frozen=True)
class Decision:
    want: Floats | None      # weights of capital wanted at today's open; None means no trade today
    why: str
    scale: float             # the volatility target's exposure, carried to the next run


def decide(policy: OverlayPolicy, days: list[date], closes: Floats, target: Floats, today: date, held: Floats,
           scale: float = MAX_EXPOSURE, first: bool = False) -> Decision:
    """Today's decision. days: the trading dates of `closes` [T, N] (oldest first), all before today.
    held: today's weights of capital (from the account at yesterday's closes). scale: the exposure from the
    last weekly adjustment. first: the portfolio's first day, treated as the start of a month and a week so
    it is built at once. Same rules as risk.simulate."""
    month, week = (True, True) if first else period_starts(days, today)
    if policy == "rebalance":
        return (Decision(target.copy(), "first trading day of the month", scale) if month
                else Decision(None, "not a rebalance day", scale))
    if policy == "trend":
        if not month:
            return Decision(None, "not a rebalance day", scale)
        if len(closes) <= TREND_WINDOW:                    # the backtest checks from its 201st day on
            return Decision(target.copy(), f"not yet {TREND_WINDOW} closes: holding the target weights", scale)
        sma = closes[-TREND_WINDOW:].mean(axis=0)
        up = closes[-1] > sma
        return Decision(np.where(up, target, 0.0), "monthly trend check: " + ", ".join(
            f"{'above' if u else 'below'} its 200-day average" for u in up), scale)
    if policy == "vol_target":
        if not (week or month):
            return Decision(None, "not an adjustment day", scale)
        rets = closes[1:] / closes[:-1] - 1.0
        if week and len(rets) >= VOL_WINDOW:               # the exposure changes only at a week's start
            # the portfolio's recent returns at today's mix; the backtest uses each past day's mix, which
            # differs by at most a month's drift
            mix = held / held.sum() if held.sum() > 0 else target
            vol = float(np.std(rets[-VOL_WINDOW:] @ mix, ddof=1) * math.sqrt(252))
            scale = min(MAX_EXPOSURE, VOL_TARGET / vol) if vol > 0 else MAX_EXPOSURE
        base = target if month or held.sum() <= 0 else held / held.sum()
        why = ("monthly rebalance, " if month else "weekly adjustment, ") + f"exposure {scale:.0%}"
        return Decision(base * scale, why, scale)
    raise ValueError(f"unknown policy {policy!r}")


def plan(want: Floats, symbols: list[str], prices: Floats, shares: Floats, capital: float,
         min_trade: float) -> list[tuple[str, int]]:
    """Orders (symbol, signed shares) that move the holdings to `want`; trades smaller than min_trade x capital
    are skipped. Sells first, so buys never need more cash than is freed."""
    goal = np.floor(want * capital / prices).astype(int)
    out = []
    for s, g, h, px in zip(symbols, goal, shares, prices, strict=True):
        d = int(g - h)
        if d and abs(d) * px >= min_trade * capital:
            out.append((s, d))
    return sorted(out, key=lambda o: o[1])


def cost_bp(side: int, price: float | None, ref: float | None) -> float | None:
    """What a fill at `price` cost against `ref`, in basis points; positive is worse for us (a buy above it, a
    sell below it). None when either price is missing."""
    return round(side * (price - ref) / ref * 1e4, 2) if price and ref else None
