"""A fake of ib_async's IB object that copies the real library's behaviour, quirks included.

The contract tests run the real IBAdapter against it. Every quirk here was observed against a paper Gateway
(ib_async 2.1.0); add one whenever the real library surprises us, with a test that pins it down.

Observed quirks:
  * qualifyContracts() on an unknown symbol returns [None]: a non-empty list.
  * Executions carry the order's orderRef; their commission report may still be unset (a huge number).
  * accountSummary() lists every tag; NetLiquidation is one of them, in USD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any


@dataclass
class _AccountValue:
    account: str
    tag: str
    value: str
    currency: str


@dataclass
class _Position:
    account: str
    contract: Any
    position: float
    avgCost: float


@dataclass
class _BarData:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class _Details:
    liquidHours: str
    industry: str = ""


@dataclass
class _Execution:
    orderRef: str
    side: str
    shares: float
    price: float


@dataclass
class _Commission:
    commission: float


@dataclass
class _Contract:
    symbol: str
    conId: int


@dataclass
class _Fill:
    contract: _Contract
    execution: _Execution
    commissionReport: _Commission | None
    time: datetime


@dataclass
class FakeIB:
    """Known symbols get stable contract ids; anything else is unknown, as IB would answer it."""

    accounts: list[str] = field(default_factory=lambda: ["DU1234567"])
    known: dict[str, int] = field(default_factory=lambda: {"SPY": 756733, "TLT": 15547841, "BRK B": 72063691,
                                                           "AAPL": 265598})
    held: dict[str, float] = field(default_factory=dict)
    cash: float = 1_000_000.0
    closed_days: set[date] = field(default_factory=set)
    fills: list[tuple[str, str, str, float, float, float | None]] = field(default_factory=list)
    connected: bool = False
    connect_args: dict[str, Any] = field(default_factory=dict)
    orders: list[tuple[str, Any]] = field(default_factory=list)   # (symbol, order) for every order placed

    def connect(self, host: str, port: int, clientId: int, readonly: bool = False, timeout: float = 4) -> None:
        self.connected = True
        self.connect_args = {"host": host, "port": port, "clientId": clientId, "readonly": readonly}

    def isConnected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def managedAccounts(self) -> list[str]:
        return list(self.accounts)

    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        out: list[Any] = []
        for c in contracts:
            if c.symbol in self.known:
                c.conId = self.known[c.symbol]
                out.append(c)
            else:
                out.append(None)           # the real library's answer for an unknown contract
        return out

    def positions(self, account: str = "") -> list[_Position]:
        return [_Position(self.accounts[0], _Contract(s, self.known[s]), q, 0.0) for s, q in self.held.items()]

    def accountSummary(self, account: str = "") -> list[_AccountValue]:
        return [_AccountValue(self.accounts[0], "AccountType", "INDIVIDUAL", ""),
                _AccountValue(self.accounts[0], "NetLiquidation", f"{self.cash:.2f}", "USD")]

    def reqHistoricalData(self, contract: Any, endDateTime: str, durationStr: str, barSizeSetting: str,
                          whatToShow: str, useRTH: bool, formatDate: int, keepUpToDate: bool) -> list[_BarData]:
        n, unit = durationStr.split()
        span = int(n) * (365 if unit == "Y" else 1)
        end = date(2026, 10, 9)
        days = [end - timedelta(days=i) for i in range(span)][::-1]
        days = [d for d in days if d.weekday() < 5 and d not in self.closed_days]
        return [_BarData(d, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 1e6) for i, d in enumerate(days)]

    def reqContractDetails(self, contract: Any) -> list[_Details]:
        parts = []
        for i in range(-3, 4):
            d = date(2026, 10, 12) + timedelta(days=i)
            parts.append(f"{d:%Y%m%d}:CLOSED" if d in self.closed_days or d.weekday() >= 5
                         else f"{d:%Y%m%d}:0930-{d:%Y%m%d}:1600")
        return [_Details(";".join(parts), "Technology" if contract.symbol == "AAPL" else "")]

    def placeOrder(self, contract: Any, order: Any) -> None:
        self.orders.append((contract.symbol, order))

    def sleep(self, seconds: float = 0) -> None:
        pass

    def reqExecutions(self, execFilter: Any = None) -> list[_Fill]:
        return [_Fill(_Contract(sym, self.known.get(sym, 0)), _Execution(ref, side, qty, px),
                      None if comm is None else _Commission(comm), datetime(2026, 10, 15, 9, 30))
                for sym, ref, side, qty, px, comm in self.fills]
