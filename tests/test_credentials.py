import subprocess
import sys
from pathlib import Path

import pytest

from investtree import credentials

REPO = Path(__file__).resolve().parents[1]


def test_unknown_names_are_refused() -> None:
    with pytest.raises(KeyError):
        credentials.get("typo-api-key")


def test_an_environment_variable_wins_and_the_store_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda service, name: "from-store")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert credentials.get("anthropic-api-key") == "from-store"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert credentials.get("anthropic-api-key") == "from-env"


def test_setting_prompts_without_echo_and_refuses_an_empty_value(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    stored: dict[str, str] = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, n, v: stored.update({n: v}))
    monkeypatch.setattr(credentials.getpass, "getpass", lambda prompt: "  sk-test  ")
    credentials.set_interactively("anthropic-api-key")
    assert stored == {"anthropic-api-key": "sk-test"}
    monkeypatch.setattr(credentials.getpass, "getpass", lambda prompt: "   ")
    with pytest.raises(ValueError):
        credentials.set_interactively("anthropic-api-key")


# Patterns that look like a real credential. A committed file matching one fails the build.
SECRET_PATTERNS = [
    r"sk-ant-[A-Za-z0-9_-]{20,}",                    # Anthropic API key
    r"IbPassword\s*=\s*\S+",                         # IBC's password line, filled in
    r"(?i)(password|passwd|api[_-]?key)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]


def test_no_credential_is_committed() -> None:
    import re

    tracked = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=False).stdout.split()
    files = [REPO / f for f in tracked] or [p for p in REPO.rglob("*") if p.is_file() and ".venv" not in p.parts
                                            and ".git" not in p.parts]
    hits = []
    for f in files:
        if f.suffix in (".png", ".jpg", ".ico", ".woff2") or not f.exists() or f.name == Path(__file__).name:
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        hits += [f"{f.relative_to(REPO)}: {p}" for p in SECRET_PATTERNS if re.search(p, text)]
    assert not hits, "possible credentials in tracked files:\n" + "\n".join(hits)


def test_the_check_command_reports_without_revealing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-shown")
    out = subprocess.run([sys.executable, "-m", "investtree", "credentials", "check", "anthropic-api-key"],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == "anthropic-api-key: set"
    assert "sk-not-shown" not in out
