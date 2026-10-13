# investTree 2: rules for the builder agent (Claude Code)

The plan is the investTree 2 Blueprint. This project is in **Phase 1: Portfolio and risk** (started 2026-10-13).
Do not start work that belongs to a later phase unless the owner asks.

## Non-negotiable rules

- **Paper only.** Code never connects to a live account or a live port (4001, 7496) unless the owner passes an
  explicit `allow_live`, and live order placement additionally needs a typed confirmation. Never weaken this.
- **Never handle the owner's broker or API credentials.** They live in the Windows credential store
  (`uv run python -m investtree credentials set ...`, run by the owner). Never print, log, commit or ask for them.
- **The ledger is append-only.** Never edit or delete anything under `ledger/`; only the ledger code appends.
- **Agents propose; the owner decides.** Do not merge to `main`, push, register a test, approve a data repair or
  change safety rails without the owner asking. Work on a branch; the owner says when to commit and push.
- **Heavy jobs run at below-normal priority**, with a modest number of workers. Ask before starting anything that
  will use the CPU for more than a few minutes.

## How to work here

- Python 3.14, managed with `uv`. Setup: `uv sync --all-groups`.
- Before calling work done: `uv run ruff check`, `uv run mypy`, `uv run pytest`. All must pass.
- Layers depend downwards only: `app` -> `execution`, `portfolio`, `research` -> `evidence` -> `data`.
  `ops` and `agents` use the same public functions people use; they never reach around them.
- One test file per module (`tests/test_<module>.py`). A fix comes with a test that fails without it.
- Write plainly: comments and messages say what something does for the person reading it.
