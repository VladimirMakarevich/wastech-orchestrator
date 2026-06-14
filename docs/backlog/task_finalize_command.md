# Backlog: Manually finalize a task (`finalize` command)

Status: **backlog / not scheduled**
Date: 2026-06-14
Owner: Vladimir Makarevich

The canonical contract is
[00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md). This document
must not override the hard invariants in [../../CLAUDE.md](../../CLAUDE.md),
[../../AGENTS.md](../../AGENTS.md), or [../rules/](../rules/) — only the orchestrator
commits/pushes/PRs, and a manual finalize must not weaken the security policy or fake a result it did
not verify.

## 1. Background

When a task ends up `failed` / `manual_action_required` — or is interrupted — an operator often takes
over and **finishes (or abandons) the work by hand** (merges the PR themselves, fixes the change
locally, or decides to drop it). Today there is **no command to reconcile the orchestrator's bookkeeping
with that human decision**. The leftovers persist:

- a SQLite task row stuck at a non-`done` status (or a stale `manual_action_required`);
- the agent branch `agent/<id>-<slug>` still in the clone (terminal cleanup never deletes it);
- possibly a working tree not on `base_branch`, or with unaccounted dirty files that block the next
  task's terminal cleanup (`git_manager._unaccounted_dirty_paths`);
- the task file sitting in `tasks/failed/` (or `tasks/processing/`) instead of the right lifecycle
  folder;
- a waiting/`transport_error` HITL artifact under `logs/<id>/hitl/`;
- a ledger record that doesn't reflect the real outcome.

The orchestrator already has the *building blocks* but no operator-facing entry point:
`_go_terminal`/`terminal_cleanup` (checkout base, write `terminal-cleanup.json`), `_move_task_file`,
`_resume_manual` (set `manual_action_required` + append a ledger record), `_resume_cleanup` (finish an
interrupted cleanup), `set_status`, and `Ledger.append`. There is **no command that says "I handled
this one — clean up the state and set the correct status."**

## 2. Goal

A first-class **`finalize <task-id> --as <done|failed|abandoned>`** that, for a task the operator
resolved out-of-band, **reconciles the bookkeeping only** — sets a correct terminal status, cleans the
working tree/branch, moves the task file to the right folder, closes waiting HITL artifacts, records
the outcome in the ledger, and releases the slot. It **does not run the pipeline** and **does not
commit/push/PR** — it records and tidies a decision the human already made.

## 3. Non-goals and limits

- Not a re-run (that is [task_rerun_command.md](task_rerun_command.md), which launches a *fresh
  attempt*). `finalize` runs no agent stages.
- Does not commit/push/PR on the operator's behalf and does not fabricate a PR URL; `--as done` only
  *records* that the operator completed/merged it (optionally capturing a `--pr-url`/`--note`).
- Does not merge or rebase code. Working-tree reconciliation is limited to safe operations (checkout
  base; optionally delete the now-unneeded agent branch) and is **fail-closed** when the tree has
  unaccounted changes (it reports them rather than discarding work).

## 4. `finalize` vs. `rerun` vs. `resume`

| | resume (exists) | rerun (backlog) | finalize (this task) |
|---|---|---|---|
| Intent | continue a crashed active task | new attempt of a terminal task | record + tidy a human-handled task |
| Runs the pipeline? | yes (from checkpoint) | yes (from scratch) | **no** |
| Commits/PRs? | yes (idempotent) | yes | **no** |
| Sets status to | continues to a natural terminal | continues to a natural terminal | the operator-declared terminal |
| Touches working tree | re-attaches branch | resets branch to base | checkout base + optional branch delete |

## 5. Proposed design

### 5.1. CLI surface

`worc finalize <task-id> --as <done|failed|abandoned> [--pr-url URL] [--note TEXT] [--keep-branch] [--dry-run]`

- Resolves the task from SQLite + its `source_path`.
- Refuses if the id is unknown.
- Refuses (or warns) if the task is **currently active in the running daemon** — finalize is for an
  idle slot / a task the operator owns. (If the slot shows active but the daemon is stopped, allow it,
  mirroring the recovery reconciler's single-active reasoning.)
- `--dry-run` prints the planned reconciliation (status, branch action, file move, ledger record) and
  writes nothing (mirror `upgrade-config`/`upgrade-docs`).

### 5.2. What it reconciles (reusing existing building blocks)

1. **Status** → set the SQLite task status to the declared terminal (`done`/`failed`/
   `manual_action_required` for `abandoned`, or a dedicated `abandoned` if added — see §10), with
   `finished_at` and a `cleanup_last_error`/note. Reuse `set_status`/`update_task`.
2. **Working tree** → run terminal cleanup once: checkout `base_branch`, write
   `terminal-cleanup.json` (reuse `terminal_cleanup`/`_resume_cleanup`). **Fail-closed** on
   unaccounted dirty paths — report them, do not discard. Optionally delete the agent branch unless
   `--keep-branch` (default: keep, since the operator may still want it).
3. **Task file** → move to the lifecycle folder matching the declared outcome (`tasks/done/` or
   `tasks/failed/`), reusing `_move_task_file` (extend it to accept the operator-declared status).
4. **HITL** → mark any `waiting`/`transport_error` interaction under `logs/<id>/hitl/` as `consumed`/
   closed so a later resume can't act on a stale prompt.
5. **Ledger** → append (not rewrite) a record capturing the **manual** outcome: `final_status`, a
   `finalized_by: operator` / `manual: true` marker, optional `pr_url`/`note`, `finished_at`. Reuse
   `Ledger.append`.
6. **Slot** → release the single-active-task slot so the next pending task can start.

### 5.3. Outcomes

- `--as done` — the operator completed/merged the work. Record done (+ optional `--pr-url`).
- `--as failed` — give up on this attempt and record it failed (no re-attempt).
- `--as abandoned` — drop the task entirely (e.g. obsolete). Recorded distinctly for audit (see §10:
  reuse `manual_action_required` vs. add a real `abandoned` status).

## 6. Safety

- `finalize` **never** runs a provider, commits, pushes, or opens/merges a PR; it only records and
  tidies. A `--pr-url` is stored verbatim as metadata, never used to call `gh`.
- Working-tree reconciliation is fail-closed: unaccounted dirty changes are reported, not discarded;
  branch deletion is opt-out, off by default.
- All writes (status, ledger, file move, HITL close) happen through the existing State Store / Ledger
  / Git Manager APIs — no hand-editing of `state.db` or `completed.jsonl`, which is exactly the
  fragile manual path this command replaces.
- The ledger stays append-only; a finalize record is added, prior records are preserved.

## 7. Observability and operator experience

- `finalize --dry-run` shows the planned status, branch action, file move, and ledger record.
- The ledger record carries `manual: true` (+ note/pr-url) so the audit trail distinguishes an
  operator-finalized outcome from a pipeline-produced one.
- `status <id>` reflects the finalized status immediately.

## 8. Testing requirements

- Unit: status set to the declared terminal; ledger gains exactly one `manual` record (prior records
  preserved); HITL `waiting` artifact is closed; file moved to the matching folder; slot released.
- Integration (temp git repo): finalize a `failed` task with a stray agent branch → working tree ends
  on `base_branch`, branch kept by default / deleted with the flag; unaccounted dirty tree → fail-closed
  with a clear report, nothing changed.
- Idempotency: a second `finalize` on an already-finalized id is a no-op (or a clear refusal).

## 9. Rollout plan

- **Phase 1:** `finalize <id> --as <done|failed>` — status reconcile + terminal cleanup + file move +
  ledger record + slot release; `--dry-run`.
- **Phase 2:** `--pr-url`/`--note` metadata, HITL close-out, `--keep-branch`/branch deletion.
- **Phase 3:** `abandoned` outcome (and the status-model decision in §10).

## 10. Open questions

- Status model: represent `abandoned` by reusing `manual_action_required` with a note, or add a real
  terminal `abandoned` status (bumps `state.db` `user_version`, touches the state machine + ledger).
- Should `finalize --as done` require a `--pr-url` (proof of the human merge) or stay optional?
- Interaction with a *running* daemon: refuse while the daemon owns the slot, or coordinate via the
  process-control lock? (Probably: require the daemon stopped / slot idle, like other state-mutating
  CLI ops.)
- Overlap with `rerun`: a shared internal "reconcile a terminal task" helper both commands build on.

## 11. Acceptance criteria

- [ ] `worc finalize <id> --as <done|failed>` sets the correct terminal status, runs terminal cleanup
      (working tree back on `base_branch`), moves the task file to the matching folder, closes any
      waiting HITL artifact, appends a `manual` ledger record, and releases the slot — without running
      any agent stage, commit, push, or PR, and without hand-editing `state.db`/the ledger.
- [ ] An unaccounted dirty working tree makes `finalize` **fail closed** with a clear report and no
      changes (no silent discard of operator work).
- [ ] `--dry-run` writes nothing and prints the planned reconciliation.
- [ ] The ledger keeps prior records and gains one finalize record marked `manual` (with optional
      `pr-url`/`note`); a re-`finalize` of the same id is a no-op or a clear refusal.
- [ ] Tests cover each outcome, the fail-closed dirty-tree path, branch keep/delete, and slot release.

## 12. References

- Terminal/cleanup: `core/orchestrator.py` `_go_terminal`/`_resume_cleanup`/`_resume_manual`/
  `_move_task_file`; `git_manager.py` `terminal_cleanup`/`_unaccounted_dirty_paths`.
- State + ledger: `state_store.py` `set_status`/`update_task`/`find_active_tasks`; `ledger.py`
  `append`/`has_task_id`. State machine + terminal statuses: `core/state_machine.py`.
- Related backlog: [task_rerun_command.md](task_rerun_command.md) (fresh attempt — the complement of
  this close-out command); origin in [post_test_run_review.md](post_test_run_review.md) /
  [follow_ups.md](follow_ups.md).
