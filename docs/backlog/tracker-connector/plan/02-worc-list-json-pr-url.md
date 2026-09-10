# Phase 02 — `pr_url` in `worc list --format json`

- **Status:** ☐
- **Depends on:** none
- **Delivers:** FR-W2 — every JSON entry `worc list` prints for a known task carries `pr_url` (URL or `null`), and the shipped guide names the JSON entry shape as the scripting contract.

## Goal

Let an external process learn, from the one read-only command worc already offers for scripting, whether and where a task's PR exists — without reading `state.db` or the ledger. Moves AC-W2.

## Steps

1. Find where the run records the PR URL: the ledger's `finalized` record carries `pr_url` (`core/orchestrator.py`), and `TaskRow` in `state_store.py` may or may not. If the row has it, read it; if only the ledger has it, resolve it through the store/ledger accessor `worc status` already uses for the same value — **no new column** unless neither source has it per task.
2. `src/wastech_orchestrator/cli.py` — `_task_entry` adds `"pr_url": <str | None>`; `_pending_entry` adds `"pr_url": None` so the shape is uniform across sections.
3. `src/wastech_orchestrator/packaged/guide/` operations page — name `worc list --format json` as the scripting surface and list the entry keys (`task_id`, `status`, `title`, `branch`, `pr_url`; pending entries add `file`, `rank`, `priority`, `queue`), with the note that `status` is the display label (it can read `parked (no daemon)` or carry `(paused)`).
4. Tests, gates, mdlint.

## Files touched

- `src/wastech_orchestrator/cli.py`
- possibly `src/wastech_orchestrator/state_store.py` / `ledger.py` (read accessor only)
- `src/wastech_orchestrator/packaged/guide/` (operations page)
- `tests/` (`cmd_list`)

## Invariants in play

- Read-only: `list` opens the store read-only and never mutates; that stays.
- No secrets: a PR URL is not a secret; nothing else joins the entry.
- Greenfield: adding a key is the whole change; no versioning of the JSON shape beyond the guide naming it.

## Tests

- `cmd_list --format json --all` on a seeded store: a task with a PR → URL; a task without → `null`; a pending file entry → `null`.
- Table output unchanged (no new column in the human view unless deliberately added).

## Docs to sync in this phase

- Packaged guide operations page.
- `docs/backlog/tracker-connector/` — tick this phase.
- PR description breadcrumb: the derived operations page on the documentation branch.

## Acceptance for this phase

- [ ] AC-W2 passes.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports` and `pytest` are green; `python tools/mdlint.py` is green.
