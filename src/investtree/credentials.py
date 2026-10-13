"""Credentials, kept in the Windows credential store (or the platform's keyring), never in files.

The owner stores a secret once from a terminal:
    uv run python -m investtree credentials set anthropic-api-key
which prompts without echoing. Code reads it with get(); it is never printed, logged or written to disk.
An environment variable of the same name in upper case (ANTHROPIC_API_KEY) takes precedence, for CI.
"""

from __future__ import annotations

import contextlib
import getpass
import os

SERVICE = "investtree2"

# The secrets the project knows about. Anything else is refused, so a typo cannot create a stray entry.
KNOWN = {
    "anthropic-api-key": "Claude API key for the operator and critic agents",
}


def _env_name(name: str) -> str:
    return name.upper().replace("-", "_")


def get(name: str) -> str | None:
    """The secret, or None when it is not set. Never logs the value."""
    if name not in KNOWN:
        raise KeyError(f"unknown secret {name!r}; known: {sorted(KNOWN)}")
    env = os.environ.get(_env_name(name))
    if env:
        return env
    import keyring

    return keyring.get_password(SERVICE, name)


def is_set(name: str) -> bool:
    return bool(get(name))


def set_interactively(name: str) -> None:
    """Prompt for the secret without echoing it, and store it in the credential store."""
    if name not in KNOWN:
        raise KeyError(f"unknown secret {name!r}; known: {sorted(KNOWN)}")
    value = getpass.getpass(f"{KNOWN[name]} (input hidden): ")
    if not value.strip():
        raise ValueError("nothing entered; the secret was not changed")
    import keyring

    keyring.set_password(SERVICE, name, value.strip())


def delete(name: str) -> None:
    if name not in KNOWN:
        raise KeyError(f"unknown secret {name!r}")
    import keyring

    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(SERVICE, name)
