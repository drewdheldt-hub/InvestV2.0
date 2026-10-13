"""Tier 1 data: dividend-adjusted daily bars for ETFs and the holdings, one Parquet file per symbol.

    data/bars/adjusted/<SYMBOL>.parquet     day, open, high, low, close, volume (oldest first)
    data/bars/manifest.json                 per symbol: rows, first and last day, content hash, when fetched

Dividend adjustment changes past prices every time a fund pays out, so a symbol's history is refreshed
whole rather than appended to. A refresh is checked before it replaces the stored file: one that fails the
quality checks is not written, and the problem is reported. Funds and today's holdings have no
survivorship problem, which is why this tier needs no paid data.

The data layer does not talk to the broker. Whoever updates it passes a fetch function (the broker
adapter's daily_bars, in practice), so this module stays testable and below the execution layer.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import numpy as np
import polars as pl
from numpy.typing import NDArray

SCHEMA = {"day": pl.Date, "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
          "volume": pl.Float64}
MAX_GAP_DAYS = 6           # calendar days between sessions; the longest US market closure since 2001 was 4
SUSPECT_MOVE = 0.25        # a daily close-to-close move this large is flagged for a look, not rejected


class BarLike(Protocol):
    """Anything with a bar's fields (the broker adapter's Bar qualifies)."""

    @property
    def day(self) -> date: ...
    @property
    def open(self) -> float: ...
    @property
    def high(self) -> float: ...
    @property
    def low(self) -> float: ...
    @property
    def close(self) -> float: ...
    @property
    def volume(self) -> float: ...


Fetch = Callable[[str, int], Sequence[BarLike]]


def folder(data: Path) -> Path:
    return data / "bars" / "adjusted"


def path(data: Path, symbol: str) -> Path:
    return folder(data) / f"{symbol.upper()}.parquet"


def manifest_path(data: Path) -> Path:
    return data / "bars" / "manifest.json"


def to_frame(bars: Iterable[BarLike]) -> pl.DataFrame:
    rows = [{"day": b.day, "open": float(b.open), "high": float(b.high), "low": float(b.low),
             "close": float(b.close), "volume": float(b.volume)} for b in bars]
    return pl.DataFrame(rows, schema=SCHEMA)


def fingerprint(frame: pl.DataFrame) -> str:
    """A content hash of the bars: the same prices give the same hash, whatever file they came from."""
    h = hashlib.sha256()
    for row in frame.select(list(SCHEMA)).iter_rows():
        h.update(repr(row).encode())
    return h.hexdigest()


# ------------------------------------------------------------------ quality
@dataclass(frozen=True)
class Problem:
    symbol: str
    day: date | None
    what: str
    fatal: bool          # a fatal problem stops the refresh; the rest are reported for a look


def check(symbol: str, frame: pl.DataFrame) -> list[Problem]:
    """What is wrong with a symbol's bars. Fatal: no rows, dates out of order or repeated, prices that are not
    positive, a close outside the day's range, a gap of more than MAX_GAP_DAYS between sessions.
    Reported only: a close-to-close move of more than SUSPECT_MOVE."""
    out: list[Problem] = []
    if frame.is_empty():
        return [Problem(symbol, None, "no bars", True)]
    days = frame["day"].to_list()
    for a, b in itertools.pairwise(days):
        if b <= a:
            out.append(Problem(symbol, b, f"date not after {a}", True))
        elif (b - a).days > MAX_GAP_DAYS:
            out.append(Problem(symbol, b, f"{(b - a).days} calendar days since the previous bar ({a})", True))
    for r in frame.iter_rows(named=True):
        if min(r["open"], r["high"], r["low"], r["close"]) <= 0:
            out.append(Problem(symbol, r["day"], "a price that is not positive", True))
        elif not r["low"] - 1e-9 <= r["close"] <= r["high"] + 1e-9 or r["low"] > r["high"] + 1e-9:
            out.append(Problem(symbol, r["day"], "close outside the day's low-high range", True))
    close = frame["close"].to_numpy()
    moves = np.abs(close[1:] / close[:-1] - 1.0) if len(close) > 1 else np.array([])
    for i in np.flatnonzero(moves > SUSPECT_MOVE):
        out.append(Problem(symbol, days[i + 1], f"close moved {moves[i]:.0%} in a day", False))
    return out


# ------------------------------------------------------------------ store
def read_manifest(data: Path) -> dict[str, dict[str, object]]:
    p = manifest_path(data)
    if not p.exists():
        return {}
    loaded: dict[str, dict[str, object]] = json.loads(p.read_text(encoding="utf-8"))
    return loaded


def _write_manifest(data: Path, manifest: dict[str, dict[str, object]]) -> None:
    p = manifest_path(data)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


@dataclass(frozen=True)
class Refresh:
    symbol: str
    written: bool
    rows: int
    last: date | None
    changed: bool                 # the stored history differs from before (a dividend, or a new day)
    problems: list[Problem]


def store(data: Path, symbol: str, frame: pl.DataFrame, now: datetime | None = None) -> Refresh:
    """Check a symbol's fresh history and, if nothing fatal is wrong, replace the stored file (atomically)
    and record its fingerprint in the manifest."""
    symbol = symbol.upper()
    problems = check(symbol, frame)
    if any(p.fatal for p in problems):
        return Refresh(symbol, False, frame.height, None, False, problems)
    digest = fingerprint(frame)
    manifest = read_manifest(data)
    before = manifest.get(symbol, {}).get("sha256")
    target = path(data, symbol)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    frame.write_parquet(tmp)
    tmp.replace(target)
    last: date = frame["day"][-1]
    manifest[symbol] = {"rows": frame.height, "first": frame["day"][0].isoformat(), "last": last.isoformat(),
                        "sha256": digest, "fetched": (now or datetime.now()).isoformat(timespec="seconds")}
    _write_manifest(data, manifest)
    return Refresh(symbol, True, frame.height, last, digest != before, problems)


def refresh(data: Path, symbols: Sequence[str], fetch: Fetch, days: int = 5300) -> list[Refresh]:
    """Fetch and store each symbol's full adjusted history (about 21 years by default). A symbol that fails is
    reported and the others carry on."""
    out = []
    for s in symbols:
        try:
            frame = to_frame(fetch(s, days))
        except Exception as e:                       # one symbol's failure must not stop the rest
            failed = Problem(s.upper(), None, f"fetch failed: {e}", True)
            out.append(Refresh(s.upper(), False, 0, None, False, [failed]))
            continue
        out.append(store(data, s, frame))
    return out


def load(data: Path, symbol: str) -> pl.DataFrame:
    p = path(data, symbol)
    if not p.exists():
        raise FileNotFoundError(f"no stored bars for {symbol.upper()}; run: investtree data update {symbol.upper()}")
    return pl.read_parquet(p)


def closes(data: Path, symbols: Sequence[str], before: date | None = None,
           ) -> tuple[list[date], NDArray[np.float64]]:
    """(dates, closes [T, N]) on the dates every symbol has, oldest first, columns in `symbols` order.
    before: only sessions strictly before this day (a morning decision uses closes up to yesterday)."""
    frames = []
    for s in symbols:
        f = load(data, s).select("day", pl.col("close").alias(s.upper()))
        frames.append(f if before is None else f.filter(pl.col("day") < before))
    joined = frames[0]
    for f in frames[1:]:
        joined = joined.join(f, on="day", how="inner")
    joined = joined.sort("day")
    return joined["day"].to_list(), joined.select([s.upper() for s in symbols]).to_numpy().astype(float)


def stale(data: Path, symbols: Sequence[str], today: date, max_age_days: int = 4) -> list[str]:
    """Symbols whose last stored session is more than max_age_days calendar days before today (a long weekend
    is allowed for), or that have never been stored."""
    manifest = read_manifest(data)
    out = []
    for s in symbols:
        last = manifest.get(s.upper(), {}).get("last")
        if not isinstance(last, str) or today - date.fromisoformat(last) > timedelta(days=max_age_days):
            out.append(s.upper())
    return out
