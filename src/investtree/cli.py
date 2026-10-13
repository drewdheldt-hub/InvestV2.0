"""The investtree command.

    investtree status                       what every job last did
    investtree scheduler run|due            run the scheduler, or show what is due now
    investtree backup run|verify            back up what git does not hold; prove a restore works
    investtree brief                        write today's operator briefing
    investtree credentials set|check NAME   store a credential (prompted, never echoed), or check it is set
    investtree broker check [--port P]      read-only connection check against the paper Gateway
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from .config import Paths, load_agents, load_schedule


def cmd_status(args: argparse.Namespace) -> int:
    from .ops import status

    paths = Paths()
    states = status.latest(paths.status_feed)
    if not states:
        print("no job has reported yet")
        return 0
    for job, st in sorted(states.items()):
        flag = "FAILED " if st.failed else "running" if st.running else "ok     "
        print(f"{flag} {job:20} {st.last.ts}  {st.last.kind}  {st.last.text}")
    return 1 if any(s.failed for s in states.values()) else 0


def cmd_scheduler(args: argparse.Namespace) -> int:
    from .ops.scheduler import Scheduler

    sch = Scheduler(load_schedule(), Paths())
    if args.action == "due":
        due = sch.due()
        print("\n".join(j.name for j in due) if due else "nothing is due now")
        return 0
    sch.run_forever()
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .ops import backup, status

    paths = Paths()
    dest = Path(args.dest) if args.dest else None
    if args.action == "run":
        r = backup.run(paths, dest)
        print(f"backed up {r.files} files to {r.destination} ({r.copied} new or changed, {r.bytes_copied:,} bytes)")
        return 0
    v = backup.verify(dest)
    if v.ok:
        print(f"restore check passed: {v.checked} files restored and matched their hashes")
        status.record(paths.status_feed, "backup-verify", "finished", f"{v.checked} files restored and matched")
        return 0
    what = "an empty backup" if not v.checked else f"{len(v.missing)} missing, {len(v.corrupt)} damaged"
    print(f"restore check FAILED: {what}", file=sys.stderr)
    for rel in (v.missing + v.corrupt)[:20]:
        print(f"  {rel}", file=sys.stderr)
    status.record(paths.status_feed, "backup-verify", "failed", what)
    return 1


def cmd_brief(args: argparse.Namespace) -> int:
    from . import credentials
    from .agents.operator import anthropic_call, write_briefing

    cfg = load_agents().operator
    key = credentials.get("anthropic-api-key")
    call = anthropic_call(key) if key and cfg.daily_token_cap > 0 and not args.plain else None
    b = write_briefing(Paths(), cfg, datetime.now(), call)
    who = "written by the model" if b.by_model else "plain"
    print(f"{'all green' if b.green else 'needs attention'}: {b.path} ({who})")
    return 0


def cmd_credentials(args: argparse.Namespace) -> int:
    from . import credentials

    if args.action == "set":
        credentials.set_interactively(args.name)
        print(f"{args.name} stored in the credential store")
        return 0
    print(f"{args.name}: {'set' if credentials.is_set(args.name) else 'not set'}")
    return 0


def cmd_broker(args: argparse.Namespace) -> int:
    from .execution.broker import BrokerError, IBAdapter

    b = IBAdapter(args.port, args.client_id)
    try:
        b.connect()
    except (BrokerError, OSError, TimeoutError, ConnectionError) as e:
        print(f"could not connect on port {args.port}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    try:
        bars = b.daily_bars("SPY", 5)
        print(f"connected to account {b.account_id()} ({'paper' if b.is_paper() else 'LIVE'}), port {args.port}")
        print(f"net liquidation {b.net_liquidation():,.2f}; SPY last close {bars[-1].close} on {bars[-1].day}")
        print(f"unknown symbol XTO recognised as unknown: {not b.qualify('XTO')}")
    finally:
        b.disconnect()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="investtree", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="what every job last did").set_defaults(fn=cmd_status)
    sc = sub.add_parser("scheduler", help="run the scheduler, or show what is due")
    sc.add_argument("action", choices=["run", "due"])
    sc.set_defaults(fn=cmd_scheduler)
    bk = sub.add_parser("backup", help="back up what git does not hold, or check that a restore works")
    bk.add_argument("action", choices=["run", "verify"])
    bk.add_argument("--dest", help="backup folder (default: OneDrive\\investTree2-backup)")
    bk.set_defaults(fn=cmd_backup)
    br = sub.add_parser("brief", help="write today's operator briefing")
    br.add_argument("--plain", action="store_true", help="do not call the model")
    br.set_defaults(fn=cmd_brief)
    cr = sub.add_parser("credentials", help="store a credential (prompted, never echoed) or check it is set")
    cr.add_argument("action", choices=["set", "check"])
    cr.add_argument("name")
    cr.set_defaults(fn=cmd_credentials)
    bc = sub.add_parser("broker", help="read-only connection check against the paper Gateway")
    bc.add_argument("action", choices=["check"])
    bc.add_argument("--port", type=int, default=4002)
    bc.add_argument("--client-id", type=int, default=70)
    bc.set_defaults(fn=cmd_broker)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
