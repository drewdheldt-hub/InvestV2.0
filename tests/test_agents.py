"""The agents: permissions enforced in code, daily token caps, and the operator's cited briefing."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from investtree.agents import usage
from investtree.agents.operator import build_digest, citations_valid, plain_briefing, write_briefing
from investtree.agents.permissions import ALLOWED, OWNER_ONLY, Action, PermissionDenied, Role, require
from investtree.config import Paths, RoleConfig
from investtree.ops import status

NOW = datetime(2026, 10, 13, 17, 30)
CFG = RoleConfig(model="claude-opus-5-5", daily_token_cap=20000)


# ------------------------------------------------------------------ permissions
def test_no_role_can_take_an_owner_only_action() -> None:
    for role in Role:
        assert not (ALLOWED[role] & OWNER_ONLY), role
        for action in OWNER_ONLY:
            with pytest.raises(PermissionDenied):
                require(role, action)


def test_the_operator_reads_and_reports_but_cannot_change_code_or_run_research() -> None:
    require(Role.OPERATOR, Action.READ)
    require(Role.OPERATOR, Action.WRITE_BRIEFING)
    for action in (Action.CHANGE_CODE, Action.RUN_EXPLORATORY, Action.DRAFT_REGISTRATION):
        with pytest.raises(PermissionDenied):
            require(Role.OPERATOR, action)


def test_every_check_is_audited_allowed_or_not(paths: Paths) -> None:
    audit = paths.var / "audit" / "agents.jsonl"
    require(Role.CRITIC, Action.READ, audit, "results")
    with pytest.raises(PermissionDenied):
        require(Role.CRITIC, Action.PLACE_ORDER, audit, "SPY")
    rows = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines()]
    assert [(r["action"], r["allowed"]) for r in rows] == [("read", True), ("place_order", False)]


# ------------------------------------------------------------------ usage caps
def test_a_role_stops_at_its_daily_cap(paths: Paths) -> None:
    day = date(2026, 10, 13)
    usage.add(paths.usage, "operator", 19_000, day)
    usage.check(paths.usage, "operator", 20_000, day, planned=500)
    with pytest.raises(usage.OverBudget):
        usage.check(paths.usage, "operator", 20_000, day, planned=2_000)
    usage.check(paths.usage, "operator", 20_000, date(2026, 10, 14), planned=2_000)    # a new day
    with pytest.raises(usage.OverBudget, match="no model budget"):
        usage.check(paths.usage, "critic", 0, day)


# ------------------------------------------------------------------ briefing
def feed_all_green(paths: Paths) -> None:
    status.record(paths.status_feed, "scheduler", "note", "heartbeat", ts="2026-10-13T12:00:00")
    status.record(paths.status_feed, "backup", "started", ts="2026-10-11T18:00:00")
    status.record(paths.status_feed, "backup", "finished", ts="2026-10-11T18:04:00")
    status.record(paths.status_feed, "data-update", "finished", ts="2026-10-12T16:40:00")
    status.record(paths.status_feed, "gateway-morning", "finished", ts="2026-10-13T07:15:30")


def test_all_green_briefing_is_quiet(paths: Paths) -> None:
    feed_all_green(paths)
    d = build_digest(paths, NOW)
    assert d.green
    assert "All green" in plain_briefing(d)


def test_a_failure_is_reported_with_its_log_and_cited(paths: Paths) -> None:
    feed_all_green(paths)
    status.record(paths.status_feed, "operator-briefing", "failed", "exit code 1", ts="2026-10-13T09:00:00",
                  log="var/logs/operator-briefing_2026-10-13.log")
    d = build_digest(paths, NOW)
    text = plain_briefing(d)
    assert "**operator-briefing failed**: exit code 1" in text
    assert "[2026-10-13T09:00:00 operator-briefing failed]" in text and citations_valid(text, d)


def test_a_silent_job_is_named(paths: Paths) -> None:
    status.record(paths.status_feed, "scheduler", "note", "heartbeat", ts="2026-10-13T12:00:00")
    status.record(paths.status_feed, "data-update", "finished", ts="2026-10-08T16:40:00")   # 5 days ago
    status.record(paths.status_feed, "gateway-morning", "finished", ts="2026-10-13T07:15:30")
    d = build_digest(paths, NOW)                     # backup has never reported
    assert d.silent == ["backup", "data-update"] and "backup has not reported" in plain_briefing(d)


def test_a_model_briefing_with_an_invented_citation_is_rejected(paths: Paths) -> None:
    feed_all_green(paths)
    status.record(paths.status_feed, "backup", "failed", "disk full", ts="2026-10-13T09:00:00")
    seen: list[str] = []

    def honest(model: str, system: str, prompt: str) -> tuple[str, int]:
        seen.append(prompt)
        return "Backup failed: disk full [2026-10-13T09:00:00 backup failed]", 900

    def invented(model: str, system: str, prompt: str) -> tuple[str, int]:
        return "Everything failed [2026-10-13T08:00:00 everything failed]", 900

    good = write_briefing(paths, CFG, NOW, honest)
    assert good.by_model and "disk full" in good.path.read_text(encoding="utf-8")
    assert "<data>" in seen[0] and "</data>" in seen[0]        # feed text is passed as data, not instructions
    bad = write_briefing(paths, CFG, NOW, invented)
    assert not bad.by_model and "**backup failed**" in bad.path.read_text(encoding="utf-8")
    assert usage.used(paths.usage, "operator", NOW.date()) == 1800


def test_no_model_call_once_the_cap_is_used_up(paths: Paths) -> None:
    feed_all_green(paths)
    usage.add(paths.usage, "operator", 20_000, NOW.date())

    def must_not_run(model: str, system: str, prompt: str) -> tuple[str, int]:
        raise AssertionError("the model was called over budget")

    b = write_briefing(paths, CFG, NOW, must_not_run)
    assert not b.by_model and b.path.name == "2026-10-13.md"


def test_a_refusal_or_empty_reply_falls_back_to_the_plain_briefing(paths: Paths) -> None:
    feed_all_green(paths)
    b = write_briefing(paths, CFG, NOW, lambda m, s, p: ("", 300))
    assert not b.by_model and "All green" in b.path.read_text(encoding="utf-8")
