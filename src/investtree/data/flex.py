"""Download Flex statements from IB's Flex Web Service and save them under data/holdings/flex/.

Read-only by design: the Flex Web Service only produces reports, it cannot trade. It needs a Flex token
(Client Portal > Performance & Reports > Flex Queries > Flex Web Service Configuration), which the owner
stores once with
    uv run python -m investtree credentials set ib-flex-token
and the id of a saved Activity Flex Query (config/accounts.local.yaml). The token is passed in, never
logged, and kept out of every error message (the request URLs carry it).

Two steps, as IB documents them: SendRequest returns a reference code, and GetStatement returns the
statement once IB has generated it (until then it answers "generation in progress").
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

SEND = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
VERSION = "3"
IN_PROGRESS = {"1019", "1021"}           # statement generation in progress; try again shortly

Get = Callable[[str], str]               # url -> response text


class FlexServiceError(RuntimeError):
    """IB refused or failed the request. The message never includes the token."""


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "investtree/2"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body: bytes = r.read()
            return body.decode("utf-8")
    except OSError as e:                  # URLError and HTTPError carry the URL, and with it the token
        raise FlexServiceError(f"could not reach the Flex Web Service ({type(e).__name__})") from None


def _url(base: str, **params: str) -> str:
    return f"{base}?{urllib.parse.urlencode(params)}"


def _response(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        raise FlexServiceError("the Flex Web Service answered with something that is not XML") from None


def fetch(token: str, query_id: str, get: Get = _http_get, wait_seconds: float = 5.0, tries: int = 12) -> str:
    """The statement XML for a saved Flex query."""
    if not token or not query_id:
        raise FlexServiceError("a Flex token and a query id are both needed")
    sent = _response(get(_url(SEND, t=token, q=query_id, v=VERSION)))
    if (sent.findtext("Status") or "") != "Success":
        raise FlexServiceError(f"Flex request refused: {sent.findtext('ErrorCode')} {sent.findtext('ErrorMessage')}")
    ref, base = sent.findtext("ReferenceCode") or "", sent.findtext("Url") or ""
    if not ref or not base.startswith("https://"):
        raise FlexServiceError("the Flex Web Service gave no reference code or statement address")
    for attempt in range(tries):
        text = get(_url(base, t=token, q=ref, v=VERSION))
        if "<FlexQueryResponse" in text[:500]:
            return text
        r = _response(text)
        code = r.findtext("ErrorCode") or ""
        if code not in IN_PROGRESS:
            raise FlexServiceError(f"Flex statement not returned: {code} {r.findtext('ErrorMessage')}")
        if attempt < tries - 1:
            time.sleep(wait_seconds)
    raise FlexServiceError(f"the statement was still being generated after {tries} tries")


def save(data: Path, xml: str, query_id: str, now: datetime | None = None) -> Path:
    """Write a statement to data/holdings/flex/<time>_<query>.xml and return its path."""
    folder = data / "holdings" / "flex"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{(now or datetime.now()):%Y-%m-%d_%H%M%S}_{query_id}.xml"
    p.write_text(xml, encoding="utf-8")
    return p
