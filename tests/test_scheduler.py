from datetime import datetime, time

import pytest

from investtree.config import Job, Paths, Schedule
from investtree.ops import scheduler, status
from investtree.ops.scheduler import Scheduler, acquire_lock, is_due, run_job

MON_17_35 = datetime(2026, 10, 12, 17, 35)       # a Monday


def job(**kw: object) -> Job:
    base: dict[str, object] = {"name": "brief", "command": ["x"], "at": time(17, 30)}
    return Job.model_validate(base | kw)


def test_due_on_its_days_after_its_time_within_the_catch_up_window() -> None:
    j = job(catch_up_hours=2)
    assert is_due(j, MON_17_35, None)
    assert not is_due(j, datetime(2026, 10, 12, 17, 29), None)          # not yet
    assert not is_due(j, datetime(2026, 10, 12, 19, 31), None)          # missed by more than the window
    assert not is_due(j, datetime(2026, 10, 11, 17, 35), None)          # Sunday is not a default day
    assert not is_due(j, MON_17_35, "2026-10-12")                        # already ran today
    assert not is_due(job(enabled=False), MON_17_35, None)


def test_tick_runs_a_due_job_once_and_marks_the_day_first(paths: Paths) -> None:
    ran: list[str] = []
    sch = Scheduler(Schedule(jobs=[job(), job(name="later", at=time(20, 0))]), paths,
                    run=lambda j, p: ran.append(j.name) or 0, clock=lambda: MON_17_35)
    assert sch.tick(wait=True) == ["brief"]
    assert sch.tick(wait=True) == []                                     # not twice in a day
    assert ran == ["brief"]
    assert Scheduler(Schedule(jobs=[job()]), paths, clock=lambda: MON_17_35).due() == []   # survives a restart


def test_run_job_records_success_failure_and_timeout(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    ok = job(name="ok", command=["{python}", "-c", "print('hello')"])
    bad = job(name="bad", command=["{python}", "-c", "import sys; sys.exit(3)"])
    assert run_job(ok, paths) == 0
    assert run_job(bad, paths) == 3
    st = status.latest(paths.status_feed)
    assert not st["ok"].failed and st["bad"].failed and st["bad"].last.text == "exit code 3"
    assert "hello" in (paths.root / str(st["ok"].last.detail["log"])).read_text(encoding="utf-8")
    # a job that runs past its timeout is killed and reported as failed
    monkeypatch.setattr(scheduler, "SECONDS_PER_MINUTE", 0.5)
    slow = job(name="slow", command=["{python}", "-c", "import time; time.sleep(30)"], timeout_minutes=1)
    assert run_job(slow, paths) == -1
    assert status.latest(paths.status_feed)["slow"].last.text == "timed out after 1 min"


def test_only_one_scheduler_can_hold_the_lock() -> None:
    first = acquire_lock(port=8798)
    assert first is not None
    try:
        assert acquire_lock(port=8798) is None
    finally:
        first.close()
    again = acquire_lock(port=8798)
    assert again is not None
    again.close()
