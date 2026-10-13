"""Where things live, and the typed configuration files in config/.

A bad config file fails when it is loaded, with the field that is wrong, not halfway through a job.
"""

from __future__ import annotations

import os
import re
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: list[Day] = ["mon", "tue", "wed", "thu", "fri"]


def project_root() -> Path:
    """The project folder: INVESTTREE_HOME if set (tests use this), else the folder holding src/."""
    env = os.environ.get("INVESTTREE_HOME")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[2]


class Paths:
    """Every location the code writes to. Only data/, var/ and reports/ hold files git does not."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or project_root()
        self.config = self.root / "config"
        self.data = self.root / "data"
        self.var = self.root / "var"
        self.reports = self.root / "reports"
        self.ledger = self.root / "ledger"
        self.registry = self.root / "registry"
        self.status_feed = self.var / "status" / "feed.jsonl"
        self.logs = self.var / "logs"
        self.usage = self.var / "usage"
        self.briefings = self.reports / "briefings"

    def not_in_git(self) -> list[Path]:
        """What the backup must cover: everything the project keeps outside git."""
        return [self.data, self.var, self.reports]


# ------------------------------------------------------------------ schedule
class Job(BaseModel):
    """One scheduled job: a command run on the given days at a local time."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    command: list[str] = Field(min_length=1)
    days: list[Day] = Field(default_factory=lambda: list(WEEKDAYS))
    at: time
    timeout_minutes: int = Field(default=60, ge=1, le=24 * 60)
    catch_up_hours: float = Field(default=2.0, ge=0, description="still run if the scheduler starts this late")
    enabled: bool = True

    @field_validator("at", mode="before")
    @classmethod
    def _parse_time(cls, v: object) -> object:
        if isinstance(v, str) and re.fullmatch(r"\d{1,2}:\d{2}", v):
            h, m = (int(x) for x in v.split(":"))
            return time(h, m)
        return v


class Schedule(BaseModel):
    jobs: list[Job]

    @field_validator("jobs")
    @classmethod
    def _unique(cls, jobs: list[Job]) -> list[Job]:
        names = [j.name for j in jobs]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"job names must be unique: {sorted(dupes)}")
        return jobs


# ------------------------------------------------------------------ agents
class RoleConfig(BaseModel):
    model: str
    daily_token_cap: int = Field(ge=0, description="input plus output tokens per day; 0 = never call the model")


class AgentsConfig(BaseModel):
    operator: RoleConfig
    critic: RoleConfig


def load_yaml(path: Path) -> object:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_schedule(paths: Paths | None = None) -> Schedule:
    paths = paths or Paths()
    return Schedule.model_validate(load_yaml(paths.config / "schedule.yaml"))


def load_agents(paths: Paths | None = None) -> AgentsConfig:
    paths = paths or Paths()
    return AgentsConfig.model_validate(load_yaml(paths.config / "agents.yaml"))
