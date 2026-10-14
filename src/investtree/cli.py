"""The investtree command.

    investtree status                       what every job last did
    investtree scheduler run|due            run the scheduler, or show what is due now
    investtree backup run|verify            back up what git does not hold; prove a restore works
    investtree brief                        write today's operator briefing
    investtree credentials set|check NAME   store a credential (prompted, never echoed), or check it is set
    investtree broker check [--port P]      read-only connection check against the paper Gateway
    investtree gateway start                start IB Gateway through IBC (paper) unless it is running

  Phase 1, portfolio and risk:
    investtree data update [SYMBOLS]        refresh tier 1 daily bars (default: everything the project uses)
    investtree holdings fetch|show          download Flex statements (read-only), or summarise them
    investtree report                       the risk report for real holdings (reports/risk/<date>.md)
    investtree suggest                      suggested trades for real accounts (reports/suggestions/<date>.md)
    investtree overlay run|fills NAME       the paper overlay's morning decision, or its fill record
    investtree overlay review NAME          fill costs and progress towards the paper gate
    investtree overlay import-v1 NAME --from DIR   carry a v1 paper overlay's state over (done once, 2026-10-13)
    investtree study WEIGHTS                every policy, and the band against the monthly rebalance, on stored
                                            bars (e.g. SPY:0.6,TLT:0.4); reports/studies/<weights>_<date>.md
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from .config import Paths, load_accounts, load_agents, load_overlays, load_schedule

if TYPE_CHECKING:
    from .execution.broker import IBAdapter


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


# ------------------------------------------------------------------ phase 1
def _listening(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def paper_broker(client_id: int, readonly: bool = True, deadline: datetime | None = None) -> IBAdapter | None:
    """A connected adapter on the first paper port that answers, waiting until `deadline` for one. Paper ports
    only, and the adapter also refuses a live account."""
    from .execution.broker import PAPER_PORTS, IBAdapter

    while True:
        port = next((p for p in PAPER_PORTS if _listening(p)), None)
        if port is not None:
            b = IBAdapter(port, client_id, readonly=readonly)
            b.connect()
            return b
        if deadline is None or datetime.now() >= deadline:
            return None
        time.sleep(60)


def _until(hhmm: str | None) -> datetime | None:
    if not hhmm:
        return None
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)


def _note(paths: Paths, job: str, text: str) -> None:
    """What a command did, in the status feed, so the operator's briefing can say it."""
    from .ops import status

    status.record(paths.status_feed, job, "note", text)


def _industries(paths: Paths) -> dict[str, str]:
    p = paths.data / "holdings" / "industries.json"
    loaded: dict[str, str] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return loaded


def project_symbols(paths: Paths) -> list[str]:
    """Every symbol something here uses: the paper overlays, the accounts' overlays, SPY (the risk report's
    benchmark), and the stocks and funds in the latest holdings."""
    from .portfolio import holdings

    out = {"SPY"}
    for o in load_overlays(paths).overlays:
        out |= set(o.weights)
    for a in load_accounts(paths).accounts:
        if a.overlay:
            out |= set(a.overlay.weights)
    try:
        h = holdings.load(paths.data)
        out |= {p.symbol for p in h.positions if p.asset_class == "STK" and p.symbol}
    except FileNotFoundError:
        pass
    return sorted(out)


def cmd_data(args: argparse.Namespace) -> int:
    from .data import bars
    from .execution.broker import BrokerError

    paths = Paths()
    symbols = [s.upper() for s in args.symbols] or project_symbols(paths)
    deadline = datetime.now() + timedelta(minutes=args.wait_minutes) if args.wait_minutes else None
    b = paper_broker(args.client_id, deadline=deadline)
    if b is None:
        print("no IB Gateway or TWS on a paper port (4002, 7497): nothing updated", file=sys.stderr)
        _note(paths, "data-update", "no paper Gateway: bars not updated")
        return 1
    industries = _industries(paths)
    try:
        results = bars.refresh(paths.data, symbols, b.daily_bars)
        for s in symbols:
            if s not in industries:
                with contextlib.suppress(BrokerError):         # unknown to IB: the bars refresh says so
                    industries[s] = b.industry(s) or "fund or other"
    finally:
        b.disconnect()
    p = paths.data / "holdings" / "industries.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(industries, indent=1, sort_keys=True), encoding="utf-8")
    failed = [r for r in results if not r.written]
    for r in results:
        flag = "ok     " if r.written else "FAILED "
        print(f"{flag} {r.symbol:6} {r.rows:6} bars, last {r.last}{' (changed)' if r.changed else ''}")
        for pr in r.problems:
            print(f"        {'fatal' if pr.fatal else 'look '}: {pr.day or ''} {pr.what}")
    _note(paths, "data-update", f"{len(results) - len(failed)} of {len(results)} symbols refreshed"
          + (f"; failed: {', '.join(r.symbol for r in failed)}" if failed else ""))
    return 1 if failed else 0


def cmd_holdings(args: argparse.Namespace) -> int:
    from . import credentials
    from .data import flex
    from .portfolio import holdings

    paths = Paths()
    if args.action == "fetch":
        cfg = load_accounts(paths)
        token = credentials.get("ib-flex-token")
        if not token or not cfg.flex_query_ids:
            print("needs a Flex token (investtree credentials set ib-flex-token) and flex_query_ids in "
                  "config/accounts.local.yaml (see config/accounts.example.yaml)", file=sys.stderr)
            return 1
        for q in cfg.flex_query_ids:
            try:
                xml = flex.fetch(token, q)
                h = holdings.parse(xml, f"query {q}")
            except (flex.FlexServiceError, holdings.FlexError) as e:
                print(f"query {q}: {e}", file=sys.stderr)
                _note(paths, "holdings-fetch", f"query {q} failed: {e}")
                return 1
            saved = flex.save(paths.data, xml, q)
            print(f"query {q}: {len(h.accounts)} account(s), {len(h.positions)} positions, as of {h.as_of}: "
                  f"{saved.name}")
        _note(paths, "holdings-fetch", f"{len(cfg.flex_query_ids)} statement(s) saved")
        return 0
    h = holdings.load(paths.data)
    print(f"holdings as of {h.as_of} from {len(h.sources)} statement(s)")
    for acct in sorted(h.accounts.values(), key=lambda a: a.account_id):
        pos = h.for_account(acct.account_id)
        value = sum(p.value for p in pos) + h.cash.get(acct.account_id, 0.0)
        lots = len([x for x in h.lots if x.account_id == acct.account_id])
        print(f"  {acct.account_id} {acct.alias} ({acct.account_type}): {len(pos)} positions, {lots} lots, "
              f"${value:,.0f}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .portfolio import holdings, report

    paths = Paths()
    h = holdings.load(paths.data)
    cfg = load_accounts(paths)
    today = date.today()
    sections = report.build(h, cfg, paths.data, _industries(paths))
    p = report.write(paths.reports, report.render(sections, h, cfg, today), today)
    print(f"risk report: {p}")
    return 0


def cmd_suggest(args: argparse.Namespace) -> int:
    from .portfolio import holdings, suggest

    paths = Paths()
    h = holdings.load(paths.data)
    today = date.today()
    sugs = suggest.build(h, load_accounts(paths), paths.data, paths.var / "suggest", today)
    p = suggest.write(paths.reports, suggest.render(sugs, h, today), today)
    trades = sum(len(s.trades) for s in sugs)
    refused = [s.account.name or s.account.id for s in sugs if s.refused]
    print(f"suggested trades: {p} ({trades} trade(s)" + (f"; refused for {', '.join(refused)}" if refused else "")
          + ")")
    _note(paths, "suggest", f"{trades} trade(s) suggested"
          + (f"; nothing suggested for {len(refused)} account(s)" if refused else ""))
    return 1 if refused else 0


def cmd_overlay(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .execution import overlay_runner as ovr

    paths = Paths()
    cfg = load_overlays(paths).get(args.name)
    if args.action == "import-v1":
        if not args.source:
            print("name v1's overlay_state folder with --from", file=sys.stderr)
            return 1
        copied = ovr.import_v1(Path(args.source), args.name, paths)
        print("copied: " + ", ".join(str(c) for c in copied))
        print("next: disable v1's 'investTree overlay paper' and 'investTree overlay fills' tasks, set execute: true "
              "in config/overlays.yaml, and enable the overlay-paper and overlay-fills jobs in config/schedule.yaml")
        return 0
    if args.action == "review":
        state = paths.overlay_state / f"{cfg.name}.json"
        journal = paths.overlay_state / f"{cfg.name}.journal.jsonl"
        fr = ovr.review_fills(journal)
        rec = ovr.paper_record(state, journal, date.today())
        print(f"fills: {fr.orders} order(s) on {fr.days} morning(s), {fr.unfilled} not filled in full; average cost "
              f"{fr.avg_vs_open_bp} bp against the open, {fr.avg_vs_close_bp} bp against the sizing close; "
              f"commission ${fr.commission}; within 10 bp of the open: {fr.within_target}")
        print(f"paper gate: started {rec['started']}, {rec['months_on_paper']} months, {rec['weekly_decisions']} "
              f"weekly decisions, {rec['monthly_rebalances']} monthly rebalances, {rec['share_mismatches']} share "
              f"mismatches: {'met' if rec['gate_met'] else 'not yet met'}")
        return 0
    job = "overlay-paper" if args.action == "run" else "overlay-fills"
    sending = args.action == "run" and cfg.execute
    b = paper_broker(cfg.client_id, readonly=not sending, deadline=_until(args.wait_until))
    if b is None:
        msg = "no IB Gateway or TWS on a paper port: " + ("no decision today" if args.action == "run"
                                                           else "fills not recorded")
        print(msg, file=sys.stderr)
        _note(paths, job, msg)
        return 1
    try:
        r = ovr.Runner(b, cfg, paths)
        out = r.run(date.today()) if args.action == "run" else r.record_fills(date.today())
    finally:
        b.disconnect()
    print(f"{cfg.name}: {out.action}: {out.why}")
    if out.detail:
        print(json.dumps(out.detail, indent=1, default=str))
    _note(paths, job, f"{cfg.name}: {out.action}: {out.why}")
    return 1 if out.failed else 0


def cmd_study(args: argparse.Namespace) -> int:
    import numpy as np

    from .data import bars
    from .portfolio import risk

    paths = Paths()
    weights = risk.parse_weights(args.weights)
    days, closes = bars.closes(paths.data, list(weights))
    dates = np.array(days, dtype="datetime64[D]")
    s = risk.study(dates, closes, weights, bootstrap_runs=args.runs, policies=(*risk.POLICIES[1:], "band"))
    v = risk.band_vs_monthly(dates, closes, np.array(list(weights.values())), runs=args.runs)
    name = "_".join(f"{k}{round(w * 100)}" for k, w in weights.items())
    lines = [f"# Study: {args.weights}", "",
             f"{days[1]} to {days[-1]}, {len(days) - 1} days of dividend-adjusted closes (tier 1 store). Costs 2 bp "
             "a side; cash earns nothing.", "",
             "| Policy | CAGR | Volatility | Sharpe | Worst drawdown | Worst month | Turnover a year | "
             "Smaller drawdown than buy and hold |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for p, r in s.policies.items():
        m = r.whole
        better = "" if r.vs_buy_and_hold is None else f"{r.vs_buy_and_hold.dd_better_share:.0%} of resamples"
        lines.append(f"| {risk.TITLES[p]} | {m.cagr:.1%} | {m.vol:.1%} | {m.sharpe:.2f} | {m.max_dd:.1%} | "
                     f"{m.worst_month:.1%} | {m.turnover_per_year or 0:.2f} | {better} |")
    lines += ["", "## Band against the monthly rebalance", "",
              "The rule was fixed before this was run: adopt the band only if its turnover is at least a third lower, "
              "its Sharpe is not clearly worse, and its worst drawdown is no more than 1 point deeper.", "",
              *[f"- {r}" for r in v.reasons], "",
              f"**Verdict: {'adopt the band' if v.adopt else 'keep the monthly rebalance'}.**", ""]
    out = paths.reports / "studies" / f"{name}_{date.today()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwritten to {out}")
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    from .ops import gateway

    r = gateway.start(wait_seconds=args.wait_seconds)
    print(r.message, file=sys.stdout if r.ok else sys.stderr)
    _note(Paths(), "gateway", r.message)
    return 0 if r.ok else 1


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
    du = sub.add_parser("data", help="refresh tier 1 daily bars from the paper Gateway")
    du.add_argument("action", choices=["update"])
    du.add_argument("symbols", nargs="*", help="default: every symbol the overlays, accounts and holdings use")
    du.add_argument("--client-id", type=int, default=80)
    du.add_argument("--wait-minutes", type=float, default=0, help="wait this long for the Gateway")
    du.set_defaults(fn=cmd_data)
    ho = sub.add_parser("holdings", help="download Flex statements (read-only), or summarise them")
    ho.add_argument("action", choices=["fetch", "show"])
    ho.set_defaults(fn=cmd_holdings)
    sub.add_parser("report", help="the risk report for real holdings").set_defaults(fn=cmd_report)
    sub.add_parser("suggest", help="suggested trades for real accounts (never sent)").set_defaults(fn=cmd_suggest)
    ov = sub.add_parser("overlay", help="the paper overlay runner")
    ov.add_argument("action", choices=["run", "fills", "review", "import-v1"])
    ov.add_argument("name")
    ov.add_argument("--wait-until", help="HH:MM: wait for the paper Gateway until then (default: do not wait)")
    ov.add_argument("--from", dest="source", help="v1's overlay_state folder (import-v1)")
    ov.set_defaults(fn=cmd_overlay)
    st = sub.add_parser("study", help="every policy, and the band against the monthly rebalance, on stored bars")
    st.add_argument("weights", help="e.g. SPY:0.6,TLT:0.4 (adding up to 1)")
    st.add_argument("--runs", type=int, default=1000, help="bootstrap resamples")
    st.set_defaults(fn=cmd_study)
    gw = sub.add_parser("gateway", help="start IB Gateway through IBC (paper) unless it is running")
    gw.add_argument("action", choices=["start"])
    gw.add_argument("--wait-seconds", type=float, default=180)
    gw.set_defaults(fn=cmd_gateway)
    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
