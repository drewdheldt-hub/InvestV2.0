"""The risk report: values, weights, concentration, risk of the current mix, and what is not covered."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from investtree.config import AccountsConfig
from investtree.data import bars
from investtree.portfolio import report
from investtree.portfolio.holdings import Account, Holdings, Position

TODAY = date(2026, 10, 13)


def store_walk(data: Path, symbol: str, n: int = 600, vol: float = 0.01, seed: int = 0) -> None:
    rng = np.random.default_rng(seed)
    px = 100 * np.cumprod(1 + rng.normal(0.0003, vol, n))
    days, d = [], TODAY - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    frame = bars.to_frame([_Bar(day, p) for day, p in zip(days[::-1], px, strict=True)])
    bars.store(data, symbol, frame)


class _Bar:
    def __init__(self, day: date, px: float) -> None:
        self.day, self.open, self.high, self.low, self.close, self.volume = day, px, px, px, px, 1e6


def pos(acct: str, sym: str, value: float, cls: str = "STK") -> Position:
    return Position(acct, sym, "1", cls, "", value / 100, 100.0, value, value * 0.9, "USD")


def holdings() -> Holdings:
    return Holdings(as_of=TODAY - timedelta(days=1),
                    accounts={"U1": Account("U1", "Brokerage", "Individual", "USD"),
                              "U2": Account("U2", "IRA", "IRA", "USD")},
                    account_as_of={"U1": TODAY - timedelta(days=1), "U2": TODAY - timedelta(days=1)},
                    positions=[pos("U1", "SPY", 60_000), pos("U1", "TLT", 30_000), pos("U2", "XYZ", 5_000, "OPT")],
                    cash={"U1": 10_000.0}, sources=["s.xml"])


def test_values_weights_and_concentration(tmp_path: Path) -> None:
    store_walk(tmp_path, "SPY", seed=1)
    store_walk(tmp_path, "TLT", vol=0.005, seed=2)
    [u1, u2, both] = report.build(holdings(), AccountsConfig(), tmp_path)
    assert u1.value == 100_000 and u1.cash == 10_000
    assert [(p.symbol, round(w, 2)) for p, w in u1.positions] == [("SPY", 0.6), ("TLT", 0.3)]
    # two positions at 2:1 -> effective number 1 / ((2/3)^2 + (1/3)^2) = 1.8
    assert u1.effective_holdings == pytest.approx(1.8)
    assert u1.by_asset_class["CASH"] == pytest.approx(0.1)
    assert u1.mix is not None and u1.mix.vol_1y is not None and 0.05 < u1.mix.vol_1y < 0.2
    assert u1.mix.worst_drawdown is not None and u1.mix.beta_spy is None      # SPY is in the mix itself
    assert u2.not_covered == ["XYZ"] and u2.mix is None
    assert both.label == "All accounts" and both.value == 105_000


def test_beta_against_spy_when_spy_is_stored(tmp_path: Path) -> None:
    store_walk(tmp_path, "SPY", seed=1)
    store_walk(tmp_path, "TLT", vol=0.005, seed=2)
    h = holdings()
    h.positions = [pos("U1", "TLT", 50_000)]
    [u1, *_] = report.build(h, AccountsConfig(), tmp_path)
    assert u1.mix is not None and u1.mix.beta_spy is not None and abs(u1.mix.beta_spy) < 0.3


def test_overlay_words_and_the_rendered_report(tmp_path: Path) -> None:
    store_walk(tmp_path, "SPY", seed=1)
    store_walk(tmp_path, "TLT", vol=0.005, seed=2)
    cfg = AccountsConfig.model_validate({"accounts": [
        {"id": "U1", "name": "Brokerage", "taxable": True,
         "overlay": {"weights": {"SPY": 0.6, "TLT": 0.4}, "policy": "vol_target"}}]})
    h = holdings()
    sections = report.build(h, cfg, tmp_path)
    assert sections[0].label == "Brokerage (taxable)"
    assert any("volatility target" in w for w in sections[0].overlay)
    text = report.render(sections, h, cfg, TODAY)
    assert "Not in config/accounts.local.yaml:** U2" in text
    assert "Not covered by the risk figures (no stored daily bars): XYZ" in text
    assert report.write(tmp_path / "reports", text, TODAY).exists()
