"""One scheduler process instead of many PowerShell scripts and Task Scheduler entries.

It reads config/schedule.yaml, runs each job at its local time on its days (at below-normal priority, with a
timeout), writes every start and finish to the status feed, and keeps each job's output in var/logs/.
A job missed while the computer was off still runs if the scheduler starts within the job's catch_up_hours.
Only one scheduler may run at a time.

    investtree scheduler run        # normally started at logon by scripts/register_scheduler.ps1
    investtree scheduler due        # what would run now, without running it
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time as _time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import DAYS, Job, Paths, Schedule
from . import status

LOCK_PORT = 8797            # a local port held while the scheduler runs: a second one cannot bind it
BELOW_NORMAL = 0x00004000   # Windows BELOW_NORMAL_PRIORITY_CLASS
HEARTBEAT_SECONDS = 6 * 3600
SECONDS_PER_MINUTE = 60.0       # tests shorten it to check the timeout quickly


def scheduled_at(job: Job, day: date) -> datetime:
    return datetime.combine(day, job.at)


def is_due(job: Job, now: datetime, last_run_day: str | None) -> bool:
    """Due when today is one of its days, its time has passed by no more than catch_up_hours, and it has not
    already run today."""
    if not job.enabled or DAYS[now.weekday()] not in job.days:
        return False
    if last_run_day == now.date().isoformat():
        return False
    at = scheduled_at(job, now.date())
    return at <= now <= at + timedelta(hours=job.catch_up_hours)


def expand(command: list[str]) -> list[str]:
    """{python} is the interpreter running the scheduler, so jobs use the project's environment."""
    return [sys.executable if part == "{python}" else part for part in command]


def run_job(job: Job, paths: Paths) -> int:
    """Run one job to completion: status events, a log file, below-normal priority, a timeout."""
    paths.logs.mkdir(parents=True, exist_ok=True)
    log = paths.logs / f"{job.name}_{datetime.now():%Y-%m-%d}.log"
    status.record(paths.status_feed, job.name, "started", log=log.relative_to(paths.root).as_posix())
    flags = BELOW_NORMAL if sys.platform == "win32" else 0
    try:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"--- {datetime.now().isoformat(timespec='seconds')} {' '.join(job.command)}\n")
            fh.flush()
            proc = subprocess.Popen(expand(job.command), cwd=paths.root, stdout=fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=flags)
            try:
                code = proc.wait(timeout=job.timeout_minutes * SECONDS_PER_MINUTE)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                status.record(paths.status_feed, job.name, "failed", f"timed out after {job.timeout_minutes} min",
                              log=log.relative_to(paths.root).as_posix())
                return -1
    except OSError as e:
        status.record(paths.status_feed, job.name, "failed", f"could not start: {e}")
        return -1
    kind: status.Kind = "finished" if code == 0 else "failed"
    status.record(paths.status_feed, job.name, kind, "" if code == 0 else f"exit code {code}",
                  exit=code, log=log.relative_to(paths.root).as_posix())
    return code


class Scheduler:
    def __init__(self, schedule: Schedule, paths: Paths,
                 run: Callable[[Job, Paths], int] = run_job,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self.schedule, self.paths, self.run, self.clock = schedule, paths, run, clock
        self.state_file = paths.var / "scheduler" / "last_run.json"
        self.threads: dict[str, threading.Thread] = {}

    def _last_runs(self) -> dict[str, str]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _mark(self, job: str, day: str) -> None:
        runs = self._last_runs()
        runs[job] = day
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(runs, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_file)

    def due(self, now: datetime | None = None) -> list[Job]:
        now = now or self.clock()
        runs = self._last_runs()
        return [j for j in self.schedule.jobs if is_due(j, now, runs.get(j.name))]

    def tick(self, wait: bool = False) -> list[str]:
        """Start every due job that is not already running; each job runs in its own thread so a long one
        never delays the others. The day is marked before the job starts, so a crash cannot run it twice."""
        now = self.clock()
        started = []
        for job in self.due(now):
            t = self.threads.get(job.name)
            if t is not None and t.is_alive():
                continue
            self._mark(job.name, now.date().isoformat())
            t = threading.Thread(target=self.run, args=(job, self.paths), name=job.name, daemon=True)
            self.threads[job.name] = t
            t.start()
            started.append(job.name)
        if wait:
            for name in started:
                self.threads[name].join()
        return started

    def run_forever(self, poll_seconds: float = 30.0) -> None:
        lock = acquire_lock()
        if lock is None:
            raise SystemExit("another scheduler is already running")
        status.record(self.paths.status_feed, "scheduler", "note", "scheduler started",
                      jobs=[j.name for j in self.schedule.jobs if j.enabled])
        beat = _time.monotonic()
        try:
            while True:
                self.tick()
                if _time.monotonic() - beat >= HEARTBEAT_SECONDS:     # so a dead scheduler shows as silent
                    status.record(self.paths.status_feed, "scheduler", "note", "heartbeat")
                    beat = _time.monotonic()
                _time.sleep(poll_seconds)
        finally:
            lock.close()


def acquire_lock(port: int = LOCK_PORT) -> socket.socket | None:
    """Hold a local port for as long as the scheduler runs. None when another scheduler already holds it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        return None
    return s


def log_path(paths: Paths, job: str, day: date) -> Path:
    return paths.logs / f"{job}_{day.isoformat()}.log"
