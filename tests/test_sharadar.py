"""Sharadar setup: access check, waiting for an export, snapshots that are never overwritten, and an API key
that never appears in a message."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from investtree.data import sharadar

KEY = "secret-key-456"


def answer(body: object, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(body).encode()


DENIED = answer({"quandl_error": {"code": "QEPx04", "message": "You do not have permission to view this dataset."}},
                403)


def test_check_reports_each_table_and_never_the_key() -> None:
    def get(url: str) -> tuple[int, bytes]:
        if "/SF1.json" in url:
            return DENIED
        return answer({"datatable": {"data": [[1]], "columns": [{"name": "ticker"}, {"name": "date"}]}})

    rows = {a.table: a for a in sharadar.check(KEY, ("SEP", "SF1"), get)}
    assert rows["SEP"].ok and rows["SEP"].detail == "2 columns"
    assert not rows["SF1"].ok and "QEPx04" in rows["SF1"].detail and KEY not in rows["SF1"].detail


def test_no_key_is_refused_before_any_request() -> None:
    with pytest.raises(sharadar.SharadarError, match="credentials set"):
        sharadar.check("", ("SEP",), lambda url: pytest.fail("no request expected"))


def test_export_waits_until_the_file_is_fresh() -> None:
    answers = iter([
        answer({"datatable_bulk_download": {"file": {"link": None, "status": "creating"}}}),
        answer({"datatable_bulk_download": {"file": {"link": "https://s3/x.zip", "status": "fresh",
                                                     "data_snapshot_time": "2026-10-12 23:01:02 UTC"}}}),
    ])
    urls: list[str] = []

    def get(url: str) -> tuple[int, bytes]:
        urls.append(url)
        return next(answers)

    assert sharadar.export_link(KEY, "SEP", get, wait_seconds=0) == ("https://s3/x.zip", "2026-10-12 23:01:02 UTC")
    assert "qopts.export=true" in urls[0]


def test_export_errors_never_include_the_key() -> None:
    with pytest.raises(sharadar.SharadarError) as e:
        sharadar.export_link(KEY, "SEP", lambda url: DENIED, wait_seconds=0)
    assert KEY not in str(e.value)
    with pytest.raises(sharadar.SharadarError) as e:
        sharadar.export_link(KEY, "SEP", lambda url: (502, b"<html>bad gateway</html>"), wait_seconds=0)
    assert KEY not in str(e.value)


def test_a_network_error_hides_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    import urllib.request

    def boom(*a: object, **k: object) -> object:
        raise urllib.error.URLError(f"cannot reach {sharadar.API}/SEP.json?api_key={KEY}")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    [row] = sharadar.check(KEY, ("SEP",))
    assert not row.ok and "URLError" in row.detail and KEY not in row.detail


def test_store_zip_makes_a_fingerprinted_snapshot_and_never_overwrites(tmp_path: Path) -> None:
    z = tmp_path / "SEP.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("SHARADAR_SEP.csv", "ticker,date,closeadj\nAAPL,2026-10-09,250.1\nXTO,2009-06-25,40.2\n")
    when = datetime(2026, 10, 13, 6, 0)
    s = sharadar.store_zip(tmp_path, "SEP", z, "2026-10-12 23:01:02 UTC", now=when)
    assert s.rows == 2 and len(s.sha256) == 64
    assert pl.read_parquet(s.path)["ticker"].to_list() == ["AAPL", "XTO"]      # delisted tickers kept
    [entry] = sharadar.read_manifest(tmp_path)["SEP"]
    assert entry["sha256"] == s.sha256 and entry["vendor_time"] == "2026-10-12 23:01:02 UTC"
    assert sharadar.latest(tmp_path, "SEP") == s.path
    with pytest.raises(FileExistsError):
        sharadar.store_zip(tmp_path, "SEP", z, "again", now=when)


def test_an_unknown_table_is_refused(tmp_path: Path) -> None:
    with pytest.raises(sharadar.SharadarError, match="unknown table"):
        sharadar.download(tmp_path, KEY, "NOPE", lambda url: pytest.fail("no request expected"))
