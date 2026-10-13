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
