"""Who may do what, enforced in code. Mirrors the blueprint's permissions table.

Every function an agent can call checks require(role, action) first, so a role cannot reach an action by
being asked nicely. Every check, allowed or refused, goes to the audit log (var/audit/agents.jsonl).
Actions that only the owner may take (merging, registering a test, approving a repair, any order) are given
to no agent role at all.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class Role(StrEnum):
    OPERATOR = "operator"
    BUILDER = "builder"
    RESEARCH_ASSISTANT = "research_assistant"
    CRITIC = "critic"


class Action(StrEnum):
    READ = "read"                              # data, results, logs, the status feed
    WRITE_BRIEFING = "write_briefing"
    PROPOSE_REPAIR = "propose_repair"
    RUN_EXPLORATORY = "run_exploratory"
    DRAFT_REGISTRATION = "draft_registration"
    WRITE_CRITIQUE = "write_critique"
    CHANGE_CODE = "change_code"                # by pull request; merging is the owner's
    PROPOSE_SAFETY_CHANGE = "propose_safety_change"
    # owner only: listed so a check can name them, never granted to a role
    MERGE = "merge"
    REGISTER_TEST = "register_test"
    APPROVE_REPAIR = "approve_repair"
    PLACE_ORDER = "place_order"


ALLOWED: dict[Role, frozenset[Action]] = {
    Role.OPERATOR: frozenset({Action.READ, Action.WRITE_BRIEFING, Action.PROPOSE_REPAIR}),
    Role.BUILDER: frozenset({Action.READ, Action.CHANGE_CODE, Action.PROPOSE_SAFETY_CHANGE}),
    Role.RESEARCH_ASSISTANT: frozenset({Action.READ, Action.RUN_EXPLORATORY, Action.DRAFT_REGISTRATION}),
    Role.CRITIC: frozenset({Action.READ, Action.WRITE_CRITIQUE}),
}
OWNER_ONLY = frozenset({Action.MERGE, Action.REGISTER_TEST, Action.APPROVE_REPAIR, Action.PLACE_ORDER})


class PermissionDenied(PermissionError):
    pass


def require(role: Role, action: Action, audit: Path | None = None, what: str = "") -> None:
    """Raise PermissionDenied unless the role may take the action; log the check either way."""
    allowed = action in ALLOWED[role]
    if audit is not None:
        audit.parent.mkdir(parents=True, exist_ok=True)
        with open(audit, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "role": role.value,
                                 "action": action.value, "allowed": allowed, "what": what}) + "\n")
    if not allowed:
        raise PermissionDenied(f"the {role.value} role may not {action.value.replace('_', ' ')}")
