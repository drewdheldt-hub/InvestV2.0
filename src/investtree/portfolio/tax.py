"""Taxes for taxable accounts: which lots to sell, wash sales, and tax-loss harvesting candidates.

Scope (the owner's phase 1 decision): taxes are modelled for taxable accounts only. Tax-deferred accounts
(IRAs, 401(k)s) are ignored for tax, except that their purchases still count for the wash-sale rule, which
the IRS applies across all of a person's accounts (Rev. Rul. 2008-5).

These are estimates to inform a decision, not tax advice: rates are the owner's own marginal rates from
config/accounts.local.yaml, and "substantially identical" is left to the owner (only the same symbol is
treated as identical; replacement pairs for harvesting are the owner's choice).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .holdings import Lot, Trade

WASH_WINDOW = timedelta(days=30)


def long_term(opened: date, sold: date) -> bool:
    """Held more than one year: sold after the first anniversary of the purchase."""
    try:
        anniversary = opened.replace(year=opened.year + 1)
    except ValueError:                        # bought on 29 February
        anniversary = date(opened.year + 1, 3, 1)
    return sold > anniversary


@dataclass(frozen=True)
class Rates:
    short_term: float = 0.37                  # ordinary income
    long_term: float = 0.15

    def on(self, gain: float, is_long: bool) -> float:
        """Tax on a gain (negative for a loss: the tax it saves)."""
        return gain * (self.long_term if is_long else self.short_term)


@dataclass(frozen=True)
class LotSale:
    lot: Lot
    shares: float
    gain: float                               # at the sale price; negative is a loss
    is_long: bool
    tax: float                                # estimated; negative means tax saved


def choose_lots(lots: Sequence[Lot], shares: float, price: float, today: date, rates: Rates) -> list[LotSale]:
    """Which lots to sell `shares` from to owe the least tax now: lots whose sale saves the most tax first
    (losses, short-term losses before long-term ones at typical rates), then the cheapest gains, long-term
    before short-term. Raises if the lots do not hold enough shares."""
    def tax_per_share(lot: Lot) -> float:
        return rates.on(price - lot.cost_per_share, long_term(lot.opened, today))

    held = sum(lot.quantity for lot in lots if lot.quantity > 0)
    if shares > held + 1e-9:
        raise ValueError(f"selling {shares:g} shares but the lots hold {held:g}")
    out: list[LotSale] = []
    left = shares
    for lot in sorted((x for x in lots if x.quantity > 0), key=lambda x: (tax_per_share(x), x.opened)):
        if left <= 1e-9:
            break
        n = min(lot.quantity, left)
        gain = n * (price - lot.cost_per_share)
        is_long = long_term(lot.opened, today)
        out.append(LotSale(lot, n, gain, is_long, rates.on(gain, is_long)))
        left -= n
    return out


# ------------------------------------------------------------------ wash sales
@dataclass(frozen=True)
class WashRisk:
    symbol: str
    why: str


def bought_within(trades: Iterable[Trade], symbol: str, day: date) -> list[Trade]:
    """Purchases of `symbol` in any account within 30 days before `day`."""
    return [t for t in trades if t.symbol == symbol and t.quantity > 0 and timedelta(0) <= day - t.day <= WASH_WINDOW]


def loss_sales_within(trades: Iterable[Trade], symbol: str, day: date, taxable: set[str]) -> list[Trade]:
    """Sales of `symbol` at a loss in a taxable account within 30 days before `day`."""
    return [t for t in trades if t.symbol == symbol and t.quantity < 0 and t.realized < 0 and t.account_id in taxable
            and timedelta(0) <= day - t.day <= WASH_WINDOW]


def wash_risk_of_sale(trades: Iterable[Trade], symbol: str, day: date) -> WashRisk | None:
    """Selling `symbol` at a loss today would be a wash sale if it was bought in the last 30 days (any account)
    and it would also be one if it is bought in the next 30 days, which the owner has to avoid."""
    recent = bought_within(trades, symbol, day)
    if recent:
        last = max(t.day for t in recent)
        return WashRisk(symbol, f"bought {last} (within 30 days), so a loss on this sale would be disallowed")
    return None


def wash_risk_of_buy(trades: Iterable[Trade], symbol: str, day: date, taxable: set[str]) -> WashRisk | None:
    """Buying `symbol` today, in any account, makes a loss sale of it in a taxable account in the last 30 days
    a wash sale."""
    recent = loss_sales_within(trades, symbol, day, taxable)
    if recent:
        last = max(t.day for t in recent)
        return WashRisk(symbol, f"sold at a loss on {last}; buying it before {last + WASH_WINDOW + timedelta(days=1)} "
                                "disallows that loss")
    return None


# ------------------------------------------------------------------ harvesting
@dataclass(frozen=True)
class Harvest:
    lot: Lot
    loss: float                               # positive number: the unrealised loss
    loss_pct: float
    is_long: bool
    tax_saved: float                          # estimated at the owner's rates
    replacement: str | None                   # the owner's chosen replacement, if any
    blocked: WashRisk | None                  # a recent purchase that would disallow the loss


def harvest_candidates(lots: Iterable[Lot], trades: Sequence[Trade], today: date, rates: Rates,
                       min_loss: float = 1000.0, min_pct: float = 0.05,
                       pairs: dict[str, str] | None = None) -> list[Harvest]:
    """Lots (already restricted to taxable accounts by the caller) with an unrealised loss of at least
    `min_loss` dollars and `min_pct` of cost, largest tax saving first. Candidates only: the owner decides,
    and must not buy the same security back within 30 days."""
    out = []
    for lot in lots:
        loss = -lot.unrealized
        if lot.quantity <= 0 or lot.cost_basis <= 0 or loss < min_loss or loss / lot.cost_basis < min_pct:
            continue
        is_long = long_term(lot.opened, today)
        out.append(Harvest(lot, loss, loss / lot.cost_basis, is_long, -rates.on(-loss, is_long),
                           (pairs or {}).get(lot.symbol), wash_risk_of_sale(trades, lot.symbol, today)))
    return sorted(out, key=lambda h: -h.tax_saved)
