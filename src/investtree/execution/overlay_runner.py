"""The paper overlay runner: each weekday before the open, one decision for one portfolio, and after the open, a
record of what the orders filled at.

Carried over from v1 (investtree/overlay.py) with the same rules and the same state file layout, so v1's
running state can be imported (import_v1) and the paper record carries on unbroken.

    run(today)            before the open: refresh the bars, decide, and (execute=True, paper) send
                          market-on-open orders for the difference between what the account holds and what
                          the policy wants
    record_fills(today)   after the open, read-only: shares and price filled, commission, the official open,
                          and the cost against the open and against the close each order was sized on

Safety: the broker adapter refuses live accounts; orders are sent only with execute=True; the runner uses
only `capital` of the account; it decides at most once a day; it refuses to manage positions it did not
build (unless adopt); trades smaller than min_trade of capital are skipped; while the STOP file exists
nothing is sent; on a day IB's calendar shows the market closed it does nothing; it will not decide on
closes more than a few days old.

State and journal: var/overlay/<name>.json and var/overlay/<name>.journal.jsonl.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from ..config import OverlayConfig, Paths
from ..data import bars
from ..portfolio import overlay
from ..portfolio.risk import MAX_EXPOSURE
from .broker import Broker

TAG = "investtree-overlay"         # v1's tag too, so fills of orders v1 sent are still recognised
MAX_CLOSE_AGE_DAYS = 5             # calendar days between the last close and today (a long weekend fits)


@dataclass
class Outcome:
    action: str                    # none, refused, halted, signal only, orders sent, orders partly sent, ...
    why: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.action in ("refused", "orders partly sent")


class Runner:
    def __init__(self, broker: Broker, cfg: OverlayConfig, paths: Paths) -> None:
        self.b, self.cfg, self.paths = broker, cfg, paths
        self.symbols = list(cfg.weights)
        self.target = np.array([cfg.weights[s] for s in self.symbols], dtype=float)
        paths.overlay_state.mkdir(parents=True, exist_ok=True)
        self.state_path = paths.overlay_state / f"{cfg.name}.json"
        self.journal = paths.overlay_state / f"{cfg.name}.journal.jsonl"
        self.state: dict[str, Any] = {"last_day": None, "expected": {}, "orders": [], "scale": MAX_EXPOSURE}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text(encoding="utf-8")))

    # ------------------------------------------------------------ files
    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def note(self, kind: str, today: date, **fields: Any) -> None:
        """One journal line (append-only), in v1's layout."""
        row = {"ts": today.isoformat(), "symbol": self.cfg.name, "kind": kind, "policy": self.cfg.policy,
               "execute": self.cfg.execute, **fields}
        with open(self.journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=_plain) + "\n")

    # ------------------------------------------------------------ the morning decision
    def run(self, today: date) -> Outcome:
        cfg = self.cfg
        if self.state["last_day"] == today.isoformat():
            return Outcome("none", "already decided today")
        if not self.b.is_trading_day(today):
            return Outcome("none", "market closed today")
        held_sh = self.b.positions(self.symbols)
        shares = np.array([held_sh.get(s, 0) for s in self.symbols], dtype=float)
        expected: dict[str, int] = self.state.get("expected") or {}
        if not expected and shares.any() and not cfg.adopt:
            msg = (f"the account already holds {', '.join(f'{s} {q}' for s, q in held_sh.items() if q)} "
                   "that this runner did not build; set adopt to manage them")
            self.note("halt", today, reason=msg)
            return Outcome("refused", msg)
        drift = {s: held_sh.get(s, 0) - int(q) for s, q in expected.items() if held_sh.get(s, 0) != int(q)}

        refreshed = bars.refresh(self.paths.data, self.symbols, self.b.daily_bars)
        not_stored = [r.symbol for r in refreshed if not r.written]
        days, closes = bars.closes(self.paths.data, self.symbols, before=today)
        days, closes = days[-cfg.history_days:], closes[-cfg.history_days:]
        if len(days) < 2:
            return Outcome("refused", "not enough history" + (f" (not refreshed: {not_stored})" if not_stored else ""))
        if (today - days[-1]).days > MAX_CLOSE_AGE_DAYS:
            msg = f"the latest close is from {days[-1]}; refusing to decide on old prices"
            self.note("halt", today, reason=msg, not_refreshed=not_stored or None)
            return Outcome("refused", msg)

        prices = closes[-1]
        capital = min(cfg.capital, self.b.net_liquidation())
        held = shares * prices / capital
        first = not self.state.get("started")
        d = overlay.decide(cfg.policy, days, closes, self.target, today, held,
                           self.state.get("scale", MAX_EXPOSURE), first=first)
        why = ("first run, building the portfolio: " if first else "") + d.why
        self.state["last_day"] = today.isoformat()
        self.state["scale"] = d.scale
        detail: dict[str, Any] = {"day": today.isoformat(), "capital": capital, "drift": drift,
                                  "held": dict(zip(self.symbols, held.round(4).tolist(), strict=True))}
        if not_stored:
            detail["not_refreshed"] = not_stored
        if d.want is None:
            self._save()
            self.note("check", today, why=why, held=detail["held"], fills_differ=drift or None)
            return Outcome("none", why, detail)

        orders = overlay.plan(d.want, self.symbols, prices, shares, capital, cfg.min_trade)
        detail.update(want=dict(zip(self.symbols, d.want.round(4).tolist(), strict=True)), orders=orders)
        if self.paths.stop_file.exists():
            # nothing sent, and the day is not used up: once the STOP file is gone a rerun can still act
            self.state["last_day"] = None
            self.note("halt", today, reason="STOP file present: nothing sent", orders=orders)
            self._save()
            return Outcome("halted", f"{why}; STOP file present, nothing sent", detail)
        # Save the decision BEFORE sending anything: if a send fails partway, a rerun today finds the day
        # already decided and cannot send the same order twice; tomorrow's check reports what filled.
        self.state["orders"] = [{"day": today.isoformat(), "symbol": s, "shares": q,
                                 "ref_px": float(prices[self.symbols.index(s)]), "sent": False} for s, q in orders]
        held_now = {s: int(h) for s, h in zip(self.symbols, shares, strict=True)}
        self.state["expected"] = dict(held_now)
        if cfg.execute and first:
            self.state["started"] = today.isoformat()        # a signal-only run builds nothing
        self._save()
        failed = None
        if cfg.execute:
            for o in self.state["orders"]:
                try:
                    self.b.place_market_on_open(o["symbol"], o["shares"], TAG)
                except Exception as e:                       # leave the rest unsent; tomorrow reconciles
                    failed = f"{o['symbol']}: {type(e).__name__}: {e}"
                    break
                o["sent"] = True
                self.state["expected"][o["symbol"]] = held_now[o["symbol"]] + o["shares"]
                self._save()
        self.note("decision", today, why=why, want=detail["want"], held=detail["held"], orders=orders,
                  sent=cfg.execute, fills_differ=drift or None, send_failed=failed)
        if failed:
            return Outcome("orders partly sent", f"{why}; sending stopped at {failed}", detail)
        if not orders:
            return Outcome("no orders", f"{why}; already within {cfg.min_trade:.1%} of the target", detail)
        return Outcome("orders sent" if cfg.execute else "signal only", why, detail)

    # ------------------------------------------------------------ after the open
    def record_fills(self, today: date) -> Outcome:
        """What today's market-on-open orders filled at. Read-only and once a day. Costs in basis points,
        positive meaning worse for us: against the open (near 0 when the order fills in the opening auction)
        and against the close it was sized on (that plus the overnight move)."""
        day = today.isoformat()
        orders = [o for o in self.state.get("orders") or [] if o.get("day") == day and o.get("sent")]
        if not orders:
            return Outcome("none", "no orders were sent today")
        if (self.state.get("fills") or {}).get("day") == day:
            return Outcome("none", "fills already recorded today")
        execs = self.b.executions_today(TAG)
        rows = []
        for o in orders:
            side = 1 if o["shares"] > 0 else -1
            mine = [e for e in execs if e.symbol == o["symbol"] and e.shares * side > 0]
            qty = sum(e.shares for e in mine)
            px = sum(e.shares * e.price for e in mine) / qty if qty else None
            open_px = self.b.opening_price(o["symbol"], today)
            rows.append({"symbol": o["symbol"], "ordered": o["shares"], "filled": qty,
                         "price": round(px, 4) if px else None, "open": open_px, "ref_px": o["ref_px"],
                         "vs_open_bp": overlay.cost_bp(side, px, open_px),
                         "vs_close_bp": overlay.cost_bp(side, px, o["ref_px"]),
                         "commission": round(sum(e.commission or 0.0 for e in mine), 2)})
        short = [f"{r['symbol']} {r['filled']} of {r['ordered']}" for r in rows if r["filled"] != r["ordered"]]
        self.state["fills"] = {"day": day, "rows": rows, "unfilled": short or None}
        self._save()
        self.note("fills", today, fills=rows, unfilled=short or None)
        if short:
            return Outcome("not all filled", "not all filled: " + ", ".join(short), {"fills": rows})
        return Outcome("fills recorded", "all filled", {"fills": rows})


def _plain(x: object) -> object:
    """JSON for numpy numbers and tuples in journal fields."""
    if isinstance(x, np.generic):
        return x.item()
    raise TypeError(f"not JSON serialisable: {type(x).__name__}")


# ------------------------------------------------------------------ moving v1's runner to v2
def import_v1(v1_state_dir: Path, name: str, paths: Paths) -> list[Path]:
    """Copy v1's state and journal for runner `name` into var/overlay/, so v2 carries on the same paper
    record. Refuses to overwrite a v2 state that already exists. Returns what was copied."""
    src = [v1_state_dir / f"{name}.json", v1_state_dir / f"{name}.journal.jsonl"]
    if not src[0].exists():
        raise FileNotFoundError(f"no v1 state at {src[0]}")
    paths.overlay_state.mkdir(parents=True, exist_ok=True)
    dst = [paths.overlay_state / p.name for p in src]
    if dst[0].exists():
        raise FileExistsError(f"{dst[0]} already exists; v2 already runs {name}")
    copied = []
    for s, d in zip(src, dst, strict=True):
        if s.exists():
            shutil.copy2(s, d)
            copied.append(d)
    return copied


# ------------------------------------------------------------------ fill review
@dataclass(frozen=True)
class FillReview:
    days: int                          # mornings with recorded fills
    orders: int
    unfilled: int                      # orders that did not fill in full
    avg_vs_open_bp: float | None       # what the market-on-open orders cost against the official open
    avg_vs_close_bp: float | None      # against the close each order was sized on
    commission: float
    within_target: bool | None         # average fill within 10 bp of the open (the Blueprint's measure)


def review_fills(journal: Path, since: date | None = None, target_bp: float = 10.0) -> FillReview:
    """Every recorded fill in a runner's journal, summarised. Averages are weighted by order value."""
    rows: list[dict[str, Any]] = []
    days = 0
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("kind") != "fills" or (since and date.fromisoformat(e["ts"]) < since):
                continue
            days += 1
            rows.extend(e.get("fills") or [])

    def avg(key: str) -> float | None:
        pts = [(r[key], abs(r["filled"]) * (r["price"] or 0)) for r in rows if r.get(key) is not None and r["filled"]]
        weight = sum(w for _, w in pts)
        return round(sum(v * w for v, w in pts) / weight, 2) if weight else None

    vs_open = avg("vs_open_bp")
    return FillReview(days=days, orders=len(rows), unfilled=sum(r["filled"] != r["ordered"] for r in rows),
                      avg_vs_open_bp=vs_open, avg_vs_close_bp=avg("vs_close_bp"),
                      commission=round(sum(r.get("commission") or 0 for r in rows), 2),
                      within_target=None if vs_open is None else abs(vs_open) <= target_bp)


def first_paper_day(state: dict[str, Any]) -> date | None:
    started = state.get("started")
    return date.fromisoformat(started) if isinstance(started, str) else None


def paper_record(state_path: Path, journal: Path, today: date) -> dict[str, Any]:
    """Progress towards the phase 1 paper gate: at least 3 months on paper, with 12 weekly decisions and 3
    monthly rebalances, and share counts that always reconciled."""
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    start = first_paper_day(state)
    weekly = monthly = mismatches = 0
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("fills_differ"):
                mismatches += 1
            if e.get("kind") == "decision" and e.get("sent"):
                why = str(e.get("why", ""))
                monthly += "monthly" in why or "first run" in why
                weekly += "weekly" in why
    months = 0.0 if start is None else (today - start) / timedelta(days=30.4375)
    return {"started": start.isoformat() if start else None, "months_on_paper": round(months, 1),
            "weekly_decisions": weekly, "monthly_rebalances": monthly, "share_mismatches": mismatches,
            "gate_met": months >= 3 and weekly >= 12 and monthly >= 3 and mismatches == 0}
