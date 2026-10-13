"""Suggested trades for real accounts: sized from the statement, tax-aware in taxable accounts, refused on old
data, and never anything but a report."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from investtree.config import AccountsConfig
from investtree.data import bars
from investtree.portfolio import suggest
from investtree.portfolio.holdings import Account, Holdings, Lot, Position, Trade

TODAY = date(2026, 10, 13)          # a Tuesday


class _Bar:
    def __init__(self, day: date, px: float) -> None:
        self.day, self.open, self.high, self.low, self.close, self.volume = day, px, px, px, px, 1e6


def store_flat(data: Path, symbol: str, px: float, last: date = TODAY - timedelta(days=1)) -> None:
    days, d = [], last
    while len(days) < 300:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    bars.store(data, symbol, bars.to_frame([_Bar(x, px) for x in days[::-1]]))


def config(policy: str = "rebalance", taxable: bool = True) -> AccountsConfig:
    return AccountsConfig.model_validate({"accounts": [
        {"id": "U1", "name": "Brokerage", "taxable": taxable,
         "overlay": {"weights": {"VTI": 0.5, "BND": 0.5}, "policy": policy}},
        {"id": "U2", "name": "IRA", "taxable": False}]})


def holdings(as_of: date = TODAY - timedelta(days=1)) -> Holdings:
    return Holdings(
        as_of=as_of,
        accounts={"U1": Account("U1", "", "Individual", "USD"), "U2": Account("U2", "", "IRA", "USD")},
        account_as_of={"U1": as_of, "U2": as_of},
        positions=[Position("U1", "VTI", "1", "STK", "", 800, 100.0, 80_000, 60_000, "USD"),
                   Position("U1", "AAPL", "2", "STK", "", 10, 200.0, 2_000, 1_000, "USD")],
        lots=[Lot("U1", "VTI", "1", 300, date(2020, 1, 2), 15_000, 100.0),      # long-term gain 50/share
              Lot("U1", "VTI", "1", 500, date(2026, 6, 1), 45_000, 100.0)],     # short-term gain 10/share
        trades=[Trade("U2", "BND", "3", date(2026, 10, 1), -10, 70.0, 0.0),
                Trade("U1", "BND", "3", date(2026, 10, 2), -100, 70.0, -500.0)],  # loss sale, taxable
        cash={"U1": 20_000.0})


def test_first_run_rebalances_to_the_target_with_lots_and_wash_checks(tmp_path: Path) -> None:
    store_flat(tmp_path, "VTI", 100.0)
    store_flat(tmp_path, "BND", 70.0)
    [u1, u2] = suggest.build(holdings(), config(), tmp_path, tmp_path / "state", TODAY)
    assert u1.refused is None and u1.decision_day and u1.outside == ["AAPL"]
    # capital = 800 x 100 + 20,000 = 100,000: VTI to 500 shares, BND to 714
    trades = {t.symbol: t for t in u1.trades}
    assert trades["VTI"].shares == -300 and trades["BND"].shares == 714
    # least tax first: the short-term lot gains 10/share (3.70 tax) and the long-term one 50/share (7.50)
    assert [ls.lot.opened for ls in trades["VTI"].lots] == [date(2026, 6, 1)]
    assert trades["VTI"].est_tax == pytest.approx(300 * 10 * 0.37)
    assert trades["BND"].wash is not None and "2026-10-02" in trades["BND"].wash.why
    assert u2.why.startswith("no overlay")


def test_tax_deferred_accounts_get_no_lots_or_tax(tmp_path: Path) -> None:
    store_flat(tmp_path, "VTI", 100.0)
    store_flat(tmp_path, "BND", 70.0)
    [u1, _] = suggest.build(holdings(), config(taxable=False), tmp_path, tmp_path / "state", TODAY)
    vti = next(t for t in u1.trades if t.symbol == "VTI")
    assert vti.lots == [] and vti.est_tax is None and u1.harvest == []


def test_old_statements_and_old_prices_are_refused(tmp_path: Path) -> None:
    store_flat(tmp_path, "VTI", 100.0)
    store_flat(tmp_path, "BND", 70.0, last=TODAY - timedelta(days=12))
    [u1, _] = suggest.build(holdings(as_of=TODAY - timedelta(days=9)), config(), tmp_path, tmp_path / "s", TODAY)
    assert u1.refused is not None and "statement" in u1.refused
    [u1, _] = suggest.build(holdings(), config(), tmp_path, tmp_path / "s", TODAY)
    assert u1.refused is not None and "no recent closes" in u1.refused


def test_only_one_decision_a_day_moves_the_state(tmp_path: Path) -> None:
    store_flat(tmp_path, "VTI", 100.0)
    store_flat(tmp_path, "BND", 70.0)
    suggest.build(holdings(), config(), tmp_path, tmp_path / "state", TODAY)
    # the next day is not a rebalance day: nothing suggested
    [u1, _] = suggest.build(holdings(), config(), tmp_path, tmp_path / "state", TODAY + timedelta(days=1))
    assert not u1.decision_day and u1.trades == [] and u1.why == "not a rebalance day"


def test_the_report_says_nothing_was_sent(tmp_path: Path) -> None:
    store_flat(tmp_path, "VTI", 100.0)
    store_flat(tmp_path, "BND", 70.0)
    h = holdings()
    text = suggest.render(suggest.build(h, config(), tmp_path, tmp_path / "state", TODAY), h, TODAY)
    assert "Suggestions only. Nothing has been sent." in text
    assert "| Sell VTI | 300 |" in text and "| Buy BND | 714 |" in text
    assert "Outside the overlay (left alone): AAPL." in text
