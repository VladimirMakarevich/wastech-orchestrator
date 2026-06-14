# Backlog: Re-run a terminal task (`rerun` command)

Status: **implemented** (2026-06-14) — `worc rerun <id>` with `--continue`. See
[CHANGELOG](../../CHANGELOG.md) `[Unreleased]`, [docs/operations.md](../operations.md) "Re-attempting a
terminal task", and the tests in `tests/core/test_cli_rerun.py`. The sections below are the design
record; the **Outcome** box captures what shipped and the decisions taken.
Date: 2026-06-14
Owner: Vladimir Makarevich

The canonical contract is
[00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md). This document
must not override the hard invariants in [../../CLAUDE.md](../../CLAUDE.md),
[../../AGENTS.md](../../AGENTS.md), or [../rules/](../rules/) — in particular: only the orchestrator
commits/pushes/PRs, the §19 validation gate runs before any branch, and a re-run must not weaken the
security policy.

## Outcome (as implemented)

One command, two modes (chosen for simplest implementation/maintenance — shared guards, one core-step
branch into `Orchestrator.rerun_task` vs `continue_task`):

- **Decisions taken.** (1) Statuses accepted: `failed` + `manual_action_required` (`done` deferred to a
  future `--allow-done`). (2) Fresh mode refuses on a prior remote branch / open PR and points at
  `finalize`; opt-in `--force-reset-remote` deletes the remote branch. (3) Prior `logs/<id>/` is
  full-archived to `logs/<id>/attempt-<N>/` (fresh); continue keeps artifacts. (4) Same-name branch
  reset (delete local, `prepare_branch` recreates). (5) Refuses while a live `watch` daemon owns the
  clone, plus the single-active-slot check, and is fail-closed on an unaccounted-dirty tree.
- **`--continue` (fix-and-continue), added during review.** For an infra failure the operator fixed by
  hand: keep the branch + work, re-enter at the failed stage by reviving the terminal task into the
  existing resume engine (`resume`/`_resume_task`). Needs the stage it failed at — persisted as the new
  `tasks.interrupted_status` column (**`state.db` v3**, one `ALTER TABLE` migration). Un-answered HITL
  prompts are reset so the re-entered stage re-asks.
- **Gaps closed vs. the original design.** The fresh reset also clears `publish_operations` (else the
  cached commit/push/PR short-circuits a fresh attempt) and `subtasks` (else committed-unit skipping
  under-runs a decomposed rerun); the reset is UPDATE-in-place (FK graph) and one transaction; the
  ledger gains `attempt`/`rerun_of`.
- **Code.** `cli.cmd_rerun`; `Orchestrator.{plan_rerun,rerun_task,continue_task}` + `RerunPlan`;
  `StateStore.{reset_task_for_rerun,revive_task_for_continue,clear_publish_operations}` + the
  `interrupted_status` column/migration; `GitManager.reset_branch_to_base` (+ dirty/remote/PR probes);
  `artifacts.archive_task_artifacts`; `hitl.reset_pending_interactions`; `LedgerRecord.{attempt,
  rerun_of}`; the `is_recovery_rerun` gate hook threaded through `build_orchestrator`.

## 1. Background

Today there is **no first-class way to re-run a task that reached a terminal status**
(`done` / `failed` / `manual_action_required`). Two distinct mechanisms are often conflated:

- **Resume (crash recovery, §13).** `watch` → `Orchestrator.resume()` →
  [`_resume_task`](../../src/wastech_orchestrator/core/orchestrator.py) continues *exactly one
  interrupted, still-active task* from its persisted checkpoint. It reads `status`/`branch`/counters/
  decomposition from SQLite and re-enters the pipeline at the stage where it stopped, idempotently
  (Git-Manager fingerprints prevent a duplicate commit/push/PR). This only applies to a task in an
  **active (non-terminal)** status — i.e. the process died mid-task. See `core/recovery.py`
  (`RecoveryReconciler`/`RecoveryPlan`) and `state_machine.py` (`ACTIVE` vs `TERMINAL`).
- **Re-run (retry a finished task).** Not implemented. A task that finished `failed` is terminal:
  its file is moved to `tasks/failed/` (`_move_task_file`), terminal cleanup has checked the working
  tree back to `base_branch` (`git_manager.terminal_cleanup`), a record is in `logs/completed.jsonl`,
  and the SQLite row is frozen at `failed`. There is **no terminal → active transition**, so resume
  never picks it up, and "continue from the point where it fell" is **not possible** for a terminal
  task.

What blocks a naive re-run today: moving the file back to `tasks/pending/` and running it is rejected
by the §19 gate as `DUPLICATE_TASK_ID` — the id is in both the State Store and the ledger
([`validation_gate.py`](../../src/wastech_orchestrator/task/validation_gate.py), the
`_store_has_task_id`/`_ledger_has_task_id` check). The gate already has an `is_recovery_rerun()`
callback hook, but it is **not exposed to the CLI**.

Motivating incident: `task-pr-title-override` failed in the *machinery* (a `mypy .` scope bug and a
lost Telegram approval), not in the agent's code (post-test-run §1.1/§4.1, now fixed). A fresh attempt
would now pass — but the operator has no supported command to launch it, and the manual workaround
(hand-edit `state.db`, delete the ledger line, `git branch -D`, move the file, `run`) is fragile.

## 2. Goal

A first-class, safe **`rerun`** that launches a **fresh attempt** of a terminal task from the *current*
`base_branch`, reusing the existing task definition, without hand-editing SQLite/the ledger/git.

Explicitly: a re-run is a **new attempt from scratch**, not a resume from the failed stage. (Resuming
a terminal task from its old mid-pipeline checkpoint is intentionally out of scope — the working tree,
branch, and base have all moved on; see §4.)

## 3. Non-goals and limits

- Not a resume-from-checkpoint of a terminal task (that state is gone after terminal cleanup).
- Does not change the resume (crash-recovery) path for active tasks.
- Does not auto-resolve code merge conflicts for the agent — it gives the agent a clean, current base
  to work from (§6) so conflicts between a stale attempt branch and an advanced `base_branch` do not
  arise in the first place.
- Does not weaken the §19 gate beyond the single, explicit, id-scoped re-run allowance.

## 4. Resume vs. re-run (the key distinction)

| | Resume (exists) | Re-run (this task) |
|---|---|---|
| Trigger | `watch`/`resume()` on startup | explicit operator command |
| Applies to | one **active** task (crash mid-run) | a **terminal** task (`done`/`failed`/`manual`) |
| Continues from | persisted stage checkpoint | the beginning (fresh attempt) |
| Branch | re-attaches to the existing `agent/<id>-<slug>` | reset to current `base_branch` (§6) |
| Gate | bypassed (internal `is_recovery_rerun`) | bypassed **only** for the named id, via the CLI |

## 5. Proposed design

### 5.1. CLI surface

Add `worc rerun <task-id>` (or `run --rerun <task-id>`):

- Resolves the task definition from its last known `source_path` (e.g. `tasks/failed/<id>.md`) or an
  explicit path argument.
- Refuses if the id has no terminal record (nothing to re-run → point the operator at `run`).
- Refuses if a task is currently active (single-slot invariant, §8.2) — re-run only from an idle slot.
- `--dry-run` prints what it would reset (branch, state row, ledger policy) and exits 0, writing
  nothing (mirror `upgrade-config`/`upgrade-docs`).

### 5.2. Duplicate-id allowance (gate)

Thread the existing `is_recovery_rerun` hook through to the CLI so the gate admits **exactly** the
named id once: `is_recovery_rerun = lambda i: i == rerun_id`. Every other §19 check still runs
(injection scan, field types, etc.) — the only relaxation is the duplicate-id rule, scoped to one id.

### 5.3. State + ledger reset

- Reset the SQLite task row for a clean attempt: clear `branch`/`slug`/counters/decomposition/cleanup
  fields and set the status back to a fresh start (or delete + re-insert). Must be one transaction.
- Ledger policy (decide in §12 open questions): append a new `rerun` attempt record (preserving the
  audit trail of the prior failure) rather than deleting the old line. The ledger is append-only by
  design, so "delete the old line" is the wrong default.
- Preserve the prior attempt's artifacts under `logs/<id>/` for audit (consider an `attempt-N/`
  namespace so a re-run does not clobber the failed run's logs).

### 5.4. Notification

If Telegram is enabled, optionally re-announce the task start (the prior run's HITL artifacts under
`logs/<id>/hitl/` belong to the old attempt and must not be reused — a re-run starts fresh).

## 6. Branch & base-branch handling (merge-conflict avoidance)

This is the core design decision the operator raised. `prepare_branch`
([`git_manager.py`](../../src/wastech_orchestrator/git_manager.py)) already does the right base sync —
`git fetch origin` → `checkout base_branch` → `pull --ff-only` → **create or reuse** the task branch.
The problem is the **reuse of a stale attempt branch**: if the failed attempt left commits on
`agent/<id>-<slug>` and `base_branch` has since advanced, reuse builds new commits on top of the old
ones with **no rebase**, which can diverge and conflict on push.

**Recommended re-run branch strategy: reset to base, do not reuse.** Before the fresh attempt, reset
the attempt branch onto the current `base_branch` (delete + recreate, or `reset --hard <base>` after
the `pull --ff-only`). The agent then re-does the change against current `main`, so there is **no
stale-branch-vs-advanced-main conflict** — the whole point. (Alternative: a versioned branch
`agent/<id>-<slug>-attempt-N`; flagged as an open question — it changes the PR/branch naming contract.)

This needs a small new Git-Manager capability (a re-run-aware branch reset) because today
`prepare_branch` deliberately **reuses** an existing branch for crash-resume idempotency; the re-run
path must opt out of that reuse without breaking the resume path.

## 7. Lifecycle

`rerun <id>` →
(1) gate-admit the id once → (2) reset state row + reset branch onto current base → (3) move the task
file back to `tasks/pending/` (or run it in place) → (4) drive the pipeline as a brand-new attempt →
(5) terminal cleanup + a fresh ledger record as usual.

## 8. Security requirements

- The duplicate-id relaxation is the **only** gate change and is scoped to a single operator-named id;
  every other §19 check (injection scan, normalization, field types) runs unchanged.
- No new path lets a task field build CLI argv/env or bypass approvals; the re-run reuses the same
  task definition through the same gate.
- The §1.2 check-command approval still applies to the fresh attempt (a changed command set is
  re-approved); HITL artifacts from the prior attempt are not reused.
- Branch reset is an orchestrator (Git Manager) operation only — agents never reset/branch.

## 9. Observability and operator experience

- `rerun --dry-run` shows the planned reset (branch action, state reset, ledger policy).
- The new ledger record links the prior attempt (e.g. `rerun_of` / `attempt: N`) so the failure→retry
  chain is auditable.
- `status <id>` should show the latest attempt and that a prior attempt exists.

## 10. Testing requirements

- Unit: gate admits the named id once and still rejects an unrelated duplicate; state-row reset clears
  branch/counters/decomposition; branch-reset helper deletes/recreates (or hard-resets) onto base and
  leaves the resume path's reuse behavior intact.
- Integration (fake CLIs): a failed task re-runs to `done`; the attempt branch starts from current
  base (no stale commits); a re-run while a task is active is refused (single-slot).
- E2e: re-run after `base_branch` advanced — the fresh attempt builds on the new base with no push
  conflict; the ledger gains a second record without losing the first.

## 11. Rollout plan

- **Phase 1:** `rerun <id>` for a `failed` task — gate allowance + state reset + branch reset-to-base +
  fresh attempt + appended ledger record. (Covers the motivating incident.)
- **Phase 2:** `--dry-run`, attempt-namespaced artifacts (`logs/<id>/attempt-N/`), `status` surfacing
  the attempt chain.
- **Phase 3:** extend to `manual_action_required` and (guarded) `done`; versioned-branch option.

## 12. Open questions

- Ledger: append an attempt record (recommended) vs. a separate `attempts` table. Define the schema
  and whether it bumps `state.db` `user_version`.
- Branch: reset-to-base on the same name (recommended; preserves the `agent/<id>-<slug>` contract) vs.
  versioned `…-attempt-N` (cleaner history, but changes branch/PR naming and downstream assumptions).
- Should `rerun` also support `done` (re-do a merged change) or only failed/manual? Probably gated/opt-in.
- If the prior attempt's PR is still open, what happens to it on re-run (close/comment/leave)?

## 13. Acceptance criteria

- [ ] `worc rerun <task-id>` launches a fresh attempt of a terminal task without hand-editing
      `state.db`/the ledger/git; `--dry-run` writes nothing.
- [ ] The §19 gate admits exactly the named id (no `DUPLICATE_TASK_ID`) while still rejecting any other
      duplicate; all other gate checks run.
- [ ] The attempt branch starts from the **current** `base_branch` (no stale commits from the prior
      attempt), so an advanced `main` does not cause a re-run merge conflict; the resume (crash-recovery)
      branch-reuse path is unchanged and still idempotent.
- [ ] The ledger keeps the prior attempt's record and gains a new one linked to it; prior-attempt
      artifacts/HITL are not reused.
- [ ] A `rerun` while another task is active is refused (single-active-task invariant).
- [ ] Tests cover the gate allowance, state/branch reset, the single-slot refusal, and a re-run after
      `base_branch` advanced.

## 14. References

- Resume/recovery: `core/recovery.py`; `core/orchestrator.py` `resume`/`_resume_task`/`_resume_cleanup`.
- Terminal handling: `core/orchestrator.py` `_fail`/`_go_terminal`/`_move_task_file`;
  `git_manager.py` `terminal_cleanup`.
- Gate: `task/validation_gate.py` (`is_recovery_rerun`, `DUPLICATE_TASK_ID`); `state_store.py`
  `task_id_exists`/`find_active_tasks`; `ledger.py` `has_task_id`.
- Branch/base: `git_manager.py` `prepare_branch`. State machine: `core/state_machine.py`.
- Origin of this item: [post_test_run_review.md](post_test_run_review.md) (the failed
  `task-pr-title-override` run) and [follow_ups.md](follow_ups.md).
