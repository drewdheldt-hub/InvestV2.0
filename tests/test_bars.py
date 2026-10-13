"""Tier 1 bars: refreshes are checked before they replace anything, and fingerprints track content."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest

from investtree.data import bars


@dataclass(frozen=True)
class B:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 1e6


def series(start: date, closes: list[float]) -> list[B]:
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(B(d, c, c * 1.01, c * 0.99, c))
        d += timedelta(days=1)
    return out


def test_store_and_load_round_trip(tmp_path: Path) -> None:
    r = bars.store(tmp_path, "spy", bars.to_frame(series(date(2026, 1, 5), [100, 101, 102])))
    assert r.written and r.changed and r.rows == 3 and r.last == date(2026, 1, 7)
    f = bars.load(tmp_path, "SPY")
    assert f["close"].to_list() == [100, 101, 102]
    assert bars.read_manifest(tmp_path)["SPY"]["sha256"] == bars.fingerprint(f)


def test_same_prices_are_not_a_change(tmp_path: Path) -> None:
    b = series(date(2026, 1, 5), [100, 101])
    bars.store(tmp_path, "SPY", bars.to_frame(b))
    assert not bars.store(tmp_path, "SPY", bars.to_frame(b)).changed
    # a dividend re-adjusts the past: same days, different prices
    assert bars.store(tmp_path, "SPY", bars.to_frame(series(date(2026, 1, 5), [99.5, 100.5]))).changed


def test_a_bad_refresh_does_not_replace_good_data(tmp_path: Path) -> None:
    bars.store(tmp_path, "SPY", bars.to_frame(series(date(2026, 1, 5), [100, 101])))
    bad = [*series(date(2026, 1, 5), [100, 101]), B(date(2026, 1, 7), 102, 101, 103, 102)]   # high below low
    r = bars.store(tmp_path, "SPY", bars.to_frame(bad))
    assert not r.written and any(p.fatal for p in r.problems)
    assert bars.load(tmp_path, "SPY").height == 2


@pytest.mark.parametrize(("rows", "what"), [
    ([], "no bars"),
    ([B(date(2026, 1, 6), 1, 1, 1, 1), B(date(2026, 1, 5), 1, 1, 1, 1)], "date not after"),
    ([B(date(2026, 1, 5), 1, 1, 1, 1), B(date(2026, 1, 20), 1, 1, 1, 1)], "calendar days since"),
    ([B(date(2026, 1, 5), 0, 1, 0, 1)], "not positive"),
    ([B(date(2026, 1, 5), 1, 1, 0.9, 1.2)], "outside the day's"),
])
def test_fatal_problems(rows: list[B], what: str) -> None:
    problems = bars.check("SPY", bars.to_frame(rows))
    assert any(p.fatal and what in p.what for p in problems), problems


def test_a_large_move_is_flagged_but_kept(tmp_path: Path) -> None:
    r = bars.store(tmp_path, "XYZ", bars.to_frame(series(date(2026, 1, 5), [100, 60, 61])))
    assert r.written and [p.what for p in r.problems] == ["close moved 40% in a day"]


def test_closes_on_common_dates_before_a_day(tmp_path: Path) -> None:
    bars.store(tmp_path, "SPY", bars.to_frame(series(date(2026, 1, 5), [100, 101, 102, 103])))
    bars.store(tmp_path, "TLT", bars.to_frame(series(date(2026, 1, 6), [50, 51, 52])))
    days, c = bars.closes(tmp_path, ["SPY", "TLT"], before=date(2026, 1, 8))
    assert days == [date(2026, 1, 6), date(2026, 1, 7)]
    assert c.tolist() == [[101, 50], [102, 51]]


def test_refresh_carries_on_past_a_failed_symbol(tmp_path: Path) -> None:
    def fetch(symbol: str, days: int) -> list[B]:
        if symbol == "BAD":
            raise RuntimeError("no contract")
        return series(date(2026, 1, 5), [10, 11])

    out = bars.refresh(tmp_path, ["BAD", "SPY"], fetch)
    assert [(r.symbol, r.written) for r in out] == [("BAD", False), ("SPY", True)]


def test_stale_symbols(tmp_path: Path) -> None:
    bars.store(tmp_path, "SPY", bars.to_frame(series(date(2026, 1, 5), [100, 101])))   # last: Tuesday 6 Jan
    assert bars.stale(tmp_path, ["SPY", "TLT"], date(2026, 1, 9)) == ["TLT"]
    assert bars.stale(tmp_path, ["SPY"], date(2026, 1, 12)) == ["SPY"]
