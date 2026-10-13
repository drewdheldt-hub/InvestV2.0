"""Suggested trades for real accounts: the overlay proposes, the owner places the order.

Nothing here can send an order; it has no broker. For each account in config/accounts.local.yaml with an
overlay, on each weekday: the same decision as the paper runner (portfolio/overlay.py), sized against what
the latest Flex statement says the account holds. For a taxable account each suggested sale also names the
lots to sell (least tax first), the estimated tax, and any wash-sale conflict; each suggested purchase is
checked against recent loss sales. Taxable accounts also get tax-loss harvesting candidates.

The overlay manages the account's cash and the positions in its weights; anything else in the account is
left alone and listed as outside the overlay. The volatility target's exposure is carried from one run to
the next in var/suggest/<account>.json.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from ..config import AccountsConfig, RealAccount
from ..data import bars
from . import overlay, tax
from .holdings import Holdings
from .risk import MAX_EXPOSURE

MAX_STATEMENT_AGE_DAYS = 4         # older holdings would size trades on shares the account may no longer hold
MAX_CLOSE_AGE_DAYS = 5


@dataclass
class Trade:
    symbol: str
    shares: int                    # signed: positive buy, negative sell
    price: float                   # the close it was sized on
    lots: list[tax.LotSale] = field(default_factory=list)
    est_tax: float | None = None   # taxable sales only
    wash: tax.WashRisk | None = None


@dataclass
class AccountSuggestion:
    account: RealAccount
    why: str
    trades: list[Trade] = field(default_factory=list)
    outside: list[str] = field(default_factory=list)          # positions the overlay does not manage
    harvest: list[tax.Harvest] = field(default_factory=list)
    refused: str | None = None                                 # why nothing could be suggested
    decision_day: bool = False                                 # the overlay wanted weights today


def _state(folder: Path, account_id: str) -> tuple[Path, dict[str, Any]]:
    p = folder / f"{account_id}.json"
    state: dict[str, Any] = {"last_day": None, "scale": MAX_EXPOSURE}
    if p.exists():
        state.update(json.loads(p.read_text(encoding="utf-8")))
    return p, state


def _save(p: Path, state: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def for_account(acct: RealAccount, h: Holdings, cfg: AccountsConfig, data: Path, state_dir: Path,
                today: date) -> AccountSuggestion:
    rates = tax.Rates(cfg.tax.short_term_rate, cfg.tax.long_term_rate)
    out = AccountSuggestion(acct, "")
    if acct.taxable:
        lots = [lot for lot in h.lots if lot.account_id == acct.id]
        out.harvest = tax.harvest_candidates(lots, h.trades, today, rates, cfg.tax.harvest_min_loss,
                                             cfg.tax.harvest_min_pct, cfg.tax.harvest_pairs)
    if acct.overlay is None:
        out.why = "no overlay configured: risk report and harvesting candidates only"
        return out
    as_of = h.account_as_of.get(acct.id)
    if acct.id not in h.accounts:
        out.refused = "this account is not in any saved Flex statement"
        return out
    if as_of is None or (today - as_of).days > MAX_STATEMENT_AGE_DAYS:
        out.refused = f"the latest statement is from {as_of}; fetch a fresh one (investtree holdings fetch)"
        return out

    ov = acct.overlay
    symbols = list(ov.weights)
    target = np.array([ov.weights[s] for s in symbols])
    try:
        days, closes = bars.closes(data, symbols, before=today)
    except FileNotFoundError as e:
        out.refused = str(e)
        return out
    if len(days) < 2 or (today - days[-1]).days > MAX_CLOSE_AGE_DAYS:
        out.refused = f"no recent closes for {', '.join(symbols)} (latest {days[-1] if days else 'none'})"
        return out
    days, closes = days[-320:], closes[-320:]
    prices = closes[-1]

    positions = h.for_account(acct.id)
    held_sh = {s: 0.0 for s in symbols}
    outside_value = 0.0
    for p in positions:
        if p.symbol in held_sh:
            held_sh[p.symbol] += p.quantity
        else:
            outside_value += p.value
            out.outside.append(p.symbol)
    shares = np.array([held_sh[s] for s in symbols])
    capital = float(shares @ prices) + h.cash.get(acct.id, 0.0)
    if capital <= 0:
        out.refused = "the overlay's positions and cash add up to nothing"
        return out

    path, state = _state(state_dir, acct.id)
    first = state.get("last_day") is None
    d = overlay.decide(ov.policy, days, closes, target, today, shares * prices / capital,
                       state.get("scale", MAX_EXPOSURE), first=first)
    if state.get("last_day") != today.isoformat():
        state.update(last_day=today.isoformat(), scale=d.scale)
        _save(path, state)
    out.why = ("first run: " if first else "") + d.why
    if d.want is None:
        return out
    out.decision_day = True

    taxable = cfg.taxable_ids
    for sym, qty in overlay.plan(d.want, symbols, prices, shares, capital, ov.min_trade):
        px = float(prices[symbols.index(sym)])
        t = Trade(sym, qty, px)
        if qty < 0 and acct.taxable:
            lots = h.lots_of(acct.id, sym)
            try:
                t.lots = tax.choose_lots(lots, -qty, px, today, rates)
                t.est_tax = sum(s.tax for s in t.lots)
            except ValueError:
                t.lots = []                   # the statement has no lot detail for it; the report says so
            if any(s.gain < 0 for s in t.lots):
                t.wash = tax.wash_risk_of_sale(h.trades, sym, today)
        elif qty > 0:
            t.wash = tax.wash_risk_of_buy(h.trades, sym, today, taxable)
        out.trades.append(t)
    return out


def build(h: Holdings, cfg: AccountsConfig, data: Path, state_dir: Path, today: date) -> list[AccountSuggestion]:
    return [for_account(a, h, cfg, data, state_dir, today) for a in cfg.accounts]


def render(sugs: list[AccountSuggestion], h: Holdings, today: date) -> str:
    lines = [f"# Suggested trades, {today}", "",
             "**Suggestions only. Nothing has been sent.** Place any order yourself, after checking it. Shares are "
             f"sized on the last close and on holdings as of {h.as_of or 'unknown'}. Tax figures are estimates at "
             "your configured rates, not tax advice.", ""]
    if not sugs:
        lines += ["No accounts are configured in config/accounts.local.yaml.", ""]
    for s in sugs:
        a = s.account
        lines += [f"## {a.name or a.id} ({'taxable' if a.taxable else 'tax-deferred'})", ""]
        if s.refused:
            lines += [f"Nothing suggested: {s.refused}.", ""]
        else:
            lines += [f"Today: {s.why}.", ""]
            if s.trades:
                lines += ["| Trade | Shares | At about | Estimated tax | Note |", "|---|---:|---:|---:|---|"]
                for t in s.trades:
                    side = "Buy" if t.shares > 0 else "Sell"
                    est = "" if t.est_tax is None else f"${t.est_tax:,.0f}"
                    note = t.wash.why if t.wash else ""
                    if t.shares < 0 and a.taxable and not t.lots:
                        note = (note + "; " if note else "") + "no lot detail in the statement: choose lots yourself"
                    lines.append(f"| {side} {t.symbol} | {abs(t.shares):,} | ${t.price:,.2f} | {est} | {note} |")
                for t in s.trades:
                    if t.lots:
                        lines.append(f"\nLots to sell for {t.symbol} (least tax first):")
                        lines += [f"- {ls.shares:,.4g} shares bought {ls.lot.opened} at ${ls.lot.cost_per_share:,.2f}: "
                                  f"{'gain' if ls.gain >= 0 else 'loss'} ${abs(ls.gain):,.0f}, "
                                  f"{'long' if ls.is_long else 'short'}-term" for ls in t.lots]
                lines.append("")
            elif s.decision_day:
                lines += ["No trade needed: the account is already within the minimum trade size of the target.", ""]
        if s.outside:
            lines += [f"Outside the overlay (left alone): {', '.join(sorted(set(s.outside)))}.", ""]
        if s.harvest:
            lines += ["Tax-loss harvesting candidates (do not buy the same security back within 30 days, in any "
                      "account):", ""]
            for hv in s.harvest[:10]:
                rep = f"; your replacement: {hv.replacement}" if hv.replacement else ""
                block = f"; **blocked**: {hv.blocked.why}" if hv.blocked else ""
                lines.append(f"- {hv.lot.symbol}: {hv.lot.quantity:,.4g} shares bought {hv.lot.opened}, loss "
                             f"${hv.loss:,.0f} ({hv.loss_pct:.0%}), {'long' if hv.is_long else 'short'}-term, "
                             f"saves about ${hv.tax_saved:,.0f}{rep}{block}")
            lines.append("")
    return "\n".join(lines)


def write(reports: Path, text: str, today: date) -> Path:
    p = reports / "suggestions" / f"{today}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p
