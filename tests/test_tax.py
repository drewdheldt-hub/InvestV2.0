"""Lot choice, holding periods, wash sales and harvesting candidates."""

from __future__ import annotations

from datetime import date

import pytest

from investtree.portfolio import tax
from investtree.portfolio.holdings import Lot, Trade

TODAY = date(2026, 10, 13)
RATES = tax.Rates(short_term=0.37, long_term=0.15)


def lot(qty: float, cost_per_share: float, opened: date, price: float = 100.0, symbol: str = "VTI",
        account: str = "U1") -> Lot:
    return Lot(account, symbol, "1", qty, opened, qty * cost_per_share, price)


def test_long_term_means_more_than_a_year() -> None:
    assert not tax.long_term(date(2025, 10, 13), date(2026, 10, 13))
    assert tax.long_term(date(2025, 10, 13), date(2026, 10, 14))
    assert tax.long_term(date(2024, 2, 29), date(2025, 3, 2))


def test_losses_first_then_cheap_long_term_gains_then_short_term_gains() -> None:
    lots = [lot(10, 50, date(2020, 1, 2)),          # long-term gain of 50/share: tax 7.5/share
            lot(10, 95, date(2026, 6, 1)),          # short-term gain of 5/share: tax 1.85/share
            lot(10, 120, date(2026, 5, 1)),         # short-term loss: saves 7.4/share
            lot(10, 110, date(2023, 1, 3))]         # long-term loss: saves 1.5/share
    sales = tax.choose_lots(lots, 35, 100.0, TODAY, RATES)
    assert [s.lot.cost_per_share for s in sales] == [120, 110, 95, 50]
    assert [s.shares for s in sales] == [10, 10, 10, 5]
    assert sum(s.tax for s in sales) == pytest.approx(-74 - 15 + 18.5 + 37.5)


def test_selling_more_than_the_lots_hold_raises() -> None:
    with pytest.raises(ValueError, match="hold"):
        tax.choose_lots([lot(5, 90, date(2026, 1, 2))], 6, 100.0, TODAY, RATES)


def test_wash_sale_risks() -> None:
    trades = [Trade("IRA", "BND", "9", date(2026, 9, 20), 10, 70, 0),           # bought in the IRA
              Trade("U1", "VTI", "1", date(2026, 10, 1), -5, 90, -100)]         # loss sale in a taxable account
    assert tax.wash_risk_of_sale(trades, "BND", TODAY) is not None             # purchases in any account count
    assert tax.wash_risk_of_sale(trades, "BND", date(2026, 10, 21)) is None
    risk = tax.wash_risk_of_buy(trades, "VTI", TODAY, taxable={"U1"})
    assert risk is not None and "2026-11-01" in risk.why
    assert tax.wash_risk_of_buy(trades, "VTI", TODAY, taxable=set()) is None


def test_harvest_candidates_are_large_losses_ranked_by_tax_saved() -> None:
    lots = [lot(100, 120, date(2026, 5, 1)),                  # short-term loss 2,000
            lot(100, 125, date(2022, 5, 2), symbol="BND"),    # long-term loss 2,500
            lot(100, 101, date(2026, 5, 1), symbol="X"),      # loss 100: too small
            lot(100, 80, date(2026, 5, 1), symbol="Y")]       # a gain
    trades = [Trade("U1", "VTI", "1", date(2026, 10, 1), 1, 100, 0)]
    out = tax.harvest_candidates(lots, trades, TODAY, RATES, pairs={"VTI": "ITOT"})
    assert [h.lot.symbol for h in out] == ["VTI", "BND"]       # 740 saved before 375
    assert out[0].replacement == "ITOT" and out[0].blocked is not None
    assert out[1].tax_saved == pytest.approx(375)
