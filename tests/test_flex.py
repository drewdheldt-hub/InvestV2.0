"""The Flex Web Service download: two steps, waits while IB generates, and never lets the token out."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from investtree.data import flex

TOKEN = "secret-token-123"
SENT = ("<FlexStatementResponse><Status>Success</Status><ReferenceCode>777</ReferenceCode>"
        "<Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>"
        "</FlexStatementResponse>")
WAIT = ("<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>"
        "<ErrorMessage>Statement generation in progress.</ErrorMessage></FlexStatementResponse>")
STATEMENT = "<FlexQueryResponse><FlexStatements count='0'/></FlexQueryResponse>"


def test_two_steps_and_waiting_while_ib_generates() -> None:
    answers = iter([SENT, WAIT, STATEMENT])
    urls: list[str] = []

    def get(url: str) -> str:
        urls.append(url)
        return next(answers)

    assert flex.fetch(TOKEN, "42", get, wait_seconds=0) == STATEMENT
    assert "SendRequest?t=secret-token-123&q=42&v=3" in urls[0]
    assert "GetStatement?t=secret-token-123&q=777&v=3" in urls[1]


@pytest.mark.parametrize("answers", [
    ["<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode>"
     "<ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"],
    [SENT, "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1003</ErrorCode>"
           "<ErrorMessage>Statement is not available.</ErrorMessage></FlexStatementResponse>"],
    [SENT, "<html>gateway error</html>"],
    [SENT, WAIT, WAIT],
])
def test_failures_never_mention_the_token(answers: list[str]) -> None:
    it = iter(answers)
    with pytest.raises(flex.FlexServiceError) as e:
        flex.fetch(TOKEN, "42", lambda url: next(it), wait_seconds=0, tries=2)
    assert TOKEN not in str(e.value)


def test_a_network_error_hides_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    import urllib.request

    def boom(*a: object, **k: object) -> object:
        raise urllib.error.URLError(f"cannot reach {flex.SEND}?t={TOKEN}")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(flex.FlexServiceError) as e:
        flex.fetch(TOKEN, "42", wait_seconds=0)
    assert TOKEN not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


def test_missing_token_or_query() -> None:
    with pytest.raises(flex.FlexServiceError):
        flex.fetch("", "42", lambda url: SENT)


def test_save_names_the_file_by_time_and_query(tmp_path: Path) -> None:
    p = flex.save(tmp_path, STATEMENT, "42", now=datetime(2026, 10, 13, 6, 30))
    assert p == tmp_path / "holdings" / "flex" / "2026-10-13_063000_42.xml"
    assert p.read_text(encoding="utf-8") == STATEMENT
