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
        self.overlay_state = self.var / "overlay"
        self.stop_file = self.root / "STOP"            # while it exists, nothing sends an order

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


# ------------------------------------------------------------------ overlays
def valid_weights(w: dict[str, float]) -> dict[str, float]:
    """Upper-case symbols; weights non-negative, adding up to more than 0 and at most 1 (the rest is cash)."""
    w = {s.upper(): v for s, v in w.items()}
    if any(v < 0 for v in w.values()) or not 0 < sum(w.values()) <= 1 + 1e-9:
        raise ValueError("weights must be non-negative and add up to more than 0 and at most 1")
    return w


class OverlayConfig(BaseModel):
    """One paper overlay runner: a portfolio, its policy, and the slice of the paper account it manages."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$")
    weights: dict[str, float] = Field(min_length=1, description="symbol -> target weight, adding up to at most 1")
    policy: Literal["rebalance", "vol_target", "trend"] = "vol_target"
    capital: float = Field(default=100_000.0, gt=0, description="the slice of the account this runner manages")
    execute: bool = Field(default=False, description="send orders (paper); False decides and records only")
    min_trade: float = Field(default=0.005, ge=0, lt=1, description="skip trades below this share of capital")
    adopt: bool = Field(default=False, description="take over positions already in the account")
    client_id: int = Field(default=61, ge=0)
    history_days: int = Field(default=320, ge=30, description="trading days of closes (the trend filter needs 200)")

    @field_validator("weights")
    @classmethod
    def _weights(cls, w: dict[str, float]) -> dict[str, float]:
        return valid_weights(w)


class OverlaysConfig(BaseModel):
    overlays: list[OverlayConfig] = Field(default_factory=list)

    def get(self, name: str) -> OverlayConfig:
        for o in self.overlays:
            if o.name == name:
                return o
        raise KeyError(f"no overlay named {name!r} in config/overlays.yaml; known: {[o.name for o in self.overlays]}")


# ------------------------------------------------------------------ real accounts
class AccountOverlay(BaseModel):
    """The overlay that proposes trades for a real account. Suggestions only: real accounts never get orders."""

    weights: dict[str, float] = Field(min_length=1)
    policy: Literal["rebalance", "vol_target", "trend"] = "vol_target"
    min_trade: float = Field(default=0.005, ge=0, lt=1)

    @field_validator("weights")
    @classmethod
    def _weights(cls, w: dict[str, float]) -> dict[str, float]:
        return valid_weights(w)


class RealAccount(BaseModel):
    id: str = Field(min_length=1)
    name: str = ""
    taxable: bool = Field(description="taxes are modelled for taxable accounts only")
    overlay: AccountOverlay | None = Field(default=None, description="none: risk report only, no suggestions")


class TaxConfig(BaseModel):
    short_term_rate: float = Field(default=0.37, ge=0, le=1)
    long_term_rate: float = Field(default=0.15, ge=0, le=1)
    harvest_min_loss: float = Field(default=1000.0, ge=0)
    harvest_min_pct: float = Field(default=0.05, ge=0, le=1)
    harvest_pairs: dict[str, str] = Field(default_factory=dict, description="symbol -> the owner's replacement")


class AccountsConfig(BaseModel):
    """config/accounts.local.yaml: the owner's real accounts. Never in git (it names real account numbers)."""

    flex_query_ids: list[str] = Field(default_factory=list)
    accounts: list[RealAccount] = Field(default_factory=list)
    tax: TaxConfig = Field(default_factory=TaxConfig)

    def get(self, account_id: str) -> RealAccount | None:
        return next((a for a in self.accounts if a.id == account_id), None)

    @property
    def taxable_ids(self) -> set[str]:
        return {a.id for a in self.accounts if a.taxable}


def load_yaml(path: Path) -> object:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_schedule(paths: Paths | None = None) -> Schedule:
    paths = paths or Paths()
    return Schedule.model_validate(load_yaml(paths.config / "schedule.yaml"))


def load_agents(paths: Paths | None = None) -> AgentsConfig:
    paths = paths or Paths()
    return AgentsConfig.model_validate(load_yaml(paths.config / "agents.yaml"))


def load_overlays(paths: Paths | None = None) -> OverlaysConfig:
    paths = paths or Paths()
    return OverlaysConfig.model_validate(load_yaml(paths.config / "overlays.yaml"))


def load_accounts(paths: Paths | None = None) -> AccountsConfig:
    """The owner's real accounts, or an empty configuration when config/accounts.local.yaml does not exist."""
    paths = paths or Paths()
    p = paths.config / "accounts.local.yaml"
    return AccountsConfig.model_validate(load_yaml(p) or {}) if p.exists() else AccountsConfig()
