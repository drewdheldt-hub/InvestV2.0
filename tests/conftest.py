from __future__ import annotations

from pathlib import Path

import pytest

from investtree.config import Paths

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    """A throwaway project folder with the real config files, so nothing touches the real var/ or reports/."""
    (tmp_path / "config").mkdir()
    for name in ("schedule.yaml", "agents.yaml", "overlays.yaml"):
        (tmp_path / "config" / name).write_text((REPO / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("INVESTTREE_HOME", str(tmp_path))
    return Paths(tmp_path)
