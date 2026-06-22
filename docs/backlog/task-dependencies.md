# Task dependencies (`depends_on`) — non-blocking merge-gated scheduling

Status: **accepted** Date: 2026-06-22 Owner: Vladimir Makarevich

Detail file for the [follow_ups.md](follow_ups.md) item "Task dependencies (`depends_on`)". This **subsumes** the older "Capture real merge SHA in `--auto` auto-merge mode" follow-up: the real merge SHA is backfilled as a byproduct of the dependency readiness probe (see [§ Real merge-SHA backfill](#real-merge-sha-backfill)).

## Problem

Tasks are processed single-slot, in deterministic filename order ([cli.py:532-565](../../src/wastech_orchestrator/cli.py#L532-L565)). Each task branches from a freshly pulled `base_branch` ([git_manager.py:240-249](../../src/wastech_orchestrator/git_manager.py#L240-L249)). When task A uses GitHub-native async auto-merge (`gh pr merge --auto`, `git.auto_merge_wait_for_checks: true`), `merge_pr` returns `"armed"` immediately ([git_manager.py:646-659](../../src/wastech_orchestrator/git_manager.py#L646-L659)) — the merge lands later, when checks pass. If, with auto-mode on, the next task B is picked back-to-back, its `pull --ff-only` of `base_branch` does **not** yet contain A's changes. A task B that needs A's work is then built on a stale main.

There is no concept of dependencies **between tasks** today (`depends_on` exists only for sub-tasks within one task's decomposition — [state_store.py:402](../../src/wastech_orchestrator/state_store.py#L402), unrelated).

## Design: `depends_on` as a non-blocking readiness gate

Not synchronous blocking. A task declares the tasks it needs merged; the scheduler **skips** it while a dependency is unmerged and runs other eligible tasks instead. The slot never idles on CI — it does useful work and re-evaluates the dependent on a later tick (the watch loop already re-scans pending and refreshes main each tick when the slot is free — [cli.py:578-596](../../src/wastech_orchestrator/cli.py#L578-L596)).

Front-matter:

```yaml
depends_on: [task-a, task-b] # this task may start only after task-a AND task-b have merged
```

Scheduling rule: a pending task is **eligible** iff every id in `depends_on` is **merged**. Among eligible pending tasks the scheduler keeps the existing deterministic order; ineligible ones are skipped this pass. When B becomes eligible (its deps merged), `prepare_branch` pulls a `base_branch` that now includes them — the whole point.

### What "merged" means (readiness signal)

For each dependency id A:

- **A has a PR** → probe `gh pr view <A.pr_url> --json state,mergeCommit` through the safe runner. `state == "MERGED"` → satisfied (and backfill the SHA, below). `OPEN` → not yet (wait). `CLOSED` (unmerged) → not satisfied (wait — see policy below).
- **A has no PR** (local-commit mode) and A is terminal `DONE` → satisfied (its commits are already on `base_branch`).
- **A still in-flight / pending / not terminal** → not satisfied (wait).

The PR URL / terminal state come from the store + ledger (`recorded_pr_url(task_id)`).

### Unsatisfiable dependency → wait forever (no auto-fail)

If a dependency is terminal-but-unmerged (A failed, went `manual_action_required`, or its PR was closed unmerged), the dependent stays pending and is skipped every pass — **indefinitely**, until the operator removes or fixes it. The orchestrator never auto-fails a dependent. (Recommended advisory: a periodic log line "task B waiting on unmerged dependency A" — diagnostic only, no state change.)

This is distinct from a **broken** dependency (next section), which is a malformed task, not a waiting one.

### Cycles and broken references → fail-closed

Resolved at scheduling time (the per-task validation gate sees one task in isolation and cannot see the graph):

- **Cycle** among pending tasks (A→B→A, self-dependency) → reject the involved tasks terminally with a clear reason. A cycle is unambiguously broken regardless of timing.
- **Unknown reference** — a `depends_on` id that resolves to no known task (neither pending nor a terminal store record) → reject the dependent with a clear "depends on unknown task X" message. Contract implication (document it): a dependency must already exist (pending or terminal) when the dependent is evaluated — add A before/with B. This is the deliberate cost of fail-closed; a "not added yet" id is treated as a typo.

The per-task gate still does **shape** validation: `depends_on` is a list of non-empty strings; self-reference is rejected there cheaply.

### Explicit `run <file>` of a dependent

In `watch`/auto the gate just skips an ineligible task. For an explicit `run <file>` whose dependency is unmerged, **refuse** with a controlled message and non-zero exit — never run it on a stale main ([cli.py:610-626](../../src/wastech_orchestrator/cli.py#L610-L626)).

## Real merge-SHA backfill

The readiness probe already reads `gh pr view --json state,mergeCommit`. When it observes `state == "MERGED"` for a dependency A whose recorded `merge_outcome` is `"armed"`, update A's `pr_merge` publish op `result_ref` and A's ledger `merge_outcome` to the real `mergeCommit.oid` ([git_manager.py:661-666](../../src/wastech_orchestrator/git_manager.py#L661-L666) already extracts it for immediate merges; reuse it). This closes the original "capture real merge SHA in `--auto` mode" gap — for any task that has a dependent.

**Out of scope (known limitation):** an armed PR that **no** task depends on is never probed, so its `merge_outcome` stays `"armed"`. A general watch-tick reconciler that backfills every armed PR for pure audit completeness is a separate, optional follow-up — not built here (the user opted to fold SHA capture into the dependency gate only).

## Change list

- `task/model.py` — `NormalizedTask` gains `depends_on: tuple[str, ...]` (default empty).
- `task/parser.py` — parse the `depends_on` front-matter list; round-trip it in `write_normalized` / `load_normalized`.
- `task/validation_gate.py` — shape validation (list of non-empty strings; reject self-reference) with a new `ValidationReason` (e.g. `INVALID_DEPENDS_ON`).
- `core/orchestrator.py` — a `dependencies_satisfied(task) -> Eligibility` method: resolve each dep against store/ledger, probe PR merge state via `git`, backfill SHA on `MERGED`, return satisfied / waiting / unresolvable(cycle|unknown). The explicit-`run` refusal and the cycle/unknown terminal rejection live here.
- `git_manager.py` — a read-only `pr_merge_state(pr_url) -> (state, sha|None)` helper (reusing `_gh` + the `mergeCommit.oid` extraction); a `backfill_merge_sha(task_id, pr_url)` that updates the `pr_merge` publish op when armed→merged.
- `cli.py` — `watch_once` skips ineligible pending tasks (lightweight front-matter read of `depends_on` for the skip decision; full validation still happens in `run_task`); `cmd_run` refuses an explicit dependent with an unmerged dep. Cross-task cycle detection over the pending set runs in selection.
- No new persistent schema required — `depends_on` lives in the task file; eligibility is computed live from PR/merge state. (Cycle detection is over the in-memory pending set + terminal records.)

## Tests

- B `depends_on: [A]`: A merged → B runs; A armed/unmerged → B skipped while independent C runs; A closed-unmerged or failed → B waits indefinitely (never auto-failed).
- Multiple deps: B eligible only when **all** of `[A, C]` merged.
- Cycle A↔B and self-dependency → terminal reject, clear reason.
- Unknown dep id → terminal reject (broken ref).
- `run B` while A unmerged → refused, non-zero exit, no branch/side effect.
- SHA backfill: A armed → probe sees `MERGED` → A's `pr_merge` op + ledger updated to the real `mergeCommit.oid`.
- `depends_on` shape: non-list / non-string / empty-string / self-ref → reject.
- Eligible-task ordering: a later-in-filename independent task runs ahead of an earlier ineligible dependent.

## Docs

- `docs/task-authoring.md` — document `depends_on` (list semantics; deps must already exist; non-blocking wait; explicit-`run` refusal; cycle/unknown rejection; unmerged-dep waits forever).
- `docs/operations.md` — scheduling behavior under `watch`/auto (skip ineligible, run independent), the wait-forever policy + operator intervention, and the merge-SHA backfill.
- Mark the "Capture real merge SHA in `--auto` auto-merge mode" follow-up row as subsumed by this task.

## Acceptance

- `ruff`, `mypy`, `pytest` green.
- With auto-mode on: a dependent task does not start until its declared dependencies have merged; independent tasks proceed meanwhile; once the dependencies merge, the dependent branches from a `base_branch` that includes them.
- An armed dependency's real merge SHA lands in its ledger record once observed merged.
- Cycles / unknown refs are rejected with a clear message; an explicit `run` of a blocked dependent is refused.
