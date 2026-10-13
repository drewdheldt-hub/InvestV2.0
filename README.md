# investTree 2

Portfolio risk tools first, evidence-gated research second, AI agents working inside guardrails enforced by
code. Built from the investTree 2 Blueprint and the lessons of investTree v1. Paper account only.

**Status: Phase 0, Foundation.** Gate to phase 1: CI green; broker contract tests pass against paper; a
restore from backup works.

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
| `src/investtree/{data,evidence,research,portfolio,app}/` | Later phases |
| `config/` | The schedule and the agents' models and caps |
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
```

## The operator's briefing

Written each weekday at 17:30 from the status feed: quiet when everything is green, specific when not.
Every statement cites the status event it rests on, as `[<time> <job> <kind>]`. With an API key and room
under its daily cap, Claude Opus 5.5 writes it (low effort; a declined request falls back to another model);
a briefing citing an event that does not exist is rejected and the plain one is written instead. The feed's
text is passed to the model as data, never as instructions.

## Rules that do not change

Paper only; credentials never in files or logs; the ledger is append-only; agents propose and the owner
decides (merging, registering tests, approving repairs, any order). Details in `CLAUDE.md`.
