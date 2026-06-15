# B07 — State Machine and State Store

## Purpose

Defines the task status vocabulary and the allowed transitions between statuses, and persists all pipeline state in a single SQLite database (`state.db`). This is the "backbone" of state: a single processing slot, transactional transitions (crash-safe), and idempotent writes that allow an interrupted task to be resumed.

## Responsibilities

- **State machine** (pure, no IO): enumerate statuses (§8), define allowed transitions, validate/assert transitions ([state_machine.py:15-143](../../../src/wastech_orchestrator/core/state_machine.py#L15)).
- **State Store**: create/migrate the database and apply the schema ([state_store.py:328-340](../../../src/wastech_orchestrator/state_store.py#L328)); store 7 entities: `tasks`, `stage_runs`, `provider_attempts`, `check_runs`, `artifacts`, `publish_operations`, `subtasks` ([state_store.py:83-196](../../../src/wastech_orchestrator/state_store.py#L83)).
- Provide `BEGIN IMMEDIATE`…`COMMIT`/`ROLLBACK` transactions ([state_store.py:359-368](../../../src/wastech_orchestrator/state_store.py#L359)).
- Answer the question "who owns the slot" ([state_store.py:455-460](../../../src/wastech_orchestrator/state_store.py#L455)).
- Version the database schema and reject a newer version ([state_store.py:63-81](../../../src/wastech_orchestrator/state_store.py#L63)).

## Block Boundaries

### Within scope

- Status vocabulary and transition table (policy).
- Persistence and reading of all 7 entities; loop counters; idempotent upserts.
- Querying active tasks (slot) and tasks with incomplete cleanup (recovery).
- Transactions, schema migration and versioning, read-only mode.

### Out of scope

- **Deciding which transition to make** — that is [B06 Pipeline](./B06-orchestrator-pipeline.md): it calls `assert_transition` then `set_status` inside a transaction ([orchestrator.py:2434-2445](../../../src/wastech_orchestrator/core/orchestrator.py#L2434)). The store itself does not choose transitions; `set_status` writes any given status.
- **Secret redaction**: the store writes exactly what it is given; the responsibility of "do not pass a secret into a field" lies with the callers ([state_store.py:8-11](../../../src/wastech_orchestrator/state_store.py#L8)).
- **Terminal outcome ledger** — that is a separate block [B08 Ledger](./B08-ledger-and-failure-reports.md).
- **Git operations themselves** — that is [B22](./B22-git-manager.md); the store only holds idempotency strings for publishing (`publish_operations`).

## Entry Points

State machine: `Status`, `ALLOWED_TRANSITIONS`, `can_transition`, `assert_transition`, `is_terminal`, `is_active`, `InvalidTransition` ([state_machine.py](../../../src/wastech_orchestrator/core/state_machine.py)).

State Store ([state_store.py](../../../src/wastech_orchestrator/state_store.py)):

- `StateStore.open(db_path)` / `open_readonly(db_path)` ([:328,:342](../../../src/wastech_orchestrator/state_store.py#L328)).
- `transaction()` ([:359](../../../src/wastech_orchestrator/state_store.py#L359)).
- Tasks: `insert_task`, `get_task`, `latest_task`, `task_id_exists`, `find_active_tasks`, `find_incomplete_cleanup`, `update_task`, `set_status`, `reset_task_for_rerun`, `revive_task_for_continue`.
- Counters: `get_counters`, `save_counters`.
- Records: `record_stage_run`, `record_skip`, `complete_stage_run`, `record_provider_attempt`, `record_check_run`, `latest_failed_check_log`, `register_artifact`.
- Publish idempotency: `record_publish_op`, `get_publish_op`, `clear_publish_operations`.
- Subtasks: `insert_subtasks`, `get_subtasks`, `set_subtask_commit`.

## Input Data and State

Path to `state.db`; dataclass row objects (`TaskRow`, `StageRunRow`, …). Internal state is a single SQLite connection (WAL, `foreign_keys=ON`, manual transaction management).

## Main Scenario (task status transition)

1. [B06](./B06-orchestrator-pipeline.md) opens a transaction via `transaction()` (`BEGIN IMMEDIATE`).
2. Inside: `assert_transition(src, dst)` (state machine policy) → `set_status(task_id, dst)` → `save_counters(...)` → optional `update_task(...)`.
3. On success: `COMMIT`; on exception: `ROLLBACK` — the database remains in the previous consistent state.

Each status transition happens inside a single transaction: first the §8 check, then the write; on exception — full rollback:

```mermaid
flowchart TB
    start(["B06: status change needed"]) --> tx["transaction(): BEGIN IMMEDIATE"]
    tx --> assert{"assert_transition(src, dst):<br/>is the transition allowed per §8?"}
    assert -->|no| inv["InvalidTransition → ROLLBACK"]
    assert -->|yes| setp["set_status → save_counters<br/>→ optional update_task"]
    setp --> ok{"exception raised?"}
    ok -->|no| commit["COMMIT — new state is persistent"]
    ok -->|yes| rb["ROLLBACK — database in previous consistent state"]
```

## Alternative Scenarios

### Out-of-band (operator-driven) statuses

`finalize` and `recovery` set a terminal status directly via `set_status`/`update_task` **without** `assert_transition` (intentional out-of-band change) — for example, `revive_task_for_continue` performs terminal→active for `rerun --continue` ([state_store.py:533-554](../../../src/wastech_orchestrator/state_store.py#L533)).

### Reset/revival on rerun

`reset_task_for_rerun` clears all per-attempt state, resets the branch, and deletes `subtasks` + `publish_operations`, leaving the status terminal (so that a subsequent upsert in `insert_task` moves it to `new`) ([state_store.py:491-531](../../../src/wastech_orchestrator/state_store.py#L491)).

### Read-only mode

`open_readonly` opens the file with `?mode=ro`, sets `PRAGMA query_only=ON`, and checks (without migrating) the schema version; used by the `status` command ([state_store.py:342-352](../../../src/wastech_orchestrator/state_store.py#L342)).

## Checks and Constraints

- **Allowed transitions** (§8): the "happy path" plus universal edges `-> failed` and `-> manual_action_required` for every non-terminal status; terminal statuses have no outgoing edges ([state_machine.py:70-113](../../../src/wastech_orchestrator/core/state_machine.py#L70)).
- **Single slot**: all statuses except `{NEW, PENDING, DONE, FAILED, MANUAL_ACTION_REQUIRED}` are considered active ([state_store.py:455-460,872-875](../../../src/wastech_orchestrator/state_store.py#L455)). This is a logical slot (a database query), not a DBMS lock.
- **Schema version** = 3; a newer version raises `IncompatibleStateError` (on both open paths); an older version is migrated in-place with idempotent `ALTER TABLE ADD COLUMN` and re-claimed ([state_store.py:40-81](../../../src/wastech_orchestrator/state_store.py#L40)).
- **Idempotency**: `insert_task`/`register_artifact`/`record_publish_op`/`insert_subtasks` are upserts keyed on a unique key; `insert_subtasks` does not "revive" already-committed subtasks (`WHERE subtasks.commit_sha IS NULL`) ([state_store.py:818-837](../../../src/wastech_orchestrator/state_store.py#L818)).
- **No secrets** in the schema (only ids, statuses, error classes, paths, sha256, counters, fingerprints, commit SHAs) ([state_store.py:8-11](../../../src/wastech_orchestrator/state_store.py#L8)).

## Output

Persistent rows in `state.db`; `TaskRow`/`SubtaskRow`/… on read; `run_id` when reserving a stage; `LoopCounters` when reading counters; list of active/incomplete tasks.

## Side Effects

- Creation of the `state.db` file (+ WAL/SHM) and writes to it.
- Transactional mutations (commit/rollback).
- In read-only mode, writes are prohibited at the SQLite level (`query_only`).

## Errors and Edge Cases

- Newer schema version → `IncompatibleStateError` (caught by CLI → exit 2).
- `complete_stage_run`/`get_counters` on a non-existent id → `KeyError`.
- FK enabled: inserting a `stage_run` without a matching `tasks` row will violate the FK constraint (confirmed by test).

## Relationships

### Uses

- [B09](./B09-fix-loop-control.md) — `LoopCounters` type (imported for counters).
- [B07 state machine] — `Status` (used internally in the store).

### Used by

- [B06 — Pipeline](./B06-orchestrator-pipeline.md) — all transitions and records; `acquire_slot` via `find_active_tasks`.
- [B22 — Git Manager](./B22-git-manager.md) — reading/writing `publish_operations` (idempotency).
- [B10 — Recovery](./B10-recovery-and-resume.md) — `find_active_tasks`/`find_incomplete_cleanup`.
- [B16 — Validation Gate](./B16-task-parsing-and-validation-gate.md) — `task_id_exists` (id deduplication).
- [B01 — CLI](./B01-cli-and-operator-commands.md) — `open_readonly` for the `status` command.

## Place in the Overall System

All pipeline decisions materialize as status transitions and rows in `state.db`. Transactionality and idempotency here are the foundation of resumability: after a crash, [B10](./B10-recovery-and-resume.md) reads this state and decides whether to continue the task.

## Code References

- [core/state_machine.py:15-143](../../../src/wastech_orchestrator/core/state_machine.py#L15) — statuses, transition table, checks.
- [state_store.py:83-196](../../../src/wastech_orchestrator/state_store.py#L83) — schema of 7 tables.
- [state_store.py:328-352](../../../src/wastech_orchestrator/state_store.py#L328) — open/open_readonly, pragmas, version.
- [state_store.py:455-471](../../../src/wastech_orchestrator/state_store.py#L455) — slot and incomplete cleanup.
- Tests: [test_state_machine.py](../../../tests/core/test_state_machine.py), [test_state_store.py](../../../tests/state/test_state_store.py), [test_db_schema_version.py](../../../tests/state/test_db_schema_version.py) — transitions, slot, transactions (commit/rollback), upsert, rejection of newer version, absence of secret columns.
