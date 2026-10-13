from datetime import datetime, timedelta

from investtree.config import Paths
from investtree.ops import status


def test_record_and_read_back_in_order(paths: Paths) -> None:
    status.record(paths.status_feed, "backup", "started", ts="2026-10-13T18:00:00")
    ev = status.record(paths.status_feed, "backup", "finished", "ok", ts="2026-10-13T18:05:00", files=3)
    events = list(status.read(paths.status_feed))
    assert [e.kind for e in events] == ["started", "finished"]
    assert events[-1].detail == {"files": 3}
    assert ev.id == "2026-10-13T18:05:00 backup finished"


def test_a_damaged_line_is_skipped_not_fatal(paths: Paths) -> None:
    status.record(paths.status_feed, "a", "finished", ts="2026-10-13T10:00:00")
    with open(paths.status_feed, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    status.record(paths.status_feed, "b", "failed", ts="2026-10-13T11:00:00")
    assert [e.job for e in status.read(paths.status_feed)] == ["a", "b"]


def test_latest_state_per_job_and_heartbeats(paths: Paths) -> None:
    f = paths.status_feed
    status.record(f, "backup", "started", ts="2026-10-13T18:00:00")
    status.record(f, "backup", "failed", "disk full", ts="2026-10-13T18:01:00")
    status.record(f, "brief", "started", ts="2026-10-13T17:30:00")
    status.record(f, "scheduler", "note", "heartbeat", ts="2026-10-13T19:00:00")
    st = status.latest(f)
    assert st["backup"].failed and not st["backup"].running
    assert st["brief"].running
    assert st["scheduler"].last.text == "heartbeat" and st["scheduler"].last_run is None   # seen, never ran a job


def test_silent_jobs(paths: Paths) -> None:
    f = paths.status_feed
    status.record(f, "scheduler", "note", "heartbeat", ts="2026-10-13T08:00:00")
    st = status.latest(f)
    now = datetime(2026, 10, 13, 20, 0)
    expected = {"scheduler": timedelta(hours=13), "backup": timedelta(days=8)}
    assert status.silent_jobs(st, expected, now) == ["backup"]                 # never reported
    assert status.silent_jobs(st, expected, now + timedelta(hours=2)) == ["backup", "scheduler"]   # 14 h since the beat
