from pathlib import Path

from investtree.config import Paths
from investtree.ops import backup


def make_files(paths: Paths) -> None:
    (paths.data / "etf").mkdir(parents=True)
    (paths.data / "etf" / "SPY.csv").write_text("day,close\n2026-10-12,776.25\n", encoding="utf-8")
    paths.status_feed.parent.mkdir(parents=True)
    paths.status_feed.write_text('{"job": "x"}\n', encoding="utf-8")
    (paths.root / "src").mkdir()
    (paths.root / "src" / "code.py").write_text("in git, not backed up", encoding="utf-8")


def test_backup_copies_only_what_git_does_not_hold_and_then_only_changes(paths: Paths, tmp_path: Path) -> None:
    make_files(paths)
    dest = tmp_path / "backup-dest"
    first = backup.run(paths, dest)
    assert (first.files, first.copied) == (2, 2)
    assert (dest / "data" / "etf" / "SPY.csv").exists() and not (dest / "src").exists()
    assert backup.run(paths, dest).copied == 0                     # nothing changed
    (paths.data / "etf" / "SPY.csv").write_text("day,close\n2026-10-13,780.00\n", encoding="utf-8")
    assert backup.run(paths, dest).copied == 1


def test_verify_restores_and_matches_every_file(paths: Paths, tmp_path: Path) -> None:
    make_files(paths)
    dest = tmp_path / "backup-dest"
    backup.run(paths, dest)
    assert backup.verify(dest).ok


def test_verify_catches_damaged_and_missing_files(paths: Paths, tmp_path: Path) -> None:
    make_files(paths)
    dest = tmp_path / "backup-dest"
    backup.run(paths, dest)
    (dest / "data" / "etf" / "SPY.csv").write_text("tampered", encoding="utf-8")
    (dest / "var" / "status" / "feed.jsonl").unlink()
    v = backup.verify(dest)
    assert not v.ok
    assert v.corrupt == ["data/etf/SPY.csv"] and v.missing == ["var/status/feed.jsonl"]


def test_an_empty_backup_does_not_pass(tmp_path: Path) -> None:
    assert not backup.verify(tmp_path / "nothing-here").ok


def test_a_file_deleted_from_the_project_stays_in_the_backup(paths: Paths, tmp_path: Path) -> None:
    make_files(paths)
    dest = tmp_path / "backup-dest"
    backup.run(paths, dest)
    (paths.data / "etf" / "SPY.csv").unlink()
    backup.run(paths, dest)
    assert (dest / "data" / "etf" / "SPY.csv").exists() and backup.verify(dest).ok
