"""Backups of everything git does not hold (data/, var/, reports/), and a restore check that proves they work.

    investtree backup run       # copy new and changed files to the backup folder, write its manifest
    investtree backup verify    # restore the backup into a temporary folder and check every file's hash

The backup folder holds a mirror of those folders plus manifest.json (path -> SHA-256 and size). It adds and
updates files but never deletes from the backup, so a file removed by mistake can still be recovered.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import Paths

MANIFEST = "manifest.json"


def default_destination() -> Path:
    """OneDrive when it is set up (as v1's backup used), else a folder beside the project."""
    one = os.environ.get("ONEDRIVE") or os.environ.get("ONEDRIVECONSUMER")     # Windows names are case-insensitive
    base = Path(one) if one else Path.home() / "Backups"
    return base / "investTree2-backup"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(paths: Paths) -> list[Path]:
    out: list[Path] = []
    for top in paths.not_in_git():
        if top.exists():
            out += sorted(p for p in top.rglob("*") if p.is_file())
    return out


@dataclass(frozen=True)
class BackupResult:
    files: int
    copied: int
    bytes_copied: int
    destination: Path


def run(paths: Paths, dest: Path | None = None) -> BackupResult:
    """Copy each file whose hash differs from the backup's copy, then write the manifest last, so a manifest
    only ever describes files that are already in place."""
    dest = dest or default_destination()
    dest.mkdir(parents=True, exist_ok=True)
    old = _read_manifest(dest)
    manifest: dict[str, dict[str, object]] = {}
    copied = nbytes = 0
    for src in _files(paths):
        rel = src.relative_to(paths.root).as_posix()
        digest, size = sha256(src), src.stat().st_size
        manifest[rel] = {"sha256": digest, "size": size}
        target = dest / rel
        if old.get(rel, {}).get("sha256") == digest and target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        shutil.copy2(src, tmp)
        tmp.replace(target)
        copied += 1
        nbytes += size
    # files no longer in the project stay listed, so verify still checks them
    for rel, entry in old.items():
        manifest.setdefault(rel, entry)
    payload = {"written": datetime.now().isoformat(timespec="seconds"), "root": paths.root.as_posix(),
               "files": manifest}
    tmp = dest / (MANIFEST + ".part")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(dest / MANIFEST)
    return BackupResult(files=len(manifest), copied=copied, bytes_copied=nbytes, destination=dest)


def _read_manifest(dest: Path) -> dict[str, dict[str, object]]:
    try:
        data = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
        files = data.get("files", {}) if isinstance(data, dict) else {}
        return {str(k): dict(v) for k, v in files.items()}
    except (OSError, ValueError):
        return {}


@dataclass(frozen=True)
class VerifyResult:
    checked: int
    missing: list[str]
    corrupt: list[str]

    @property
    def ok(self) -> bool:
        return self.checked > 0 and not self.missing and not self.corrupt


def verify(dest: Path | None = None) -> VerifyResult:
    """Restore the whole backup into a temporary folder, as a real restore would, and check every file
    against the manifest. Nothing in the project is touched."""
    dest = dest or default_destination()
    manifest = _read_manifest(dest)
    missing, corrupt = [], []
    with tempfile.TemporaryDirectory(prefix="investtree-restore-") as tmp:
        for rel, entry in sorted(manifest.items()):
            src = dest / rel
            if not src.exists():
                missing.append(rel)
                continue
            restored = Path(tmp) / rel
            restored.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, restored)
            if sha256(restored) != entry.get("sha256"):
                corrupt.append(rel)
    return VerifyResult(checked=len(manifest), missing=missing, corrupt=corrupt)
