"""The investtree command: phase 1 commands without a Gateway, and the symbols they work on."""

from __future__ import annotations

import pytest

from investtree import cli
from investtree.config import Paths
from investtree.ops import status


@pytest.fixture
def no_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_listening", lambda port: False)


def test_project_symbols_cover_overlays_accounts_and_spy(paths: Paths) -> None:
    (paths.config / "accounts.local.yaml").write_text(
        "accounts:\n  - id: U1\n    taxable: true\n    overlay: {weights: {VTI: 0.6, BND: 0.4}}\n", encoding="utf-8")
    assert cli.project_symbols(paths) == ["BND", "SPY", "VTI"]


def test_without_a_gateway_nothing_runs_and_the_feed_says_why(paths: Paths, no_gateway: None,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["overlay", "run", "spy_voltarget"]) == 1
    assert cli.main(["data", "update", "SPY"]) == 1
    notes = {e.job: e.text for e in status.read(paths.status_feed)}
    assert notes["overlay-paper"].endswith("no decision today")
    assert "not updated" in notes["data-update"]


def test_an_unknown_overlay_is_named(paths: Paths) -> None:
    with pytest.raises(KeyError, match="no overlay named"):
        cli.main(["overlay", "review", "nope"])


def test_review_with_no_record_yet(paths: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["overlay", "review", "spy_voltarget"]) == 0
    assert "not yet met" in capsys.readouterr().out


def test_study_writes_the_band_verdict(paths: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    from datetime import date, timedelta

    import numpy as np

    from investtree.data import bars

    rng = np.random.default_rng(0)
    days = [date(2020, 1, 6) + timedelta(days=i) for i in range(800)]
    days = [d for d in days if d.weekday() < 5]
    for sym, vol in (("AAA", 0.01), ("BBB", 0.004)):
        px = 100 * np.cumprod(1 + rng.normal(0.0002, vol, len(days)))
        rows = [type("B", (), {"day": d, "open": p, "high": p, "low": p, "close": p, "volume": 1.0})()
                for d, p in zip(days, px, strict=True)]
        bars.store(paths.data, sym, bars.to_frame(rows))
    assert cli.main(["study", "AAA:0.6,BBB:0.4", "--runs", "50"]) == 0
    assert "**Verdict:" in capsys.readouterr().out
    assert len(list((paths.reports / "studies").glob("AAA60_BBB40_*.md"))) == 1


def test_overlay_retries_while_the_gateway_does_not_answer(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    from investtree.execution import overlay_runner as ovr

    calls: list[int] = []

    class Quiet:
        def disconnect(self) -> None: ...

    def broker(*a: object, **k: object) -> Quiet:
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("positions request timed out")
        return Quiet()

    class Stub:
        def __init__(self, *a: object) -> None: ...
        def run(self, today: object) -> ovr.Outcome: return ovr.Outcome("none", "not an adjustment day")

    monkeypatch.setattr(cli, "paper_broker", broker)
    monkeypatch.setattr(cli, "RETRY_SECONDS", 0)
    monkeypatch.setattr(ovr, "Runner", Stub)
    assert cli.main(["overlay", "run", "spy_voltarget", "--wait-until", "23:59"]) == 0 and len(calls) == 2


def test_overlay_gives_up_at_its_cut_off(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    def broker(*a: object, **k: object) -> object:
        raise TimeoutError("positions request timed out")

    monkeypatch.setattr(cli, "paper_broker", broker)
    assert cli.main(["overlay", "run", "spy_voltarget"]) == 1                  # no --wait-until: one attempt
    notes = {e.job: e.text for e in status.read(paths.status_feed)}
    assert notes["overlay-paper"] == "the Gateway did not answer (TimeoutError); no decision today"
