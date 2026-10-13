from datetime import time

import pytest
from pydantic import ValidationError

from investtree.config import Paths, Schedule, load_agents, load_schedule


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
