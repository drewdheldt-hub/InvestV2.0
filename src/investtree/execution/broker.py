"""The broker adapter: the only code that talks to the broker library (ib_async today).

Everything above it uses the Broker protocol, so the library can be swapped, and the contract tests
(tests/test_broker.py) pin down how the adapter behaves, both against a fake that copies ib_async's real
quirks and, with `pytest -m paper`, against a running paper Gateway.

Safety, built in:
  * live ports (4001, 7496) and live accounts are refused unless the caller passes allow_live=True;
  * a session that manages more than one account is refused rather than guessing which to use;
  * an unknown symbol is reported as unknown. ib_async 2.x answers it with [None], a non-empty list,
    which v1 once mistook for success;
  * orders need a session opened with readonly=False, and on a live account also the typed confirmation
    LIVE_CONFIRMATION.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

LIVE_PORTS = (4001, 7496)
PAPER_PORTS = (4002, 7497)            # Gateway first, as the scheduler prefers it
IB_SYMBOL = {"BRK.B": "BRK B", "BF.B": "BF B"}   # IB spells dual-class shares with a space
UNSET = 1e9                            # ib_async marks an unset number with a huge value
LIVE_CONFIRMATION = "TRADE LIVE"       # what the owner types to allow an order on a live account


class BrokerError(RuntimeError):
    """Something the caller must not carry on past: a refused account, an unknown contract, no connection."""


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Fill:
    symbol: str
    shares: int                # signed: positive bought, negative sold
    price: float
    commission: float | None   # None until the broker has reported it
    tag: str


class Broker(Protocol):
    """What the rest of the project may ask of a broker. Read-only except place_market_on_open."""

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_paper(self) -> bool: ...
    def account_id(self) -> str: ...
    def qualify(self, symbol: str) -> bool: ...
    def positions(self, symbols: list[str]) -> dict[str, int]: ...
    def net_liquidation(self) -> float: ...
    def daily_bars(self, symbol: str, days: int, adjusted: bool = True) -> list[Bar]: ...
    def executions_today(self, tag: str) -> list[Fill]: ...
    def is_trading_day(self, day: date) -> bool: ...
    def opening_price(self, symbol: str, day: date) -> float | None: ...
    def place_market_on_open(self, symbol: str, shares: int, tag: str) -> None: ...


def _to_date(d: Any) -> date:
    return d.date() if isinstance(d, datetime) else d


class IBAdapter:
    """Broker on Interactive Brokers through ib_async. `ib_factory` makes the IB object (tests pass a fake)."""

    def __init__(self, port: int, client_id: int, *, allow_live: bool = False, readonly: bool = True,
                 live_confirmation: str = "", host: str = "127.0.0.1",
                 ib_factory: Callable[[], Any] | None = None) -> None:
        self.port, self.client_id, self.host = port, client_id, host
        self.allow_live, self.readonly = allow_live, readonly
        self.live_confirmation = live_confirmation
        self._factory = ib_factory
        self.ib: Any = None
        self.accounts: list[str] = []
        self.contracts: dict[str, Any] = {}

    # ------------------------------------------------------------ session
    def connect(self) -> None:
        if self.port in LIVE_PORTS and not self.allow_live:
            raise BrokerError(f"port {self.port} is a LIVE trading port; use {PAPER_PORTS} for paper")
        if self._factory is not None:
            self.ib = self._factory()
        else:
            from ib_async import IB

            self.ib = IB()
        self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=self.readonly, timeout=15)
        self.accounts = list(self.ib.managedAccounts())
        if not self.accounts:
            self.disconnect()
            raise BrokerError("connected, but the session manages no account")
        if len(self.accounts) > 1:
            self.disconnect()
            raise BrokerError(f"the session manages {len(self.accounts)} accounts; refusing to guess which to use")
        if not self.is_paper() and not self.allow_live:
            self.disconnect()
            raise BrokerError(f"account {self.accounts[0]} is a LIVE account; refused without allow_live")

    def disconnect(self) -> None:
        if self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()

    def is_paper(self) -> bool:
        # IB paper account ids start with D (DU..., DF...); live ones with U, F or I.
        return bool(self.accounts) and all(a.upper().startswith("D") for a in self.accounts)

    def account_id(self) -> str:
        if not self.accounts:
            raise BrokerError("not connected")
        return self.accounts[0]

    # ------------------------------------------------------------ contracts
    def qualify(self, symbol: str) -> bool:
        """Look the stock up with IB; False when IB does not know it. A contract counts as known only when
        IB returned it and gave it a contract id."""
        if symbol in self.contracts:
            return True
        from ib_async import Stock

        c = Stock(IB_SYMBOL.get(symbol, symbol), "SMART", "USD")
        got = self.ib.qualifyContracts(c)
        ok = bool(got) and all(x is not None for x in got) and bool(getattr(c, "conId", 0))
        if ok:
            self.contracts[symbol] = c
        return ok

    def _contract(self, symbol: str) -> Any:
        if not self.qualify(symbol):
            raise BrokerError(f"IB has no contract for {symbol}")
        return self.contracts[symbol]

    # ------------------------------------------------------------ account
    def positions(self, symbols: list[str]) -> dict[str, int]:
        ids = {self._contract(s).conId: s for s in symbols}
        out = dict.fromkeys(symbols, 0)
        for p in self.ib.positions(self.account_id()):
            if p.contract.conId in ids:
                out[ids[p.contract.conId]] = int(p.position)
        return out

    def net_liquidation(self) -> float:
        for v in self.ib.accountSummary(self.account_id()):
            if v.tag == "NetLiquidation" and v.currency in ("USD", "BASE", ""):
                return float(v.value)
        raise BrokerError("the account summary has no NetLiquidation value")

    # ------------------------------------------------------------ market data
    def daily_bars(self, symbol: str, days: int, adjusted: bool = True) -> list[Bar]:
        """Completed daily bars, regular trading hours. adjusted: dividends and splits folded into past prices."""
        duration = f"{max(1, math.ceil(days / 240))} Y" if days > 240 else f"{max(days, 1) * 7 // 5 + 5} D"
        rows = self.ib.reqHistoricalData(self._contract(symbol), endDateTime="", durationStr=duration,
                                         barSizeSetting="1 day", whatToShow="ADJUSTED_LAST" if adjusted else "TRADES",
                                         useRTH=True, formatDate=1, keepUpToDate=False)
        bars = [Bar(_to_date(b.date), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
                for b in rows or []]
        return bars[-days:]

    def is_trading_day(self, day: date) -> bool:
        """IB's own calendar: the contract's liquid hours list each day, with CLOSED on holidays."""
        details = self.ib.reqContractDetails(self._contract("SPY"))
        hours = (details[0].liquidHours if details else "") or ""
        key = day.strftime("%Y%m%d")
        for part in hours.split(";"):
            if part.startswith(key):
                return "CLOSED" not in part
        return day.weekday() < 5                      # not listed: assume a normal weekday

    def opening_price(self, symbol: str, day: date) -> float | None:
        """The official opening price of `day` from its daily bar; None before that session has opened, rather
        than an earlier day's open."""
        rows = self.ib.reqHistoricalData(self._contract(symbol), endDateTime="", durationStr="2 D",
                                         barSizeSetting="1 day", whatToShow="TRADES", useRTH=True, formatDate=1,
                                         keepUpToDate=False)
        bar = next((b for b in reversed(rows or []) if _to_date(b.date) == day), None)
        return float(bar.open) if bar is not None else None

    def industry(self, symbol: str) -> str:
        """IB's industry for a stock ("Technology", ...); an ETF's or an unknown one's is ""."""
        details = self.ib.reqContractDetails(self._contract(symbol))
        return str(getattr(details[0], "industry", "") or "") if details else ""

    # ------------------------------------------------------------ orders
    def place_market_on_open(self, symbol: str, shares: int, tag: str) -> None:
        """Send a market-on-open order (signed shares: positive buys) tagged `tag`. Refused on a read-only
        session, and on a live account unless the session was opened with the typed live confirmation."""
        if self.readonly:
            raise BrokerError("this session is read-only; orders need readonly=False")
        if not self.is_paper() and self.live_confirmation != LIVE_CONFIRMATION:
            raise BrokerError(f"account {self.account_id()} is LIVE; an order needs the typed confirmation")
        if shares == 0:
            return
        from ib_async import MarketOrder

        order = MarketOrder("BUY" if shares > 0 else "SELL", abs(int(shares)))
        order.orderRef, order.account, order.tif = tag, self.account_id(), "OPG"
        self.ib.placeOrder(self._contract(symbol), order)
        self.ib.sleep(0.5)

    # ------------------------------------------------------------ executions
    def executions_today(self, tag: str) -> list[Fill]:
        """Today's executions of this account's orders carrying `tag`. IB reports a day's executions that day only."""
        from ib_async import ExecutionFilter

        by_id = {c.conId: s for s, c in self.contracts.items()}
        out = []
        for f in self.ib.reqExecutions(ExecutionFilter(acctCode=self.account_id())):
            e = f.execution
            if (e.orderRef or "") != tag:
                continue
            report = f.commissionReport
            commission = float(report.commission) if report is not None and 0 <= report.commission < UNSET else None
            out.append(Fill(symbol=by_id.get(f.contract.conId, f.contract.symbol),
                            shares=int(e.shares) * (1 if e.side == "BOT" else -1), price=float(e.price),
                            commission=commission, tag=tag))
        return out
