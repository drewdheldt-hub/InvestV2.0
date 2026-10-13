"""Each role's daily token cap: model calls stop for the day once a role has used its share.

Usage is kept per role and day in var/usage/<date>.json, and the operator reports it in its briefing.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


class OverBudget(RuntimeError):
    pass


def _file(usage_dir: Path, day: date) -> Path:
    return usage_dir / f"{day.isoformat()}.json"


def used(usage_dir: Path, role: str, day: date) -> int:
    try:
        data = json.loads(_file(usage_dir, day).read_text(encoding="utf-8"))
        return int(data.get(role, 0))
    except (OSError, ValueError):
        return 0


def check(usage_dir: Path, role: str, cap: int, day: date, planned: int = 0) -> None:
    """Raise OverBudget when the role has no room left today for a call of about `planned` tokens."""
    if cap <= 0:
        raise OverBudget(f"the {role} role has no model budget (daily_token_cap is 0)")
    spent = used(usage_dir, role, day)
    if spent + planned > cap:
        raise OverBudget(f"the {role} role has used {spent:,} of its {cap:,} tokens today")


def add(usage_dir: Path, role: str, tokens: int, day: date) -> int:
    f = _file(usage_dir, day)
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[role] = int(data.get(role, 0)) + int(tokens)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(f)
    return int(data[role])
