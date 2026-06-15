# B22 — Git and GitHub Operations (Git Manager)

## Purpose

The sole component that commits, pushes, and opens Pull Requests — agents never do this. All git/gh calls go through a safe runner as an argv list (no shell, no interpolation of user strings) with an environment allowlist. Implements the invariants "only the orchestrator does commit/push/PR" and "launch without shell interpolation".

## Responsibilities

- Branch flow: `fetch` → checkout `base_branch` → `pull` → create/reuse `agent/<task-id>-<slug>` ([git_manager.py:278-293](../../../src/wastech_orchestrator/git_manager.py#L278)).
- **Scoped staging** (§21.1): only code paths + `:(exclude)tasks/…` — **never** `git add .` ([git_manager.py:526-549](../../../src/wastech_orchestrator/git_manager.py#L526)).
- Three footprint modes (external / in_repo+exclude_local / in_repo+commit) and runtime-excludes ([git_manager.py:383-438](../../../src/wastech_orchestrator/git_manager.py#L383)).
- Idempotent commit/push/PR/merge via `publish_operations` + remote state verification ([git_manager.py:559-748](../../../src/wastech_orchestrator/git_manager.py#L559)).
- Terminal cleanup to `base_branch` when provably safe ([git_manager.py:805-829](../../../src/wastech_orchestrator/git_manager.py#L805)).
- `SnapshotHook` implementation for capturing partial changes ([git_manager.py:442-462](../../../src/wastech_orchestrator/git_manager.py#L442)).

## Block Boundaries

### Within this block's responsibility

- All git/gh operations, publication idempotency, footprint/excludes, working tree snapshots, redaction of stderr/diffs before writing.

### Outside this block's responsibility

- **When** to commit/push/create PR and status transitions — that is [B06](./B06-orchestrator-pipeline.md).
- **Process launch security** — that is [B19](./B19-subprocess-runner.md).
- **Redaction rules** — [B21](./B21-secret-redaction.md) (Git Manager applies them).
- **Environment allowlist** — [B25](./B25-security-policy.md).
- **Partial change contract shape** — that is [B17/snapshots](./B17-agent-router-and-fallback.md); Git Manager implements it.
- **Writing `check_runs`/ledger** — that is [B07](./B07-state-machine-and-store.md)/[B08](./B08-ledger-and-failure-reports.md).

## Entry Points

- `GitManager(...)` ([git_manager.py:200](../../../src/wastech_orchestrator/git_manager.py#L200)); constructed in `build_orchestrator` ([orchestrator.py:2617](../../../src/wastech_orchestrator/core/orchestrator.py#L2617)).
- Branch/diffs/publication/cleanup: `prepare_branch`, `reset_branch_to_base`, `delete_branch`, `commit_code`/`commit_subtask`/`commit_audit`, `push`, `create_pr`, `merge_pr`, `write_current_diff`/`cumulative_committed_diff`/`diff_stat`, `terminal_cleanup`, `preflight_footprint`/`ensure_exclude_local`/`ensure_runtime_excludes`.
- Read probes: `unaccounted_dirty_paths`, `remote_branch_exists`, `recorded_pr_url`, `verify_pr_state`, `refresh_base`, `commit_on_branch`.
- `SnapshotHook`: `capture`, `partial_change_since` (calls [B17](./B17-agent-router-and-fallback.md)).
- Module-level function `append_runtime_excludes(repo_root, *, tracked=False)` ([git_manager.py:100](../../../src/wastech_orchestrator/git_manager.py#L100)) — [B03 init/install](./B03-installer-and-scaffolding.md).

## Input Data and State

`OrchestratorConfig` (repo, footprint, security, auto_merge), `StateStore` (for `publish_operations`), `artifacts_root`. Internal state — the current active task `_ActiveTask` (for partial diff paths) and the environment allowlist built once in the constructor.

## Main Scenario (publishing a successful task)

1. `commit_code` — scoped staging of code paths and a single commit (or current HEAD if there is nothing to change).
2. `commit_audit` — when `tracking=commit`, a separate commit of `tasks/` only (lifecycle + `summary.md`); `logs/` is not committed.
3. `push` — push `agent/<id>-<slug>` to `origin` (refuses to push to base).
4. `create_pr` — `gh pr create` with body from `summary.md`.
5. (opt.) `merge_pr` — `gh pr merge --<strategy> [--auto]`.
6. `terminal_cleanup` — checkout `base_branch` if the working tree is safe.

All steps are idempotent: a repeated call after a restart checks `publish_operations` and/or the remote state and does not duplicate the operation.

```mermaid
flowchart TB
    start(["publish (when — decided by B06)"]) --> cc["commit_code: scoped staging of code paths<br/>(NEVER git add .) → single commit"]
    cc --> ca["commit_audit (if tracking=commit):<br/>separate commit of tasks/ only"]
    ca --> push["push: agent/id-slug to origin<br/>(refuses to push to base_branch)"]
    push --> pr["create_pr: gh pr create, body from summary.md"]
    pr --> mg{"auto_merge?"}
    mg -->|yes| merge["merge_pr: gh pr merge --strategy<br/>(never --admin/force, single attempt)"]
    mg -->|no| clean
    merge --> clean["terminal_cleanup: checkout base,<br/>if working tree is safe"]
    idem["idempotency: publish_operations (B07)<br/>+ remote state check"] -.-> push
    idem -.-> pr
    idem -.-> merge
```

## Alternative Scenarios

### Partial Changes (SnapshotHook)

`capture` takes a snapshot of HEAD/porcelain/diff-checksum; `partial_change_since` writes `logs/<task>/partial/NNN.diff` and returns `PartialChange` (without rollback) when the diff has changed ([git_manager.py:451-475](../../../src/wastech_orchestrator/git_manager.py#L451)).

### Rerun (branch reset)

`reset_branch_to_base`: checkout base, optionally delete the remote branch (closes PR), force-delete the local branch — so that a fresh `prepare_branch` recreates it from the current base ([git_manager.py:295-316](../../../src/wastech_orchestrator/git_manager.py#L295)).

### Already-merged PR

`merge_pr` on failure with a marker "already merged/not open/was merged" treats this as an idempotent success (`"merged"`), otherwise raises `GitCommandError` ([git_manager.py:739-748](../../../src/wastech_orchestrator/git_manager.py#L739)).

## Checks and Constraints

- **argv list, no shell**; stderr is always redacted ([git_manager.py:225-254](../../../src/wastech_orchestrator/git_manager.py#L225)).
- **Never `git add .`** — only explicit pathspec + `:(exclude)` for artifact directories ([git_manager.py:526-538](../../../src/wastech_orchestrator/git_manager.py#L526)).
- Refuses to push directly to `base_branch` (§12.12) → `GitCommandError` ([git_manager.py:654-658](../../../src/wastech_orchestrator/git_manager.py#L654)).
- `merge_pr` **never** uses `--admin`/force, exactly one attempt (branch protections are preserved) ([git_manager.py:733-737](../../../src/wastech_orchestrator/git_manager.py#L733)).
- footprint preflight: refuses to start if the repository already tracks a path that the footprint must keep outside git → `ManualActionRequired` ([git_manager.py:398-419](../../../src/wastech_orchestrator/git_manager.py#L398)).
- Environment — allowlist only; git/gh credentials are configured outside the orchestrator ([git_manager.py:217](../../../src/wastech_orchestrator/git_manager.py#L217)).
- `verify_pr_state`/`recorded_pr_url`/`refresh_base`/`fetch` — best-effort (do not raise errors).

## Output

Branch creation/switching; commits (SHA); push; PR URL; merge marker; `CleanupOutcome`; diffs on disk; `PartialChange`. Idempotent markers are written to `publish_operations` ([B07](./B07-state-machine-and-store.md)).

## Side Effects

- Git mutations (branches, commits), network (`fetch`/`pull`/`push`/PR/merge via `gh`).
- Files: `logs/<task>/current.diff`, `partial/NNN.diff`, `publish/terminal-cleanup.json`, entries in `.git/info/exclude` or `.gitignore`.
- `publish_operations` rows in the State Store (idempotency).
- Heartbeat log during long operations.

## Errors and Edge Cases

- Failed required git/gh call → `GitCommandError`.
- footprint tracks a forbidden path → `ManualActionRequired`.
- Blocked merge (branch protection/conflict) → `GitCommandError` (Core sets `manual_action_required`, PR remains open).
- Dirty working tree during cleanup → `CleanupOutcome(safe=False)`; status becomes `manual_action_required` on successful publication.

## Relationships

### Uses

- [B19 — Subprocess Runner](./B19-subprocess-runner.md) — `run_process`.
- [B21 — Redaction](./B21-secret-redaction.md) — redaction of stderr and diffs; `read_denied_secrets`.
- [B25 — Security](./B25-security-policy.md) — `build_child_env`.
- [B07 — State Store](./B07-state-machine-and-store.md) — `publish_operations` (idempotency).
- [B27 — Observability](./B27-observability.md) — heartbeat and logging.
- [B17/snapshots](./B17-agent-router-and-fallback.md) — `WorkingTreeSnapshot`/`PartialChange` types.

### Used by

- [B06 — Pipeline](./B06-orchestrator-pipeline.md) — the entire git and publication flow.
- [B17 — Router](./B17-agent-router-and-fallback.md) — as `SnapshotHook` (snapshot/partial diff).
- [B03 — Installer](./B03-installer-and-scaffolding.md) — `append_runtime_excludes`.
- [B01 — CLI](./B01-cli-and-operator-commands.md) — read probes via the rerun/finalize plan in [B06](./B06-orchestrator-pipeline.md).

## Place in the Overall System

Git Manager is the system's gateway to Git/GitHub. It upholds the invariant "only the orchestrator publishes", isolates the core from git syntax, and makes publication resilient to failures (idempotency) and safe (scoped staging, refusal to push to base, no `--admin`).

## Code Evidence

- [git_manager.py:225-271](../../../src/wastech_orchestrator/git_manager.py#L225) — argv launch, stderr redaction, `_git_checked`/`_gh`.
- [git_manager.py:278-438](../../../src/wastech_orchestrator/git_manager.py#L278) — branch flow, footprint, runtime-excludes.
- [git_manager.py:479-643](../../../src/wastech_orchestrator/git_manager.py#L479) — scoped staging, idempotent commits, audit commit.
- [git_manager.py:647-748](../../../src/wastech_orchestrator/git_manager.py#L647) — push/PR/merge (idempotent, no `--admin`).
- [git_manager.py:805-868](../../../src/wastech_orchestrator/git_manager.py#L805) — terminal cleanup + artifact.
- Test: [tests/git/test_git_manager.py](../../../tests/git/test_git_manager.py) — `agent/<id>-<slug>` branch, absence of `git add .`, footprint/excludes, push/PR/merge idempotency, already-merged, refusal to push to base, redacted diff, terminal cleanup.
