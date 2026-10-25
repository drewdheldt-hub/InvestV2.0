"""Tier 2 research data from Sharadar, through Nasdaq Data Link: access check and table snapshots.

Setup only, ahead of phase 3: this downloads Sharadar's tables as they are and keeps each download as a
dated, fingerprinted snapshot. The point-in-time store built on top of them (queries by as-of date and index
membership) is phase 3 work.

    data/vendor/sharadar/<TABLE>/<snapshot>.parquet    one file per download, never overwritten
    data/vendor/sharadar/manifest.json                 per snapshot: rows, bytes, sha256, Sharadar's own
                                                       snapshot time, when downloaded

Snapshots are kept rather than replaced because vendors revise history (restated fundamentals, corrected
prices); a registered test names the snapshot it ran on, and a changed fingerprint stops it.

The API key belongs to the owner, who stores it once with
    uv run python -m investtree credentials set nasdaq-data-link-api-key
It is passed in, never logged, and kept out of every error message (request URLs carry it).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl

API = "https://data.nasdaq.com/api/v3/datatables/SHARADAR"

# What each table is for. Phase 3 needs the first five; the rest are optional.
TABLES = {
    "TICKERS": "every ticker, delisted ones included: permaticker, exchange, sector, first and last price dates",
    "SEP": "daily stock prices, delisted stocks included (closeadj: dividend- and split-adjusted)",
    "ACTIONS": "corporate actions: splits, dividends, delistings, ticker changes, mergers",
    "SP500": "S&P 500 additions and removals over time, for point-in-time membership",
    "SF1": "fundamentals; dimensions ARQ, ARY and ART are as originally reported",
    "SFP": "daily fund and ETF prices",
    "DAILY": "daily metrics: market cap, enterprise value, valuation ratios",
}
PHASE3 = ("TICKERS", "SEP", "ACTIONS", "SP500", "SF1")

Get = Callable[[str], tuple[int, bytes]]          # url -> (HTTP status, body)


class SharadarError(RuntimeError):
    """Nasdaq Data Link refused or failed a request. The message never includes the API key."""


def _http_get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "investtree/2"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body: bytes = r.read()
            return int(r.status), body
    except urllib.error.HTTPError as e:           # 4xx/5xx carry a JSON error body worth reading
        return int(e.code), e.read()
    except OSError as e:                          # URLError carries the URL, and with it the key
        raise SharadarError(f"could not reach Nasdaq Data Link ({type(e).__name__})") from None


def _url(table: str, key: str, **params: str) -> str:
    return f"{API}/{table}.json?{urllib.parse.urlencode({**params, 'api_key': key})}"


def _json(status: int, body: bytes, what: str) -> dict[str, object]:
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        raise SharadarError(f"{what}: Nasdaq Data Link answered HTTP {status} with something not JSON") from None
    if not isinstance(data, dict):
        raise SharadarError(f"{what}: unexpected answer")
    err = data.get("quandl_error")
    if status != 200 or err:
        code = err.get("code", "") if isinstance(err, dict) else ""
        msg = err.get("message", "") if isinstance(err, dict) else ""
        raise SharadarError(f"{what}: HTTP {status} {code} {msg}".strip())
    return data


# ------------------------------------------------------------------ access check
@dataclass(frozen=True)
class Access:
    table: str
    ok: bool
    detail: str                  # columns seen, or why not


def check(key: str, tables: tuple[str, ...] = tuple(TABLES), get: Get = _http_get) -> list[Access]:
    """Whether the key can read each table: one row from each, nothing stored."""
    if not key:
        raise SharadarError("no API key; store it with: investtree credentials set nasdaq-data-link-api-key")
    out = []
    for t in tables:
        try:
            data = _json(*get(_url(t, key, **{"qopts.per_page": "1"})), what=t)
            dt = data.get("datatable")
            cols = [c.get("name") for c in dt.get("columns", [])] if isinstance(dt, dict) else []
            out.append(Access(t, True, f"{len(cols)} columns"))
        except SharadarError as e:
            out.append(Access(t, False, str(e)))
    return out


# ------------------------------------------------------------------ bulk download
@dataclass(frozen=True)
class Snapshot:
    table: str
    path: Path
    rows: int
    bytes: int
    sha256: str
    vendor_time: str             # Sharadar's data_snapshot_time for this export


def export_link(key: str, table: str, get: Get = _http_get, wait_seconds: float = 30.0,
                tries: int = 40) -> tuple[str, str]:
    """(download link, Sharadar's snapshot time) for a whole table. Nasdaq Data Link builds the export file on
    request; until it is ready the link is empty and the status is creating or regenerating."""
    for attempt in range(tries):
        data = _json(*get(_url(table, key, **{"qopts.export": "true"})), what=f"{table} export")
        bulk = data.get("datatable_bulk_download")
        f = bulk.get("file") if isinstance(bulk, dict) else None
        if not isinstance(f, dict):
            raise SharadarError(f"{table} export: no file in the answer")
        link, status = f.get("link"), str(f.get("status", ""))
        if link and status.lower() == "fresh":
            return str(link), str(f.get("data_snapshot_time", ""))
        if attempt < tries - 1:
            time.sleep(wait_seconds)
    raise SharadarError(f"{table} export: the file was still being prepared after {tries} tries")


def _download(link: str, dest: Path) -> None:
    """Stream the export zip to disk (SEP is several gigabytes; it never sits in memory)."""
    req = urllib.request.Request(link, headers={"User-Agent": "investtree/2"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as fh:
            while chunk := r.read(1 << 20):
                fh.write(chunk)
    except OSError as e:                          # the signed link is temporary, but still kept out of messages
        raise SharadarError(f"download failed ({type(e).__name__})") from None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def folder(data: Path) -> Path:
    return data / "vendor" / "sharadar"


def store_zip(data: Path, table: str, zip_path: Path, vendor_time: str, now: datetime | None = None) -> Snapshot:
    """Convert an export zip (one CSV) into a Parquet snapshot, streaming, and record it in the manifest."""
    now = now or datetime.now()
    dest = folder(data) / table / f"{now:%Y-%m-%d_%H%M%S}.parquet"
    if dest.exists():
        raise FileExistsError(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp, zipfile.ZipFile(zip_path) as z:
        csvs = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if len(csvs) != 1:
            raise SharadarError(f"{table} export: expected one CSV in the zip, found {len(csvs)}")
        csv = Path(z.extract(csvs[0], tmp))
        part = dest.with_suffix(".part")
        pl.scan_csv(csv, infer_schema_length=100_000, try_parse_dates=True).sink_parquet(part)
    rows = pl.scan_parquet(part).select(pl.len()).collect().item()
    part.replace(dest)
    snap = Snapshot(table, dest, int(rows), dest.stat().st_size, _sha256(dest), vendor_time)
    manifest = read_manifest(data)
    manifest.setdefault(table, []).append({
        "file": dest.relative_to(folder(data)).as_posix(), "rows": snap.rows, "bytes": snap.bytes,
        "sha256": snap.sha256, "vendor_time": vendor_time, "downloaded": now.isoformat(timespec="seconds")})
    p = folder(data) / "manifest.json"
    tmp_manifest = p.with_suffix(".tmp")
    tmp_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp_manifest.replace(p)
    return snap


def download(data: Path, key: str, table: str, get: Get = _http_get, wait_seconds: float = 30.0) -> Snapshot:
    """Export, download and store one whole table as a new snapshot."""
    if table not in TABLES:
        raise SharadarError(f"unknown table {table}; known: {', '.join(TABLES)}")
    link, vendor_time = export_link(key, table, get, wait_seconds)
    folder(data).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder(data)) as tmp:
        z = Path(tmp) / f"{table}.zip"
        _download(link, z)
        return store_zip(data, table, z, vendor_time)


def read_manifest(data: Path) -> dict[str, list[dict[str, object]]]:
    p = folder(data) / "manifest.json"
    if not p.exists():
        return {}
    loaded: dict[str, list[dict[str, object]]] = json.loads(p.read_text(encoding="utf-8"))
    return loaded


def latest(data: Path, table: str) -> Path:
    """The most recent snapshot of a table."""
    snaps = read_manifest(data).get(table) or []
    if not snaps:
        raise FileNotFoundError(f"no {table} snapshot; run: investtree vendor sharadar download {table}")
    return folder(data) / str(snaps[-1]["file"])
