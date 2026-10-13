"""The paper overlay runner keeps v1's safety rules: once a day, no foreign positions, STOP, no stale prices,
decision saved before sending, and fills recorded read-only."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pytest

from investtree.config import OverlayConfig, Paths
from investtree.execution import overlay_runner as ovr
from investtree.execution.broker import Bar, Fill

MONDAY = date(2026, 10, 12)


@dataclass
class PaperBroker:
    """Implements the Broker protocol in memory: SPY rising 0.1% a day, $1M of paper."""

    held: dict[str, int] = field(default_factory=dict)
    closed: set[date] = field(default_factory=set)
    last_bar: date = MONDAY - timedelta(days=3)          # Friday
    fail_on: str | None = None
    sent: list[tuple[str, int, str]] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_paper(self) -> bool: return True
    def account_id(self) -> str: return "DU1234567"
    def qualify(self, symbol: str) -> bool: return True
    def positions(self, symbols: list[str]) -> dict[str, int]: return {s: self.held.get(s, 0) for s in symbols}
    def net_liquidation(self) -> float: return 1_000_000.0
    def is_trading_day(self, day: date) -> bool: return day.weekday() < 5 and day not in self.closed
    def opening_price(self, symbol: str, day: date) -> float | None: return 100.0
    def executions_today(self, tag: str) -> list[Fill]: return [f for f in self.fills if f.tag == tag]

    def daily_bars(self, symbol: str, days: int, adjusted: bool = True) -> list[Bar]:
        out, d, px = [], self.last_bar, 100.0
        while len(out) < min(days, 300):
            if d.weekday() < 5:
                out.append(Bar(d, px, px * 1.005, px * 0.995, px, 1e6))
                px /= 1.001
            d -= timedelta(days=1)
        return out[::-1]

    def place_market_on_open(self, symbol: str, shares: int, tag: str) -> None:
        if symbol == self.fail_on:
            raise RuntimeError("connection lost")
        self.sent.append((symbol, shares, tag))


def runner(paths: Paths, broker: PaperBroker, **cfg: object) -> ovr.Runner:
    c = OverlayConfig.model_validate({"name": "spy_voltarget", "weights": {"SPY": 1.0}, "policy": "rebalance",
                                      "capital": 100_000, "execute": True, **cfg})
    return ovr.Runner(broker, c, paths)  # type: ignore[arg-type]


def journal(paths: Paths) -> list[dict[str, object]]:
    p = paths.overlay_state / "spy_voltarget.journal.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_first_run_builds_the_portfolio_and_saves_before_sending(paths: Paths) -> None:
    b = PaperBroker()
    out = runner(paths, b).run(MONDAY)
    assert out.action == "orders sent" and out.why.startswith("first run")
    [(sym, qty, tag)] = b.sent
    assert (sym, tag) == ("SPY", "investtree-overlay") and qty == 100_000 // 100.0
    state = json.loads((paths.overlay_state / "spy_voltarget.json").read_text(encoding="utf-8"))
    assert state["started"] == MONDAY.isoformat() and state["expected"] == {"SPY": qty}
    assert journal(paths)[-1]["kind"] == "decision"


def test_once_a_day_and_never_on_a_closed_day(paths: Paths) -> None:
    b = PaperBroker(closed={MONDAY})
    assert runner(paths, b).run(MONDAY).why == "market closed today"
    b.closed.clear()
    r = runner(paths, b)
    r.run(MONDAY)
    assert r.run(MONDAY).why == "already decided today"
    assert len(b.sent) == 1


def test_positions_it_did_not_build_are_refused_unless_adopted(paths: Paths) -> None:
    b = PaperBroker(held={"SPY": 50})
    out = runner(paths, b).run(MONDAY)
    assert out.action == "refused" and out.failed and not b.sent
    assert runner(paths, b, adopt=True).run(MONDAY).action == "orders sent"


def test_the_stop_file_halts_without_using_up_the_day(paths: Paths) -> None:
    b = PaperBroker()
    paths.stop_file.write_text("", encoding="utf-8")
    assert runner(paths, b).run(MONDAY).action == "halted" and not b.sent
    paths.stop_file.unlink()
    assert runner(paths, b).run(MONDAY).action == "orders sent"


def test_old_prices_are_refused(paths: Paths) -> None:
    b = PaperBroker(last_bar=MONDAY - timedelta(days=10))
    out = runner(paths, b).run(MONDAY)
    assert out.action == "refused" and "old prices" in out.why and not b.sent


def test_a_failed_send_stops_the_rest_and_a_rerun_cannot_resend(paths: Paths) -> None:
    b = PaperBroker(fail_on="SPY")
    out = runner(paths, b, weights={"SPY": 0.5, "TLT": 0.5}).run(MONDAY)
    assert out.action == "orders partly sent" and out.failed
    b.fail_on = None
    assert runner(paths, b, weights={"SPY": 0.5, "TLT": 0.5}).run(MONDAY).why == "already decided today"


def test_signal_only_sends_nothing_and_builds_nothing(paths: Paths) -> None:
    b = PaperBroker()
    out = runner(paths, b, execute=False).run(MONDAY)
    assert out.action == "signal only" and not b.sent
    state = json.loads((paths.overlay_state / "spy_voltarget.json").read_text(encoding="utf-8"))
    assert "started" not in state


def test_fills_are_recorded_once_with_their_costs(paths: Paths) -> None:
    b = PaperBroker()
    r = runner(paths, b)
    r.run(MONDAY)
    qty = b.sent[0][1]
    b.fills = [Fill("SPY", qty, 100.05, 1.0, ovr.TAG), Fill("SPY", 5, 99.0, 1.0, "someone-else")]
    out = r.record_fills(MONDAY)
    [row] = out.detail["fills"]
    assert out.action == "fills recorded"
    assert row["vs_open_bp"] == pytest.approx(5.0) and row["commission"] == 1.0
    assert r.record_fills(MONDAY).why == "fills already recorded today"


def test_import_v1_copies_state_and_will_not_overwrite(paths: Paths, tmp_path: Path) -> None:
    v1 = tmp_path / "v1state"
    v1.mkdir()
    (v1 / "spy_voltarget.json").write_text('{"started": "2026-10-08", "expected": {"SPY": 124}}', encoding="utf-8")
    (v1 / "spy_voltarget.journal.jsonl").write_text("", encoding="utf-8")
    assert len(ovr.import_v1(v1, "spy_voltarget", paths)) == 2
    with pytest.raises(FileExistsError):
        ovr.import_v1(v1, "spy_voltarget", paths)
    # the imported runner carries on: SPY 124 is its own, not a foreign position
    b = PaperBroker(held={"SPY": 124})
    assert runner(paths, b, policy="vol_target").run(MONDAY).action != "refused"


def test_fill_review_and_paper_record(tmp_path: Path) -> None:
    j = tmp_path / "j.jsonl"
    rows = [
        {"ts": "2026-10-08", "kind": "decision", "why": "first run, building the portfolio: monthly rebalance",
         "sent": True},
        {"ts": "2026-10-08", "kind": "fills", "fills": [
            {"symbol": "SPY", "ordered": 100, "filled": 100, "price": 100.0, "vs_open_bp": 2.0,
             "vs_close_bp": 30.0, "commission": 1.0}]},
        {"ts": "2026-10-15", "kind": "decision", "why": "weekly adjustment, exposure 95%", "sent": True},
        {"ts": "2026-10-16", "kind": "check", "fills_differ": {"SPY": -3}},
    ]
    j.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    fr = ovr.review_fills(j)
    assert (fr.days, fr.orders, fr.avg_vs_open_bp, fr.within_target) == (1, 1, 2.0, True)
    s = tmp_path / "s.json"
    s.write_text('{"started": "2026-10-08"}', encoding="utf-8")
    rec = ovr.paper_record(s, j, date(2027, 1, 20))
    assert rec["weekly_decisions"] == 1 and rec["monthly_rebalances"] == 1 and rec["share_mismatches"] == 1
    assert rec["months_on_paper"] > 3 and not rec["gate_met"]
