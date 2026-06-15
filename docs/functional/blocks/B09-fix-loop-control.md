# B09 — Fix Loop Control

## Purpose

Deterministically guarantees task completion: counts fix cycles and decides whether a task is stuck. Replaces a supervisor agent with simple persistent counters with hard limits, to prevent infinite ping-pong of `review ↔ fixing` or `testing ↔ fixing`.

## Responsibilities

- Store per-task counters (`stage_attempts`, `test_fix_cycles`, `review_fix_cycles`, `fix_iterations`) ([loop_control.py:35-42](../../../src/wastech_orchestrator/core/loop_control.py#L35)).
- On entry into `fixing`, increment the relevant counters and decide whether the task is stuck and which limit has been exhausted ([loop_control.py:63-83](../../../src/wastech_orchestrator/core/loop_control.py#L63)).
- Reset counters when checks/review pass and when transitioning to the next subtask ([loop_control.py:85-103](../../../src/wastech_orchestrator/core/loop_control.py#L85)).

## Block Boundaries

### Within the block's responsibility

- Counter rules §8.1 and the "stuck / not stuck" decision with the name of the exhausted limit.

### Outside the block's responsibility

- **Persistence** of counters — that is [B07](./B07-state-machine-and-store.md) (`save_counters`/`get_counters`).
- **Ownership of `stage_attempts`** — it is counted by [B17 Router](./B17-agent-router-and-fallback.md); here it is only mirrored as the latest value ([loop_control.py:5-7](../../../src/wastech_orchestrator/core/loop_control.py#L5)).
- **The decision to enter `fixing`** and **writing the failure report** — that is [B06](./B06-orchestrator-pipeline.md)/[B08](./B08-ledger-and-failure-reports.md).

## Entry Points

- `LoopController(limits: AgentsConfig)` — constructed in `build_orchestrator` ([orchestrator.py:2637](../../../src/wastech_orchestrator/core/orchestrator.py#L2637)).
- `enter_fixing(counters, loop)` → `LoopDecision` ([loop_control.py:63](../../../src/wastech_orchestrator/core/loop_control.py#L63)) — [B06 `_enter_fixing`](./B06-orchestrator-pipeline.md) ([orchestrator.py:1480](../../../src/wastech_orchestrator/core/orchestrator.py#L1480)).
- `on_check_pass` / `on_review_pass` / `reset_for_next_subtask` ([loop_control.py:85-103](../../../src/wastech_orchestrator/core/loop_control.py#L85)).
- `FixLoop` (TEST/REVIEW), `LoopCounters`, `LoopDecision`.

## Input Data and State

`AgentsConfig` limits (`max_fix_cycles`, `max_total_fix_iterations`); mutable `LoopCounters` (passed by the caller). The controller holds no state of its own.

## Main Scenario (`enter_fixing`)

1. `fix_iterations += 1`; depending on `loop`, either `test_fix_cycles` or `review_fix_cycles` is incremented.
2. If the cycle has reached `max_fix_cycles` → `stuck`, `limit_name="max_fix_cycles"` (checked first).
3. Otherwise, if `fix_iterations` has reached `max_total_fix_iterations` → `stuck`, `limit_name="max_total_fix_iterations"`.
4. Otherwise `stuck=False` — [B06](./B06-orchestrator-pipeline.md) transitions to `fixing`.

`enter_fixing` decision: the per-loop limit (`max_fix_cycles`) is checked before the global limit (`max_total_fix_iterations`):

```mermaid
flowchart TB
    start(["enter_fixing(counters, loop)"]) --> inc["fix_iterations += 1;<br/>test_fix_cycles or review_fix_cycles += 1 (by loop)"]
    inc --> c1{"cycle reached max_fix_cycles?"}
    c1 -->|yes| stuck1["stuck, limit = max_fix_cycles"]
    c1 -->|no| c2{"fix_iterations reached max_total_fix_iterations?"}
    c2 -->|yes| stuck2["stuck, limit = max_total_fix_iterations"]
    c2 -->|no| go["not stuck → B06 enters fixing"]
    stuck1 --> manual["B06: manual_action_required + report (B08)"]
    stuck2 --> manual
```

## Checks and Constraints

- Two independent limits: per-loop (`max_fix_cycles`) and global hard stop (`max_total_fix_iterations`); the configuration validator requires `max_total_fix_iterations ≥ max_fix_cycles` ([B05](./B05-configuration.md)).
- `on_check_pass` resets only `test_fix_cycles`; `on_review_pass` resets both cycle counters.
- `reset_for_next_subtask` resets `stage_attempts` and both cycle counters, but **not** `fix_iterations` (it accumulates across all subtasks so that decomposition cannot bypass the hard stop) ([loop_control.py:94-103](../../../src/wastech_orchestrator/core/loop_control.py#L94)).

## Output

`LoopDecision(stuck, loop, limit_name)`; mutated `LoopCounters`.

## Side Effects

None — the module is pure (it only mutates the passed `LoopCounters`; it does not write to disk or the database).

## Errors and Edge Cases

- When both limits are triggered on the same entry, the per-loop limit (`max_fix_cycles`) is reported first.

## Relationships

### Uses

- [B05 — Configuration](./B05-configuration.md) — limits from `AgentsConfig`.

### Used by

- [B06 — Pipeline](./B06-orchestrator-pipeline.md) — test/review/fixing loop control.
- [B07 — State Store](./B07-state-machine-and-store.md) — imports `LoopCounters` for counter persistence.

## Role in the Overall System

This is the "circuit breaker" for the fix loop: after a test/review failure, [B06](./B06-orchestrator-pipeline.md) asks this block whether another fix attempt is allowed; when the limit is exhausted, the task moves to `manual_action_required` with a failure report ([B08](./B08-ledger-and-failure-reports.md)).

## Code Confirmation

- [core/loop_control.py:56-103](../../../src/wastech_orchestrator/core/loop_control.py#L56) — `LoopController` and all counter rules.
- Test: [tests/core/test_loop_control.py](../../../tests/core/test_loop_control.py) — increments, both limits, resets, accumulation of `fix_iterations` across subtasks.
