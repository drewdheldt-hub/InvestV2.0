from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from investtree.config import (
    AccountsConfig,
    OverlayConfig,
    Paths,
    Schedule,
    load_accounts,
    load_agents,
    load_overlays,
    load_schedule,
    load_yaml,
)

REPO = Path(__file__).resolve().parents[1]


def test_the_real_config_files_load(paths: Paths) -> None:
    sch = load_schedule(paths)
    assert {j.name for j in sch.jobs} >= {"operator-briefing", "backup", "backup-verify"}
    assert next(j for j in sch.jobs if j.name == "backup").at == time(18, 0)
    agents = load_agents(paths)
    assert agents.critic.daily_token_cap == 0          # the critic starts with the research phase


def test_a_bad_job_fails_when_loaded_naming_the_field() -> None:
    with pytest.raises(ValidationError, match="days"):
        Schedule.model_validate({"jobs": [{"name": "x", "command": ["a"], "at": "17:00", "days": ["someday"]}]})
    with pytest.raises(ValidationError, match="unique"):
        Schedule.model_validate({"jobs": [{"name": "x", "command": ["a"], "at": "17:00"},
                                          {"name": "x", "command": ["b"], "at": "18:00"}]})
    with pytest.raises(ValidationError):
        Schedule.model_validate({"jobs": [{"name": "Bad Name", "command": ["a"], "at": "17:00"}]})


def test_only_data_var_and_reports_live_outside_git(paths: Paths) -> None:
    assert [p.name for p in paths.not_in_git()] == ["data", "var", "reports"]


def test_overlay_jobs_name_overlays_that_exist(paths: Paths) -> None:
    names = {o.name for o in load_overlays(paths).overlays}
    for job in load_schedule(paths).jobs:
        if job.command[3:4] == ["overlay"]:
            assert job.command[5] in names, job.name


def test_the_v1_overlay_does_not_send_orders_from_v2_yet(paths: Paths) -> None:
    """v1 still runs spy_voltarget on the same paper account; v2 sending too would double every trade."""
    assert not load_overlays(paths).get("spy_voltarget").execute
    assert not any(j.enabled for j in load_schedule(paths).jobs if j.name in ("overlay-paper", "overlay-fills"))


def test_overlay_weights_are_checked() -> None:
    assert OverlayConfig(name="x", weights={"spy": 0.6, "tlt": 0.4}).weights == {"SPY": 0.6, "TLT": 0.4}
    for bad in ({"SPY": 0.7, "TLT": 0.4}, {"SPY": -0.1, "TLT": 0.5}, {"SPY": 0.0}):
        with pytest.raises(ValidationError, match="weights"):
            OverlayConfig(name="x", weights=bad)


def test_accounts_are_optional_and_the_example_is_valid(paths: Paths) -> None:
    assert load_accounts(paths).accounts == []
    example = AccountsConfig.model_validate(load_yaml(REPO / "config" / "accounts.example.yaml"))
    assert example.taxable_ids == {"U1234567"} and example.get("U7654321") is not None
    with pytest.raises(ValidationError, match="taxable"):
        AccountsConfig.model_validate({"accounts": [{"id": "U1"}]})      # taxable must be stated, not assumed
