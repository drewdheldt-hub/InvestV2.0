# investTree 2

Portfolio risk tools first, evidence-gated research second, AI agents working inside guardrails enforced by
code. Built from the investTree 2 Blueprint and the lessons of investTree v1. Paper account only.

**Status: Phase 1, Portfolio and risk.** Phase 0 passed its gate on 2026-10-13. Gate to phase 2: the overlay
replicates its study exactly (tested: `tests/test_risk.py`); at least 3 months on paper with 12 weekly decisions
and 3 monthly rebalances; share counts always reconcile; success measures met (`investtree overlay review`).

Owner decisions for this phase: holdings are read from IB accounts only, read-only, through Flex statements;
taxes are modelled for taxable accounts only.

## Setup

```
uv sync --all-groups                                   # Python 3.14 and every dependency, locked
uv run pytest                                          # tests (the paper-broker tests are skipped)
uv run pytest -m paper                                 # read-only contract tests against a running paper Gateway
uv run python -m investtree credentials set anthropic-api-key   # optional: lets the operator agent call Claude
powershell -ExecutionPolicy Bypass -File scripts\register_scheduler.ps1   # start the scheduler at logon
```

## What is here

| Path | What it is |
|---|---|
| `src/investtree/execution/broker.py` | The broker adapter: the only code that talks to ib_async. Paper only unless `allow_live`; unknown symbols reported as unknown |
| `src/investtree/execution/fake_ib.py` | A fake of ib_async that copies the real library's quirks, for the contract tests |
| `src/investtree/ops/scheduler.py` | One scheduler process running `config/schedule.yaml`, at below-normal priority, with timeouts |
| `src/investtree/ops/status.py` | The status feed every job reports into (`var/status/feed.jsonl`) |
| `src/investtree/ops/backup.py` | Backups of `data/`, `var/`, `reports/`, and a restore check that proves they work |
| `src/investtree/credentials.py` | Credentials in the Windows credential store, never in files |
| `src/investtree/agents/` | Permissions per role (enforced in code, audited), daily token caps, the operator's briefing |
| `src/investtree/data/bars.py` | Tier 1 data: dividend-adjusted daily bars in Parquet, checked before storing, fingerprinted |
| `src/investtree/data/flex.py` | Downloads IB Flex statements (read-only) with the token from the credential store |
| `src/investtree/portfolio/risk.py` | The overlays' 20-year study, ported from v1 and reproducing its stored result |
| `src/investtree/portfolio/overlay.py` | One day's overlay decision and order sizing, shared by the paper runner and suggestions |
| `src/investtree/portfolio/holdings.py` | Real holdings, lots and trades parsed from Flex statements |
| `src/investtree/portfolio/tax.py` | Lot choice, wash sales and harvesting candidates, for taxable accounts |
| `src/investtree/portfolio/report.py` | The risk report for real holdings |
| `src/investtree/portfolio/suggest.py` | Suggested trades for real accounts (never sent) |
| `src/investtree/execution/overlay_runner.py` | The paper overlay runner (v1's rules and state layout), fill review, the paper gate's record |
| `src/investtree/{evidence,research,app}/` | Later phases |
| `config/` | The schedule, the agents' models and caps, the paper overlays; `accounts.example.yaml` to copy to `accounts.local.yaml` (not in git) |
| `CLAUDE.md`, `.claude/settings.json` | Rules and permissions for the builder agent (Claude Code) |
| `ledger/`, `registry/`, `experiments/` | Pre-registrations, result manifests and experiment configs (from phase 2) |

## Commands

Run as `uv run python -m investtree <command>`. (Windows Application Control blocks the generated
`investtree.exe` launcher because it is new and unsigned; `python -m` runs the same code.)

```
investtree status                    what every job last did
investtree scheduler run|due         run the scheduler, or show what is due now
investtree backup run|verify         back up; restore into a temporary folder and check every hash
investtree brief [--plain]           today's operator briefing, in reports/briefings/<date>.md
investtree broker check [--port]     read-only connection check against the paper Gateway
investtree credentials set|check     store a credential (prompted, hidden) or check it is set
investtree data update [SYMBOLS]     refresh tier 1 daily bars from the paper Gateway (read-only)
investtree holdings fetch|show       download Flex statements, or summarise the saved ones
investtree report                    risk report for real holdings: reports/risk/<date>.md
investtree suggest                   suggested trades for real accounts: reports/suggestions/<date>.md
investtree overlay run|fills NAME    the paper overlay's morning decision, or its fill record
investtree overlay review NAME       fill costs, and progress towards the paper gate
investtree overlay import-v1 NAME    carry v1's running paper overlay over to v2
```

## Real accounts

Real accounts are read-only. Their holdings come from IB Flex statements, and the overlay only ever suggests
trades for them; nothing in the code can send an order to a real account. To set them up:

1. In Client Portal, create an Activity Flex Query (XML; Account Information; Open Positions with Summary and
   Lot; Trades with Execution and Closed Lots; Cash Report; last 365 days) and enable the Flex Web Service.
2. `uv run python -m investtree credentials set ib-flex-token`
3. Copy `config/accounts.example.yaml` to `config/accounts.local.yaml` and fill in the query id and accounts.
4. `investtree holdings fetch`, `investtree data update`, then `investtree report` and `investtree suggest`.

## Moving the paper overlay from v1

v1 still runs `spy_voltarget` (SPY at a 10% volatility target, $100,000 of the paper account, since
2026-10-08). v2 makes the same decisions on the same data, but two runners must never trade the same
account: v2's overlay jobs stay disabled until the owner moves it with `investtree overlay import-v1
spy_voltarget`, disables v1's two overlay tasks, sets `execute: true` and enables `overlay-paper` and
`overlay-fills`.

## The operator's briefing

Written each weekday at 17:30 from the status feed: quiet when everything is green, specific when not.
Every statement cites the status event it rests on, as `[<time> <job> <kind>]`. With an API key and room
under its daily cap, Claude Opus 5.5 writes it (low effort; a declined request falls back to another model);
a briefing citing an event that does not exist is rejected and the plain one is written instead. The feed's
text is passed to the model as data, never as instructions.

## Rules that do not change

Paper only; credentials never in files or logs; the ledger is append-only; agents propose and the owner
decides (merging, registering tests, approving repairs, any order). Details in `CLAUDE.md`.
