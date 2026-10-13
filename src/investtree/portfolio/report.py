"""The risk report for real holdings: what is held, how concentrated it is, how much it can move, and what the
overlays say today. Read-only; written to reports/risk/<date>.md (never in git: it names real accounts).

Risk of the current mix uses today's weights held constant over the stored daily history (tier 1 bars), so
it answers "how would what I hold now have behaved", not "how did my account do". Positions without stored
bars (options, bonds, anything not yet fetched) are listed as not covered rather than silently dropped.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

from ..config import AccountsConfig
from ..data import bars
from .holdings import Holdings, Position
from .risk import MAX_EXPOSURE, TREND_WINDOW, VOL_TARGET, VOL_WINDOW


@dataclass(frozen=True)
class MixRisk:
    """How today's mix would have behaved, held constant. Weights are of the covered value (cash excluded)."""

    days: int                          # common history used
    since: date | None
    vol_1y: float | None               # annualised, last 252 days
    vol_20d: float | None              # annualised, last 20 days: what the volatility target looks at
    worst_drawdown: float | None       # over the whole common history
    worst_month: float | None
    var_95_1d: float | None            # 1-day loss exceeded on 5% of days (historical, last 2 years), a fraction
    beta_spy: float | None             # against SPY over the last 2 years, when SPY is stored


@dataclass
class AccountRisk:
    label: str                         # account name, or "All accounts"
    account_ids: list[str]
    value: float                       # positions plus cash
    cash: float
    positions: list[tuple[Position, float]]      # (position, weight of value), largest first
    effective_holdings: float          # 1 / sum of squared weights of the positions
    by_asset_class: dict[str, float]
    by_industry: dict[str, float]
    not_covered: list[str]             # symbols with no stored bars
    mix: MixRisk | None
    overlay: list[str] = field(default_factory=list)   # what the account's overlay says today, in words


def _mix_risk(data: Path, weights: dict[str, float]) -> MixRisk | None:
    if not weights:
        return None
    symbols = list(weights)
    days, closes = bars.closes(data, symbols)
    if len(days) < 30:
        return None
    w = np.array([weights[s] for s in symbols])
    w = w / w.sum()
    rets = (closes[1:] / closes[:-1] - 1.0) @ w
    eq = np.cumprod(1.0 + rets)
    peak = np.maximum.accumulate(np.r_[1.0, eq])[1:]
    months = np.array(days[1:], dtype="datetime64[M]")
    starts = np.r_[0, np.flatnonzero(months[1:] != months[:-1]) + 1]
    monthly = np.multiply.reduceat(1.0 + rets, starts) - 1.0

    def vol(n: int) -> float | None:
        return float(np.std(rets[-n:], ddof=1) * math.sqrt(252)) if len(rets) >= n else None

    recent = rets[-504:]
    beta = None
    if "SPY" not in symbols and bars.path(data, "SPY").exists():
        _, sc = bars.closes(data, ["SPY", *symbols])
        sr = sc[1:] / sc[:-1] - 1.0
        port, mkt = (sr[:, 1:] @ w)[-504:], sr[-504:, 0]
        if len(mkt) > 60 and np.var(mkt) > 0:
            beta = float(np.cov(port, mkt)[0, 1] / np.var(mkt, ddof=1))
    elif symbols == ["SPY"]:
        beta = 1.0
    return MixRisk(days=len(rets), since=days[0], vol_1y=vol(252), vol_20d=vol(VOL_WINDOW),
                   worst_drawdown=float(np.max(1.0 - eq / peak)), worst_month=float(monthly.min()),
                   var_95_1d=float(-np.percentile(recent, 5)) if len(recent) >= 100 else None, beta_spy=beta)


def _overlay_words(data: Path, weights: dict[str, float], policy: str, mix: MixRisk | None) -> list[str]:
    out = []
    if policy == "vol_target" and mix is not None and mix.vol_20d:
        scale = min(MAX_EXPOSURE, VOL_TARGET / mix.vol_20d)
        out.append(f"volatility target: the last 20 days ran at {mix.vol_20d:.1%} a year, so the overlay would hold "
                   f"{scale:.0%} invested")
    if policy == "trend":
        for s in weights:
            try:
                _, c = bars.closes(data, [s])
            except FileNotFoundError:
                out.append(f"trend filter: {s} has no stored bars")
                continue
            if len(c) <= TREND_WINDOW:
                out.append(f"trend filter: {s} has fewer than {TREND_WINDOW} closes")
                continue
            above = c[-1, 0] > c[-TREND_WINDOW:, 0].mean()
            out.append(f"trend filter: {s} is {'above' if above else 'below'} its 200-day average "
                       f"({'hold' if above else 'cash'} at the next monthly check)")
    if policy == "rebalance":
        out.append("monthly rebalance: back to the target weights on the first trading day of each month")
    return out


def account_risk(label: str, ids: list[str], h: Holdings, data: Path, industries: dict[str, str],
                 overlay: tuple[dict[str, float], str] | None = None) -> AccountRisk:
    positions = [p for p in h.positions if p.account_id in ids]
    cash = sum(h.cash.get(a, 0.0) for a in ids)
    value = sum(p.value for p in positions) + cash
    weighted = sorted(((p, p.value / value if value else 0.0) for p in positions), key=lambda x: -abs(x[1]))
    pw = np.array([w for _, w in weighted])
    invested = pw.sum()
    effective = float(1.0 / ((pw / invested) ** 2).sum()) if invested > 0 else 0.0
    by_class: dict[str, float] = defaultdict(float)
    by_industry: dict[str, float] = defaultdict(float)
    by_symbol: dict[str, float] = defaultdict(float)
    for p, w in weighted:
        by_class[p.asset_class or "unknown"] += w
        by_industry[industries.get(p.symbol, "unknown")] += w
        by_symbol[p.symbol] += p.value
    if cash:
        by_class["CASH"] += cash / value if value else 0.0
    stored = {s: v for s, v in by_symbol.items() if bars.path(data, s).exists() and v > 0}
    not_covered = sorted(s for s in by_symbol if s not in stored)
    mix = _mix_risk(data, stored)
    words = _overlay_words(data, overlay[0], overlay[1], mix) if overlay else []
    return AccountRisk(label, ids, value, cash, weighted, effective, dict(by_class), dict(by_industry),
                       not_covered, mix, words)


def build(h: Holdings, accounts: AccountsConfig, data: Path, industries: dict[str, str] | None = None,
          ) -> list[AccountRisk]:
    """One section per account in the statements, then all accounts together."""
    industries = industries or {}
    out = []
    for acct_id, acct in sorted(h.accounts.items()):
        cfg = accounts.get(acct_id)
        label = (cfg.name if cfg and cfg.name else acct.alias or acct_id) + (
            "" if cfg is None else " (taxable)" if cfg.taxable else " (tax-deferred)")
        ov = (cfg.overlay.weights, cfg.overlay.policy) if cfg and cfg.overlay else None
        out.append(account_risk(label, [acct_id], h, data, industries, ov))
    if len(h.accounts) > 1:
        out.append(account_risk("All accounts", sorted(h.accounts), h, data, industries))
    return out


# ------------------------------------------------------------------ writing it down
def _pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{digits}%}"


def render(sections: list[AccountRisk], h: Holdings, accounts: AccountsConfig, today: date) -> str:
    lines = [f"# Risk report, {today}", "",
             f"Holdings as of {h.as_of or 'unknown'} from {len(h.sources)} Flex statement(s). Risk figures hold "
             "today's weights constant over the stored daily history; they describe the current mix, not past "
             "account performance.", ""]
    unconfigured = sorted(a for a in h.accounts if accounts.get(a) is None)
    if unconfigured:
        lines += [f"**Not in config/accounts.local.yaml:** {', '.join(unconfigured)}. Taxes are not modelled and "
                  "no trades are suggested for these until they are listed there with `taxable` set.", ""]
    for s in sections:
        lines += [f"## {s.label}", "",
                  f"Value ${s.value:,.0f}, of which cash ${s.cash:,.0f}. {len(s.positions)} positions; "
                  f"effective number of holdings {s.effective_holdings:.1f}.", ""]
        if s.mix:
            m = s.mix
            lines += ["| Measure | Current mix |", "|---|---|",
                      f"| Volatility, last year | {_pct(m.vol_1y)} a year |",
                      f"| Volatility, last 20 days | {_pct(m.vol_20d)} a year |",
                      f"| Worst drawdown since {m.since} | {_pct(m.worst_drawdown)} |",
                      f"| Worst month | {_pct(m.worst_month)} |",
                      f"| 1-day loss exceeded 1 day in 20 | {_pct(m.var_95_1d, 2)}"
                      + (f" (about ${m.var_95_1d * (s.value - s.cash):,.0f})" if m.var_95_1d else "") + " |",
                      f"| Beta to SPY, last 2 years | {'n/a' if m.beta_spy is None else f'{m.beta_spy:.2f}'} |", ""]
        if s.not_covered:
            lines += [f"Not covered by the risk figures (no stored daily bars): {', '.join(s.not_covered)}. "
                      f"Fetch them with `investtree data update {' '.join(s.not_covered)}`.", ""]
        if s.overlay:
            lines += ["Overlay today:", *[f"- {w}" for w in s.overlay], ""]
        lines += ["| Symbol | Quantity | Value | Weight | Unrealised |", "|---|---:|---:|---:|---:|"]
        for p, w in s.positions[:25]:
            lines.append(f"| {p.symbol} | {p.quantity:,.4g} | ${p.value:,.0f} | {w:.1%} | ${p.unrealized:,.0f} |")
        if len(s.positions) > 25:
            lines.append(f"| ({len(s.positions) - 25} more) | | | | |")
        lines += ["", "By asset class: " + ", ".join(f"{k} {v:.1%}" for k, v in
                                                       sorted(s.by_asset_class.items(), key=lambda x: -x[1])),
                  ""]
        if any(k != "unknown" for k in s.by_industry):
            lines += ["By industry: " + ", ".join(f"{k} {v:.1%}" for k, v in
                                                  sorted(s.by_industry.items(), key=lambda x: -x[1])), ""]
    return "\n".join(lines)


def write(reports: Path, text: str, today: date) -> Path:
    p = reports / "risk" / f"{today}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p
