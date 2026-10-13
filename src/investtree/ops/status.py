"""The status feed: one append-only file of events that every scheduled job reports into.

The dashboard and the operator agent both read it, so a failed or silent job shows up in one place.
Each event has an id ("<time> <job> <kind>") that a briefing cites, so its claims can be checked.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

Kind = Literal["started", "finished", "failed", "skipped", "note"]


@dataclass(frozen=True)
class Event:
    ts: str                       # local time, ISO, seconds
    job: str
    kind: Kind
    text: str = ""
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.ts} {self.job} {self.kind}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record(feed: Path, job: str, kind: Kind, text: str = "", ts: str | None = None, **detail: object) -> Event:
    """Append one event. A single write of one line, so concurrent writers do not interleave."""
    ev = Event(ts=ts or _now(), job=job, kind=kind, text=text, detail=dict(detail))
    feed.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(ev), sort_keys=True) + "\n"
    with open(feed, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    return ev


def read(feed: Path, since: datetime | None = None) -> Iterator[Event]:
    """Events oldest first; a damaged line is skipped rather than stopping the reader."""
    if not feed.exists():
        return
    with open(feed, encoding="utf-8") as fh:
        for line in fh:
            try:
                d = json.loads(line)
                ev = Event(ts=d["ts"], job=d["job"], kind=d["kind"], text=d.get("text", ""),
                           detail=d.get("detail") or {})
            except (ValueError, KeyError, TypeError):
                continue
            if since is None or datetime.fromisoformat(ev.ts) >= since:
                yield ev


@dataclass(frozen=True)
class JobState:
    job: str
    last: Event                  # the latest event of any kind, notes (such as heartbeats) included
    last_run: Event | None       # the latest started, finished, failed or skipped event
    last_finished: Event | None
    running: bool
    failed: bool


def latest(feed: Path, since: datetime | None = None) -> dict[str, JobState]:
    """Each job's most recent event, and whether it is running or its last run failed. A note (a heartbeat)
    counts as being seen but does not change whether the job is running or failed."""
    seen: dict[str, Event] = {}
    run: dict[str, Event] = {}
    done: dict[str, Event] = {}
    for ev in read(feed, since):
        seen[ev.job] = ev
        if ev.kind == "note":
            continue
        run[ev.job] = ev
        if ev.kind in ("finished", "failed", "skipped"):
            done[ev.job] = ev
    return {j: JobState(job=j, last=e, last_run=run.get(j), last_finished=done.get(j),
                        running=run.get(j) is not None and run[j].kind == "started",
                        failed=run.get(j) is not None and run[j].kind == "failed") for j, e in seen.items()}


def silent_jobs(states: dict[str, JobState], expected: dict[str, timedelta], now: datetime) -> list[str]:
    """Jobs that should have reported within their expected interval but did not (or never have)."""
    out = []
    for job, within in expected.items():
        st = states.get(job)
        if st is None or now - datetime.fromisoformat(st.last.ts) > within:
            out.append(job)
    return sorted(out)
