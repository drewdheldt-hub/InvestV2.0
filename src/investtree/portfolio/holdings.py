"""Real holdings, read-only, from Interactive Brokers Flex statements.

A Flex statement is IB's own XML report of an account: open positions (optionally one row per tax lot),
trades, and account details. It is read-only by nature: nothing in it can place an order. Statements are
saved under data/holdings/flex/ (never in git), by `investtree holdings fetch` through IB's Flex Web
Service or by hand from Client Portal (Performance & Reports > Flex Queries).

What the Flex query needs (Client Portal > Flex Queries > Activity Flex Query):
  * Account Information: all fields
  * Open Positions: options "Summary" and "Lot", all fields
  * Trades: options "Execution" and "Closed Lots", all fields
  * Format XML; Period "Last 365 Calendar Days" (the wash-sale check needs the last 30 days at least)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path


class FlexError(ValueError):
    """A file that is not a usable Flex statement."""


@dataclass(frozen=True)
class Account:
    account_id: str
    alias: str
    account_type: str            # IB's description, e.g. "Individual", "IRA"
    currency: str


@dataclass(frozen=True)
class Position:
    account_id: str
    symbol: str
    conid: str
    asset_class: str             # IB's assetCategory: STK, OPT, FUT, BOND, CASH, ...
    description: str
    quantity: float
    price: float                 # IB's mark price at the statement date
    value: float                 # position value in the position's currency
    cost_basis: float            # total, in the position's currency
    currency: str

    @property
    def unrealized(self) -> float:
        return self.value - self.cost_basis


@dataclass(frozen=True)
class Lot:
    """One tax lot of an open position: when it was bought, how many shares remain, what they cost."""

    account_id: str
    symbol: str
    conid: str
    quantity: float
    opened: date
    cost_basis: float            # total for the remaining shares
    price: float                 # mark price at the statement date

    @property
    def cost_per_share(self) -> float:
        return self.cost_basis / self.quantity if self.quantity else 0.0

    @property
    def unrealized(self) -> float:
        return self.quantity * self.price - self.cost_basis


@dataclass(frozen=True)
class Trade:
    account_id: str
    symbol: str
    conid: str
    day: date
    quantity: float              # signed: positive bought, negative sold
    price: float
    realized: float              # FIFO realised profit or loss of a closing trade; 0 for an opening one


@dataclass
class Holdings:
    """Everything one or more Flex statements say, merged. as_of: the latest statement date."""

    as_of: date | None = None
    accounts: dict[str, Account] = field(default_factory=dict)
    account_as_of: dict[str, date] = field(default_factory=dict)  # each account's own statement date
    positions: list[Position] = field(default_factory=list)
    lots: list[Lot] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    cash: dict[str, float] = field(default_factory=dict)          # account -> cash in base currency
    sources: list[str] = field(default_factory=list)

    def for_account(self, account_id: str) -> list[Position]:
        return [p for p in self.positions if p.account_id == account_id]

    def lots_of(self, account_id: str, symbol: str) -> list[Lot]:
        return [lot for lot in self.lots if lot.account_id == account_id and lot.symbol == symbol]


# ------------------------------------------------------------------ parsing
def _num(x: str | None) -> float:
    if x is None or x.strip() in ("", "--", "N/A"):
        return 0.0
    return float(x.replace(",", ""))


def _day(x: str | None) -> date | None:
    """Flex dates come as 20261012, 2026-10-12, or with a time after ';' or ' ' (20261012;093000)."""
    if not x or not x.strip():
        return None
    s = x.strip().replace(";", " ").split(" ")[0]
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise FlexError(f"unrecognised Flex date {x!r}")


def parse(text: str, source: str = "") -> Holdings:
    """One Flex statement file (which may hold several accounts)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise FlexError(f"{source or 'statement'} is not XML: {e}") from e
    statements = root.findall(".//FlexStatement")
    if root.tag != "FlexQueryResponse" or not statements:
        raise FlexError(f"{source or 'statement'} is not a Flex statement (no FlexStatement element)")
    h = Holdings(sources=[source] if source else [])
    for st in statements:
        acct = st.get("accountId", "")
        when = _day(st.get("toDate")) or _day(st.get("whenGenerated"))
        if when and (h.as_of is None or when > h.as_of):
            h.as_of = when
        if when:
            h.account_as_of[acct] = when
        info = st.find("AccountInformation")
        if info is not None:
            h.accounts[acct] = Account(acct, info.get("acctAlias", ""), info.get("accountType", ""),
                                       info.get("currency", "USD"))
        else:
            h.accounts.setdefault(acct, Account(acct, "", "", "USD"))
        for p in st.iterfind("OpenPositions/OpenPosition"):
            level = p.get("levelOfDetail", "SUMMARY").upper()
            side = -1.0 if p.get("side", "Long").lower() == "short" else 1.0
            qty = abs(_num(p.get("position") or p.get("quantity"))) * side
            if level == "SUMMARY":
                h.positions.append(Position(
                    account_id=p.get("accountId", acct), symbol=p.get("symbol", ""), conid=p.get("conid", ""),
                    asset_class=p.get("assetCategory", ""), description=p.get("description", ""),
                    quantity=qty, price=_num(p.get("markPrice")), value=_num(p.get("positionValue")),
                    cost_basis=_num(p.get("costBasisMoney")), currency=p.get("currency", "USD")))
            elif level == "LOT":
                opened = _day(p.get("openDateTime")) or _day(p.get("holdingPeriodDateTime"))
                if opened is None:
                    raise FlexError(f"a lot of {p.get('symbol')} has no open date")
                h.lots.append(Lot(account_id=p.get("accountId", acct), symbol=p.get("symbol", ""),
                                  conid=p.get("conid", ""), quantity=qty, opened=opened,
                                  cost_basis=_num(p.get("costBasisMoney")), price=_num(p.get("markPrice"))))
        for t in st.iterfind("Trades/Trade"):
            if t.get("levelOfDetail", "EXECUTION").upper() != "EXECUTION":
                continue                          # closed-lot rows repeat the execution, per lot
            day = _day(t.get("tradeDate"))
            if day is None:
                continue
            qty = _num(t.get("quantity"))
            if t.get("buySell", "").upper().startswith("SELL") and qty > 0:
                qty = -qty
            h.trades.append(Trade(account_id=t.get("accountId", acct), symbol=t.get("symbol", ""),
                                  conid=t.get("conid", ""), day=day, quantity=qty,
                                  price=_num(t.get("tradePrice")), realized=_num(t.get("fifoPnlRealized"))))
        for c in st.iterfind("CashReport/CashReportCurrency"):
            if c.get("currency") == "BASE_SUMMARY":
                h.cash[acct] = _num(c.get("endingCash"))
    return h


def merge(parts: Iterable[Holdings]) -> Holdings:
    """Several statements into one. For each account, positions and lots come from its latest statement;
    trades from all of them, without duplicates."""
    out = Holdings()
    latest: dict[str, tuple[date, Holdings]] = {}
    seen: set[Trade] = set()
    for h in parts:
        out.sources.extend(h.sources)
        if h.as_of and (out.as_of is None or h.as_of > out.as_of):
            out.as_of = h.as_of
        for acct in h.accounts:
            when = h.account_as_of.get(acct, date.min)
            if acct not in latest or when >= latest[acct][0]:      # on a tie, the later statement wins
                latest[acct] = (when, h)
        for t in h.trades:
            if t not in seen:
                seen.add(t)
                out.trades.append(t)
    for acct, (_, h) in latest.items():
        out.accounts[acct] = h.accounts[acct]
        if acct in h.account_as_of:
            out.account_as_of[acct] = h.account_as_of[acct]
        out.positions.extend(p for p in h.positions if p.account_id == acct)
        out.lots.extend(lot for lot in h.lots if lot.account_id == acct)
        if acct in h.cash:
            out.cash[acct] = h.cash[acct]
    out.trades.sort(key=lambda t: (t.day, t.account_id, t.symbol))
    return out


def flex_folder(data: Path) -> Path:
    return data / "holdings" / "flex"


def load(data: Path) -> Holdings:
    """Every saved Flex statement, merged (latest positions per account, all trades)."""
    files = sorted(flex_folder(data).glob("*.xml"))
    if not files:
        raise FileNotFoundError(f"no Flex statements in {flex_folder(data)}; run: investtree holdings fetch")
    return merge(parse(f.read_text(encoding="utf-8"), f.name) for f in files)
