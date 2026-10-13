"""Contract tests for the broker adapter.

The same contract runs against the fake (always) and against a running paper Gateway or TWS (only with
`pytest -m paper`, read-only: no order is ever sent). Fake-only tests cover what cannot be staged on a real
paper account: a live account, several accounts, holidays, executions.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import date

import pytest

from investtree.execution.broker import BrokerError, IBAdapter
from investtree.execution.fake_ib import FakeIB


def fake(**kw: object) -> IBAdapter:
    ib = FakeIB(**kw)  # type: ignore[arg-type]
    return IBAdapter(4002, 70, ib_factory=lambda: ib)


@pytest.fixture(params=["fake", pytest.param("paper", marks=pytest.mark.paper)])
def broker(request: pytest.FixtureRequest) -> Iterator[IBAdapter]:
    if request.param == "fake":
        b = fake()
    else:
        from investtree.execution.broker import PAPER_PORTS

        b = next((IBAdapter(p, 71) for p in PAPER_PORTS if _listening(p)), None)  # type: ignore[assignment]
        if b is None:
            pytest.skip("no Gateway or TWS listening on a paper port")
    b.connect()
    yield b
    b.disconnect()


def _listening(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ------------------------------------------------------------------ the contract
def test_connects_to_one_paper_account(broker: IBAdapter) -> None:
    assert broker.is_paper()
    assert broker.account_id().upper().startswith("D")


def test_a_known_symbol_qualifies_and_an_unknown_one_does_not(broker: IBAdapter) -> None:
    assert broker.qualify("SPY")
    assert not broker.qualify("XTO")          # delisted: ib_async answers [None], which is not success


def test_daily_bars_are_ordered_completed_and_limited_to_what_was_asked(broker: IBAdapter) -> None:
    bars = broker.daily_bars("SPY", 10)
    assert len(bars) == 10
    assert all(a.day < b.day for a, b in itertools.pairwise(bars))
    assert all(b.low <= min(b.open, b.close) and b.high >= max(b.open, b.close) for b in bars)


def test_positions_cover_every_symbol_asked_for(broker: IBAdapter) -> None:
    pos = broker.positions(["SPY", "TLT"])
    assert set(pos) == {"SPY", "TLT"} and all(isinstance(q, int) for q in pos.values())


def test_net_liquidation_is_positive(broker: IBAdapter) -> None:
    assert broker.net_liquidation() > 0


def test_asking_about_an_unknown_symbol_raises(broker: IBAdapter) -> None:
    with pytest.raises(BrokerError, match="no contract"):
        broker.daily_bars("XTO", 5)


# ------------------------------------------------------------------ fake only: safety and quirks
def test_a_live_port_is_refused_before_connecting() -> None:
    ib = FakeIB()
    with pytest.raises(BrokerError, match="LIVE trading port"):
        IBAdapter(4001, 70, ib_factory=lambda: ib).connect()
    assert not ib.connected


def test_a_live_account_or_several_accounts_are_refused() -> None:
    with pytest.raises(BrokerError, match="LIVE account"):
        fake(accounts=["U7654321"]).connect()
    with pytest.raises(BrokerError, match="refusing to guess"):
        fake(accounts=["DU1", "DU2"]).connect()


def test_connections_are_read_only_by_default() -> None:
    ib = FakeIB()
    IBAdapter(4002, 70, ib_factory=lambda: ib).connect()
    assert ib.connect_args["readonly"] is True


def test_dual_class_tickers_use_ibs_spelling() -> None:
    b = fake()
    b.connect()
    assert b.qualify("BRK.B")


def test_holidays_come_from_ibs_calendar() -> None:
    b = fake(closed_days={date(2026, 10, 12)})
    b.connect()
    assert not b.is_trading_day(date(2026, 10, 12))
    assert b.is_trading_day(date(2026, 10, 13))
    assert not b.is_trading_day(date(2026, 10, 11))           # a Sunday


def test_executions_are_filtered_by_tag_signed_and_unset_commissions_are_none() -> None:
    b = fake(fills=[("SPY", "investtree-overlay", "BOT", 3, 780.1, 1.0),
                    ("SPY", "someone-else", "BOT", 5, 780.0, 1.0),
                    ("TLT", "investtree-overlay", "SLD", 2, 90.5, None),
                    ("SPY", "investtree-overlay", "BOT", 1, 780.2, 1.7976931348623157e308)])
    b.connect()
    b.qualify("SPY")
    b.qualify("TLT")
    fills = b.executions_today("investtree-overlay")
    assert [(f.symbol, f.shares, f.commission) for f in fills] == [("SPY", 3, 1.0), ("TLT", -2, None), ("SPY", 1, None)]


def test_orders_are_refused_on_a_read_only_session() -> None:
    b = fake()
    b.connect()
    with pytest.raises(BrokerError, match="read-only"):
        b.place_market_on_open("SPY", 10, "investtree-overlay")


def test_a_paper_order_is_market_on_open_tagged_and_signed() -> None:
    ib = FakeIB()
    b = IBAdapter(4002, 70, readonly=False, ib_factory=lambda: ib)
    b.connect()
    b.place_market_on_open("SPY", -7, "investtree-overlay")
    [(sym, order)] = ib.orders
    assert (sym, order.action, order.totalQuantity, order.tif, order.orderRef) == (
        "SPY", "SELL", 7, "OPG", "investtree-overlay")


def test_a_live_order_needs_the_typed_confirmation() -> None:
    ib = FakeIB(accounts=["U7654321"])
    b = IBAdapter(4001, 70, allow_live=True, readonly=False, ib_factory=lambda: ib)
    b.connect()
    with pytest.raises(BrokerError, match="typed confirmation"):
        b.place_market_on_open("SPY", 1, "test")
    assert not ib.orders


def test_the_opening_price_is_none_before_the_session_opens() -> None:
    b = fake()
    b.connect()
    assert b.opening_price("SPY", date(2026, 10, 9)) is not None     # the fake's last session
    assert b.opening_price("SPY", date(2026, 10, 12)) is None


def test_industry_is_empty_for_a_fund() -> None:
    b = fake()
    b.connect()
    assert b.industry("AAPL") == "Technology"
    assert b.industry("SPY") == ""
