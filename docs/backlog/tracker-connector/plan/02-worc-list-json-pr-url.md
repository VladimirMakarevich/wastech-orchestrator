# Phase 02 — `pr_url` and a `rejected` section in `worc list --format json`

- **Status:** ☐
- **Depends on:** none
- **Delivers:** FR-W2 — every JSON entry `worc list` prints for a known task carries `pr_url` (URL or `null`); FR-W3 — the `--all` view gains a `rejected` section derived from the ledger (Q-12, option b); the shipped guide names the JSON entry shape as the scripting contract.

## Goal

Let an external process learn, from the one read-only command worc already offers for scripting, whether and where a task's PR exists and which tasks worc's gate refused and why — without that process reading `state.db`, `.worc/` or the ledger itself. Moves AC-W2 and AC-W3.

## Steps

1. **Resolved (2026-09-10):** `TaskRow` has no PR column and the ledger is not needed — the URL is the `result_ref` of the completed `pr` publish-op row, `store.get_publish_op(task_id, KIND_PR, None)`, which `cli.py` already wraps as `_recorded_pr_url(store, task_id)` for `worc prs`. Reuse that helper; **no new column**, no ledger read.
2. `src/wastech_orchestrator/cli.py` — `_task_entry` gains a `pr_url` argument (or the store) and emits `"pr_url": <str | None>`; `_list_sections` already holds the read-only store where it builds the `active` / `recent` / `all` entries, so the lookup happens there (one `get_publish_op` per row — fine at listing sizes). `_pending_entry` adds `"pr_url": None` so the shape is uniform across sections. `_entry_line` (the table view) is unchanged.
3. `src/wastech_orchestrator/packaged/guide/README.md` — there is no operations page in the packaged guide, and `worc list` is mentioned only here: add a short scripting note naming `worc list --format json` as the surface and listing the entry keys (`task_id`, `status`, `title`, `branch`, `pr_url`; pending entries add `file`, `rank`, `priority`, `queue`), with two facts a consumer needs — `--all` lists DB rows only (pending files appear in the default view and under `--pending`; the default `recent` section is capped), and `status` is the display label from `_display_status` (`running (paused)`, `running (paused until …)`, `parked (no daemon)`), so match its leading token.
4. `src/wastech_orchestrator/cli.py` — the `rejected` section (D15): under `args.all`, open the ledger read-only (`Ledger(logs_root)`, the way the daemon-log helper already does), take the ids for which `only_validation_rejects(id)` holds **and** the store has no row, and for each emit one entry from its latest record: `task_id`, `status: "rejected"`, `title: None`, `branch: None`, `pr_url: None`, `validation_reason`, `rejected_at` (= `finished_at`). Append it as a fourth section `("rejected", …)` so the JSON view flattens it like the others; teach `_entry_line` to print `rejected  <id>  (<validation_reason>)`. No ledger file → an empty section. The default view, `--recent`, `--pending` and `--format ids` do not change (a rejected id is not one `rerun` / `status` accept, so it stays out of the completion surface).
5. The guide note from step 3 also names the `rejected` entry shape and says when an id appears there (validation-only ledger trace, no row) and when it leaves (re-submitted under the same id and accepted).
6. Tests, gates, mdlint.

## Files touched

- `src/wastech_orchestrator/cli.py` (`_task_entry`, `_pending_entry`, `_list_sections`, `_entry_line`); `ledger.py` is read through its existing `records()` / `only_validation_rejects()` and is not changed
- `src/wastech_orchestrator/packaged/guide/README.md`
- `tests/core/test_cli_pipeline.py` (next to `test_cmd_list_format_json`; `_seed_list_db` seeds `TaskRow`s — add a completed `pr` publish-op row for the task with a PR)

## Invariants in play

- Read-only: `list` opens the store read-only and never mutates; that stays, and the ledger is read the same way — never appended from here.
- The duplicate-id exemption for validation-only ledger traces is untouched: the `rejected` section only reads the same predicate (`only_validation_rejects`), it does not reserve an id.
- No secrets: a PR URL is not a secret; nothing else joins the entry.
- Greenfield: adding a key is the whole change; no versioning of the JSON shape beyond the guide naming it.

## Tests

- `cmd_list --format json --all` on a seeded store: a task with a completed `pr` publish-op → its URL; a task without → `null`. `cmd_list --format json --pending` with a file in `tasks/pending/`: the file-derived entry → `null`.
- `cmd_list --format json --all` with a seeded ledger (AC-W3): a validation-only id with no row → one `rejected` entry with its `validation_reason` and `rejected_at`; an id with a reject record **and** a row → ordinary row only; no ledger file → no `rejected` entries, exit 0. The table view under `--all` prints the `rejected:` section; the default view and `--format ids` print no rejected id.
- Table output otherwise unchanged (no new column in the human view; the only addition is the `rejected:` section under `--all`).

## Docs to sync in this phase

- `packaged/guide/README.md` (scripting note).
- `docs/backlog/tracker-connector/` — tick this phase.
- PR description breadcrumb: the derived operations page on the documentation branch.

## Acceptance for this phase

- [ ] AC-W2 and AC-W3 pass.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports` and `pytest` are green; `python tools/mdlint.py` is green.
