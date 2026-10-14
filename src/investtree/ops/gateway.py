"""Start IB Gateway through IBC so it logs in to the PAPER account on its own, unless it is already running.

IBC (C:\\IBC, settings in %USERPROFILE%\\Documents\\IBC\\config.ini) is machine setup shared by anything that
uses the Gateway; this module only launches it and waits for the paper port. IBC then keeps the Gateway up:
it accepts the paper-trading disclaimer, restarts it each night at 23:45 (AutoRestartTime) and takes over a
session logged in elsewhere.

The paper login lives in IBC's config.ini, which belongs to the owner. This code checks only that the file
exists; it never opens it. A missing or wrong login shows up as the Gateway not opening its port in time,
and IBC's own log (C:\\IBC\\Logs) says why.

Scheduled by v2's scheduler (gateway-morning, gateway-night): each morning, or as soon as the scheduler
starts at logon, and at 23:55 each night so a failed nightly restart gets a full login within minutes.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PORT = 4002                                   # IB Gateway's paper port
IBC_START = Path(r"C:\IBC\StartGateway.bat")
NORMAL_PRIORITY = 0x00000020                  # the Gateway runs at normal priority, not the job's below-normal
CREATE_NEW_CONSOLE = 0x00000010


def ibc_settings() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents" / "IBC" / "config.ini"


def listening(port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _launch(bat: Path) -> None:
    """Start IBC's batch file in its own minimised console, detached from this process."""
    flags = (NORMAL_PRIORITY | CREATE_NEW_CONSOLE) if sys.platform == "win32" else 0
    info = None
    if sys.platform == "win32":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 7                  # SW_SHOWMINNOACTIVE
    subprocess.Popen(["cmd", "/c", str(bat)], cwd=bat.parent, creationflags=flags, startupinfo=info,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@dataclass(frozen=True)
class Result:
    ok: bool
    started: bool                             # this call launched it (False: it was already up, or failed)
    message: str


def start(wait_seconds: float = 180, poll_seconds: float = 10, ibc: Path = IBC_START,
          settings: Path | None = None, is_up: Callable[[], bool] = listening,
          launch: Callable[[Path], None] = _launch) -> Result:
    if is_up():
        return Result(True, False, f"IB Gateway already running (port {PORT})")
    settings = settings or ibc_settings()
    if not ibc.exists():
        return Result(False, False, f"IBC is not installed: {ibc} not found")
    if not settings.exists():
        return Result(False, False, f"IBC has no settings file at {settings}; the owner sets up the paper login there")
    launch(ibc)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        time.sleep(poll_seconds)
        if is_up():
            return Result(True, True, f"IB Gateway is up on port {PORT}")
    return Result(False, True, f"IB Gateway did not open port {PORT} within {wait_seconds:.0f} s; see C:\\IBC\\Logs")
