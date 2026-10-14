"""Starting IB Gateway: nothing done when it is up, IBC launched otherwise, and IBC's settings never opened."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest

from investtree.ops import gateway


def setup(tmp_path: Path) -> tuple[Path, Path]:
    bat, ini = tmp_path / "StartGateway.bat", tmp_path / "config.ini"
    bat.write_text("", encoding="utf-8")
    ini.write_text("", encoding="utf-8")
    return bat, ini


def test_already_running_launches_nothing(tmp_path: Path) -> None:
    bat, ini = setup(tmp_path)
    launched: list[Path] = []
    r = gateway.start(ibc=bat, settings=ini, is_up=lambda: True, launch=launched.append)
    assert r.ok and not r.started and not launched


def test_launches_ibc_and_waits_for_the_port(tmp_path: Path) -> None:
    bat, ini = setup(tmp_path)
    launched: list[Path] = []
    answers = iter([False, False, True])
    r = gateway.start(poll_seconds=0, ibc=bat, settings=ini, is_up=lambda: next(answers), launch=launched.append)
    assert r.ok and r.started and launched == [bat]


def test_gives_up_after_the_wait(tmp_path: Path) -> None:
    bat, ini = setup(tmp_path)
    r = gateway.start(wait_seconds=0, ibc=bat, settings=ini, is_up=lambda: False, launch=lambda p: None)
    assert not r.ok and "did not open port" in r.message


def no_launch(bat: Path) -> None:
    raise AssertionError("IBC launched")


def test_missing_ibc_or_settings_launch_nothing(tmp_path: Path) -> None:
    bat, ini = setup(tmp_path)
    assert not gateway.start(ibc=tmp_path / "missing.bat", settings=ini, is_up=lambda: False, launch=no_launch).ok
    assert not gateway.start(ibc=bat, settings=tmp_path / "none.ini", is_up=lambda: False, launch=no_launch).ok


def test_the_settings_file_is_never_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It holds the paper login: its existence is checked, its content never read."""
    bat, ini = setup(tmp_path)
    real_open = builtins.open

    def guarded(file: Any, *a: Any, **k: Any) -> Any:
        assert Path(str(file)) != ini, "config.ini was opened"
        return real_open(file, *a, **k)

    def no_read(self: Path, *a: Any, **k: Any) -> str:
        raise AssertionError(f"read {self}")

    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(Path, "read_text", no_read)
    monkeypatch.setattr(Path, "read_bytes", no_read)
    assert not gateway.start(wait_seconds=0, ibc=bat, settings=ini, is_up=lambda: False, launch=lambda p: None).ok
