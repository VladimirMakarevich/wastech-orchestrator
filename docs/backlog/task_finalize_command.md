# Backlog: Manually finalize a task (`finalize` command)

Status: **implemented** (2026-06-14) — `worc finalize <id> --as <done|failed|abandoned>`. See
[CHANGELOG](../../CHANGELOG.md) `[Unreleased]`, [docs/operations.md](../operations.md) "Finalize a task
you handled by hand", and `tests/core/test_cli_finalize.py`. The sections below are the design record;
the **Outcome** box captures what shipped.
Date: 2026-06-14
Owner: Vladimir Makarevich

The canonical contract is
[00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md). This document
must not override the hard invariants in [../../CLAUDE.md](../../CLAUDE.md),
[../../AGENTS.md](../../AGENTS.md), or [../rules/](../rules/) — only the orchestrator
commits/pushes/PRs, and a manual finalize must not weaken the security policy or fake a result it did
not verify.

## Outcome (as implemented)

Shipped as designed, with one safety correction to the daemon gate:

- **Daemon gate — stricter than §5.1.** `finalize` **refuses whenever the `watch` daemon PID is alive**
  (not only when *this* task is active), because terminal cleanup runs `git checkout base` in the
  **shared clone** and would corrupt a daemon working *any* task. The orphaned-crash case (PID dead,
  task left active) is still allowed and is exactly what finalize reconciles. This matches the `rerun`
  gate. (§5.1/§10 below describe the original per-task gate; the stricter rule supersedes them.)
- **Decisions implemented:** `--as done|failed|abandoned`; `abandoned` = variant A
  (`manual_action_required` + `outcome: abandoned` ledger marker); PR-URL provenance
  (`--pr-url` > recorded `publish_operations` `kind=pr` > none-with-warning); default-on best-effort
  read-only `gh pr view` merge check (`--no-verify-pr`); fail-closed on an unaccounted-dirty tree;
  branch kept unless `--delete-branch`; idempotent (refuses a re-finalize via the `manual` ledger
  marker); `--dry-run`/`--yes`.
- **Code.** `cli.cmd_finalize` (+ `_report_finalize_plan`); `Orchestrator.{plan_finalize,finalize_task}`
  + `FinalizePlan` + the extracted pipeline-free `_relocate_task_file`;
  `StateStore` reused as-is (no schema change — the `manual` marker lives in the ledger);
  `git_manager.{verify_pr_state,delete_branch}`; `hitl.consume_pending_interactions`;
  `LedgerRecord.{manual,note,outcome}`.

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

A common variant deserves calling out: the **PR was already created (and possibly merged) but the
orchestrator crashed/failed before recording `done`.** In that case the PR URL is *already on disk* —
the publishing step records it in `publish_operations.result_ref` with `kind="pr"`
(`git_manager.create_pr`) — even though the task row is stuck at `failed`/`manual_action_required` and
the ledger has no terminal record. `finalize --as done` must reconcile exactly this: it should pick up
the recorded URL rather than make the operator hunt for it, and must not re-create or re-merge anything.

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
- Does not *discover* a PR by querying GitHub by title/branch. The system never queries GitHub today,
  and a custom `pr_title` does not change task/branch identification (the branch is always
  `agent/<id>-<slug>` derived from the task id + slugified title — see §5.4). The PR URL comes from one
  of: the recorded `publish_operations` row, an explicit `--pr-url`, or stays unknown.
- A *read-only* `gh pr view <url> --json state,mergedAt` to confirm a merge is **not** a mutation and
  is permitted (default-on, best-effort — see §5.4 / §10); the invariant it must never cross is
  creating, pushing, or merging — not reading public PR state.
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

`worc finalize <task-id> --as <done|failed|abandoned> [--pr-url URL] [--note TEXT] [--keep-branch] [--no-verify-pr] [--yes] [--dry-run]`

- `--no-verify-pr` skips the default read-only PR merge check (§5.4); `--yes`/`--force` confirms the
  WARNING paths (no URL found, or PR not merged) non-interactively.

- Resolves the task from SQLite + its `source_path`.
- Refuses if the id is unknown.
- **Daemon interaction (decided):** before any write, check the daemon PID file
  (`<artifacts_root>/orchestrator.pid` via `process_control.read_pid`/`is_running`):
  - PID **alive** *and* the task is in an active (non-terminal) status → the running `watch` daemon owns
    the slot → **refuse** with a clear message ("stop the daemon or wait for the slot to idle").
  - PID **dead** (crashed) but the status is still active → orphaned state → **allow**; finalize
    reconciles it, mirroring the recovery reconciler's single-active reasoning on daemon startup.
  - No PID file / slot idle → allow. This matches how other state-mutating CLI ops treat a live daemon.
- `--dry-run` prints the planned reconciliation (status, branch action, file move, ledger record) and
  writes nothing (mirror `upgrade-config`/`upgrade-docs`).

### 5.2. What it reconciles (reusing existing building blocks)

1. **Status** → set the SQLite task status to the declared terminal: `done`/`failed`, and
   `manual_action_required` for `abandoned` (**decided: variant A** — reuse the existing terminal
   status and distinguish "abandoned" via the ledger/note, no new status / no `state.db` migration; see
   §10), with `finished_at` and a `cleanup_last_error`/note. Reuse `set_status`/`update_task`.
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
- `--as abandoned` — drop the task entirely (e.g. obsolete). **Variant A:** persisted as
  `manual_action_required` with an `outcome: abandoned` marker in the ledger record (and the note), so
  the audit trail distinguishes "abandoned" (deliberately dropped) from "failed" (tried, didn't work)
  without a new status. Filtering for abandoned tasks is done on the ledger marker, not the status.

### 5.4. Identifying the PR and the "merged-but-failed" case

This is the scenario raised in review: a custom PR name, and a PR that is already merged while the DB
and artifacts are still `failed`. The design must make both unambiguous.

**A custom PR title does not affect identification.** `pr_title` only feeds `gh pr create --title`. The
task is always keyed by its id; the branch is always `agent/<task-id>-<slug>` with
`slug = slugify(task.title)` (`git_manager.branch_name`). So `finalize` never needs to "find the PR by
name" — it resolves the task by id and the branch deterministically, regardless of any custom title.

**PR URL provenance (precedence).** When recording `--as done`, the URL is resolved in this order:

1. explicit `--pr-url URL` (operator override; always wins);
2. else the recorded `publish_operations` row for the task with `kind="pr"` (`result_ref`) — this is
   where `create_pr` stores the URL even on a run that later failed;
3. else **none** — `finalize` still records `done` and finalizes every state, but it **emits a WARNING**
   ("no PR URL found for this task; recording done without merge proof") and **requires an explicit
   confirmation** before writing (an interactive prompt, or `--yes`/`--force` in non-interactive use).
   This keeps the audit trail honest about a URL-less finalize without blocking a legitimately
   completed task.

`--dry-run` must print *which source* the URL came from (explicit / recorded / none-with-warning), so
the operator can see whether finalize found a recorded PR or is recording blind.

**Merged-but-failed reconciliation.** If a publish op exists (case 2), the orchestrator already created
(and possibly merged) the PR before the failure; `finalize --as done` just records the human-confirmed
terminal state and tidies bookkeeping. It must **not** call `create_pr`/`merge_pr` again. Note that
`merge_pr` already treats `_ALREADY_MERGED_MARKERS` as idempotent success — `finalize` should mirror
that spirit (assume the merge happened; do not attempt it).

**Read-only merge check (default, best-effort).** When a URL is available (cases 1–2), `finalize --as
done` runs a read-only `gh pr view <url> --json state,mergedAt` before writing. This is read-only — it
inspects PR state, never creates/pushes/merges — so it does not weaken the security policy. Behaviour:

- PR is `MERGED` → record `done` normally.
- PR is `OPEN`/`CLOSED` (not merged) → **WARNING** + require confirmation, same gate as the no-URL case
  above ("you are marking done, but the PR is not merged").
- `gh` missing, unauthenticated, offline, or the PR/URL is gone → **skip the check** with a noted
  "verification skipped" and proceed (it is best-effort, never a hard failure). A `--no-verify-pr`
  escape hatch disables it outright.

So `finalize --as done` writes `done` in all paths, but only *silently* when it has positive proof of a
merge; otherwise it warns and asks. See §10.

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
- PR URL provenance: `--as done` with no `--pr-url` but an existing `publish_operations` `kind="pr"`
  row → the recorded `result_ref` is picked up and written to the ledger; with neither, the documented
  fallback (URL-less `done` or refusal) holds; explicit `--pr-url` overrides the recorded one.
- Merged-but-failed: a `failed` task that already has a completed PR publish-op → `finalize --as done`
  records `done` reusing that URL and does **not** invoke `create_pr`/`merge_pr`; a custom `pr_title`
  on the task does not change which branch/task is resolved.
- Confirmation gates: no-URL `--as done` warns and refuses without `--yes`, finalizes with it; verify
  check (mock `gh`) → MERGED finalizes silently, OPEN/CLOSED warns+needs `--yes`, `gh` failure/missing
  is skipped (still finalizes) and `--no-verify-pr` bypasses the call entirely.
- Daemon gate: live PID + active task → finalize refuses; dead PID + active task → finalize proceeds;
  no PID / idle slot → proceeds (mock `process_control.read_pid`/`is_running`).
- `--as abandoned` → status is `manual_action_required`, ledger record carries `outcome: abandoned`
  (distinct from a plain `--as failed` record), all other reconciliation identical.

## 9. Rollout plan

- **Phase 1:** `finalize <id> --as <done|failed>` — status reconcile + terminal cleanup + file move +
  ledger record + slot release; `--dry-run`.
- **Phase 2:** `--pr-url`/`--note` metadata, HITL close-out, `--keep-branch`/branch deletion.
- **Phase 3:** `abandoned` outcome (variant A — `manual_action_required` + `outcome: abandoned` ledger
  marker; §10).

The daemon-liveness gate (§5.1) is part of Phase 1, since every phase writes state.

## 10. Open questions

- ~~Status model for `abandoned`?~~ **Decided: variant A** — reuse `manual_action_required` + an
  `outcome: abandoned` ledger/note marker. No new status, no `state.db` `user_version` bump, no state
  machine change. (Variant B — a real terminal `abandoned` status — was rejected as too costly for the
  benefit; revisit only if status-level filtering becomes a hard requirement.)
- ~~Should `finalize --as done` require a `--pr-url`?~~ **Decided:** stays optional; auto-fills from the
  recorded `publish_operations` row (§5.4 provenance). When no URL is found anywhere, still record
  `done` and finalize, but emit a WARNING and require confirmation (`--yes`/`--force` in non-interactive
  use).
- ~~Read-only PR verification — default / flag / off?~~ **Decided:** default on, best-effort (§5.4).
  Runs `gh pr view --json state,mergedAt` when a URL is known; not-merged → WARNING + confirm;
  `gh`/auth/network/PR missing → skip with a note, never a hard failure; `--no-verify-pr` disables it.
- ~~Interaction with a *running* daemon?~~ **Decided:** gate on the PID file
  (`process_control.read_pid`/`is_running`) — refuse when the PID is alive and the task is active
  (daemon owns the slot); allow when the PID is dead-but-active (orphaned crash) or the slot is idle.
  See §5.1. (Caveat: the bare-PID recycling gap from `follow_ups.md` row 21 applies here too — the same
  hardening, recording start-time/boot-id, would benefit this check.)
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
- [ ] `--as done` resolves the PR URL by the §5.4 precedence (`--pr-url` > recorded
      `publish_operations` row > none), never re-creates/re-merges a PR, and `--dry-run` reports which
      source the URL came from; a custom `pr_title` never affects task/branch resolution.
- [ ] When no URL is found anywhere, `--as done` still finalizes but emits a WARNING and requires
      confirmation (`--yes` non-interactively); the default read-only `gh pr view` check warns+confirms
      on a not-merged PR and is skipped (not failed) when `gh`/auth/network/PR is unavailable or
      `--no-verify-pr` is set.
- [ ] `finalize` refuses while a **live** daemon owns the active task, and proceeds when the daemon PID
      is dead (orphaned) or the slot is idle.
- [ ] `--as abandoned` records `manual_action_required` with an `outcome: abandoned` ledger marker
      (variant A), distinct from `--as failed`.
- [ ] Tests cover each outcome (`done`/`failed`/`abandoned`), the fail-closed dirty-tree path, branch
      keep/delete, slot release, the daemon gate, and the URL-provenance / verify-PR paths.

## 12. References

- Terminal/cleanup: `core/orchestrator.py` `_go_terminal`/`_resume_cleanup`/`_resume_manual`/
  `_move_task_file`; `git_manager.py` `terminal_cleanup`/`_unaccounted_dirty_paths`.
- State + ledger: `state_store.py` `set_status`/`update_task`/`find_active_tasks`; `ledger.py`
  `append`/`has_task_id`. State machine + terminal statuses: `core/state_machine.py`.
- Daemon gate + recovery: `process_control.py` `read_pid`/`is_running`/`pid_file_path`
  (`<artifacts_root>/orchestrator.pid`); `core/recovery.py` `RecoveryReconciler.reconcile`
  (single-active reasoning); `cli.py` `cmd_watch`/`watch_loop` (the daemon itself).
- PR URL provenance: `state_store.py` `publish_operations` table (`kind="pr"`, `result_ref`) +
  `get_publish_op`; `git_manager.py` `create_pr` (records the URL), `merge_pr` +
  `_ALREADY_MERGED_MARKERS` (idempotent already-merged handling), `branch_name`. PR title:
  `task/model.py` `pr_title`; `core/orchestrator.py` `_publish` (`title=pr_title or title`).
- Related backlog: [task_rerun_command.md](task_rerun_command.md) (fresh attempt — the complement of
  this close-out command); origin in [post_test_run_review.md](post_test_run_review.md) /
  [follow_ups.md](follow_ups.md).
