# B09 — Fix Loop Control

> Reconstructed from code (`core/loop_control.py`, `core/flow/run_state.py`) and tests (`tests/core/test_supervisor.py`, `tests/state/test_state_store.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/core/loop_control.py`, `src/wastech_orchestrator/core/flow/run_state.py`

## Responsibility

Since the flow engine, this block is no longer a bespoke loop controller — it is the **shared rework-accounting primitive** plus the **operator-facing loop counters**. Two things live here: (1) `record_rework`, the single accounting path that increments the one global fix counter on every in-flow rework/fail edge ([loop_control.py:37](../../../src/wastech_orchestrator/core/loop_control.py#L37)); and (2) `LoopCounters`, the mutable per-task counter struct persisted on the `tasks` row that backs the ledger, the CLI `status` line, and the failure report ([loop_control.py:27](../../../src/wastech_orchestrator/core/loop_control.py#L27)). The `FlowRunState` checkpoint that physically holds the counters during a traversal also lives here ([run_state.py:30](../../../src/wastech_orchestrator/core/flow/run_state.py#L30)).

The actual bounding — comparing a counter against a cap and ending the run when a budget is exhausted — is **not** in this block. That is generic engine bookkeeping owned by B28 ([engine.py:355](../../../src/wastech_orchestrator/core/flow/engine.py#L355)). This module carries no flow domain knowledge: it does not know the names `test_fix` / `review_fix` (those are YAML edges) and it does not decide termination.

## Public surface

- `LoopCounters` ([loop_control.py:27](../../../src/wastech_orchestrator/core/loop_control.py#L27)) — dataclass: `stage_attempts`, `test_fix_cycles`, `review_fix_cycles`, `fix_iterations` (all default `0`); the per-task counters persisted on the `tasks` row.
- `record_rework(run_state) -> int` ([loop_control.py:37](../../../src/wastech_orchestrator/core/loop_control.py#L37)) — the single global accounting path; bumps `FlowRunState.loop_counters["global_fix_iterations"]` and returns the new value.
- `FlowRunState` ([run_state.py:30](../../../src/wastech_orchestrator/core/flow/run_state.py#L30)) — the mutable per-traversal checkpoint holding `loop_counters` (`dict[str, int]`), with `bump` / `counter` / `reset` / `reset_for_next_subtask` and the `fix_iterations` convenience property.
- `FlowRunState.GLOBAL_FIX_KEY` ([run_state.py:37](../../../src/wastech_orchestrator/core/flow/run_state.py#L37)) — the reserved counter key `"global_fix_iterations"`.

## Behavior

### The accounting primitive

`record_rework` is a one-liner over the checkpoint: `return run_state.bump(run_state.GLOBAL_FIX_KEY)` ([loop_control.py:50](../../../src/wastech_orchestrator/core/loop_control.py#L50)). Its purpose is to be the **one** place a rework is counted globally. Every in-flow rework/fail edge — the test-driven loop and the review-driven loop alike — charges its global cost here and only here, so a rework can never be double-incremented. `test_record_rework_single_increment` anchors this: two calls return `1` then `2`, and afterwards only the `GLOBAL_FIX_KEY` key exists in `loop_counters` ([test_supervisor.py:242](../../../tests/core/test_supervisor.py#L242)). The immutable per-verdict record lives in the `evaluations` table written by the evaluator node (B30); recording a verdict never touches this counter.

### The counter struct and its keys

`LoopCounters` is pure data — no methods, no enforcement. Its four fields back distinct surfaces:

- `fix_iterations` — the single global per-task counter; mirrors `FlowRunState.fix_iterations`, which reads `GLOBAL_FIX_KEY` ([run_state.py:47](../../../src/wastech_orchestrator/core/flow/run_state.py#L47)).
- `test_fix_cycles` / `review_fix_cycles` — the length of the _current consecutive_ named-loop cycle, mirrored from the runtime counters keyed `"test_fix"` / `"review_fix"`.
- `stage_attempts` — attempts of a single stage run including provider fallback; owned and counted by the Router (`StageOutcome.stage_attempts`), not by this block. See the audit note below.

`FlowRunState.loop_counters` is a single `dict[str, int]` carrying three key flavours without per-flow special cases ([run_state.py:13](../../../src/wastech_orchestrator/core/flow/run_state.py#L13)): a named loop's name, a synthetic inline-budget edge key, and the reserved `GLOBAL_FIX_KEY`. `reset_for_next_subtask` drops every per-loop / per-edge counter but **preserves** the global one, so the global fix budget accumulates across a decomposed task while each subtask gets fresh per-loop budgets ([run_state.py:69](../../../src/wastech_orchestrator/core/flow/run_state.py#L69)).

### How the engine bounds it (B28 — framing only, not this block)

The engine charges and caps each rework/fail edge in `_charge_rework` ([engine.py:355](../../../src/wastech_orchestrator/core/flow/engine.py#L355)): it calls `record_rework` for the global increment, then compares against the relevant cap. The effective ceilings are `min(flow_budget, config_cap)`:

- a **named-loop** edge (`loop: <name>`) is capped at `min(flow_budget, agents.max_fix_cycles)`, reported as `max_fix_cycles` ([engine.py:395](../../../src/wastech_orchestrator/core/flow/engine.py#L395));
- an inline **`budget: N`** edge (no named loop) uses `allow N` semantics keyed by `edge_key` ([engine.py:372](../../../src/wastech_orchestrator/core/flow/engine.py#L372));
- the **global** counter is capped at `min(flow_budget, agents.max_total_fix_iterations)`, reported as `max_total_fix_iterations` ([engine.py:398](../../../src/wastech_orchestrator/core/flow/engine.py#L398)).

The per-loop / inline cap is checked **before** the global cap when both could trip on the same entry. On exhaustion the engine returns `MANUAL_ACTION_REQUIRED` and writes a failure report ([engine.py:252](../../../src/wastech_orchestrator/core/flow/engine.py#L252)); a forward (non-rework) edge instead resets the loop/inline counter anchored at that node ([engine.py:381](../../../src/wastech_orchestrator/core/flow/engine.py#L381)). The packaged implementation flow declares the two named loops and their budgets — `test_fix: 15`, `review_fix: 15`, `global_fix_iterations: 30` — purely in YAML ([implementation.yaml:77](../../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml#L77)).

### Mirroring the engine's counters back into `LoopCounters`

The engine counts in `FlowRunState.loop_counters`; the `tasks` counter columns back the operator surfaces. The orchestrator (B06) reconciles them in `_sync_counters_from_run_state`, called before every terminal transition ([orchestrator.py:929](../../../src/wastech_orchestrator/core/orchestrator.py#L929)). It `replace`s `p.counters` with `fix_iterations` from the global counter and `test_fix_cycles` / `review_fix_cycles` from the named-loop counters ([orchestrator.py:938](../../../src/wastech_orchestrator/core/orchestrator.py#L938)) — so the surfaces do not read `0` after the engine ran fix loops. Independently, `save_flow_checkpoint` mirrors the global counter into the `tasks.fix_iterations` column on **every** checkpoint, so a live `status` reflects loops mid-run, not only at the terminal step ([state_store.py:854](../../../src/wastech_orchestrator/state_store.py#L854)).

Persistence round-trips through the state store: `save_counters` writes the four columns, `get_counters` reads them back into a `LoopCounters` ([state_store.py:709](../../../src/wastech_orchestrator/state_store.py#L709)). Resume rehydrates `p.counters` via `get_counters` ([orchestrator.py:733](../../../src/wastech_orchestrator/core/orchestrator.py#L733)). The counter then surfaces in the ledger entry's `fix_iterations` field ([ledger.py:75](../../../src/wastech_orchestrator/ledger.py#L75)), the CLI `status` line ([cli.py:1085](../../../src/wastech_orchestrator/cli.py#L1085)), and the failure report's `counters` block ([recorder.py:62](../../../src/wastech_orchestrator/core/flow/recorder.py#L62)).

## Invariants & guarantees

- A rework is counted globally **exactly once** — `record_rework` is the sole writer of `GLOBAL_FIX_KEY`, so no edge double-counts ([loop_control.py:50](../../../src/wastech_orchestrator/core/loop_control.py#L50), anchored by [test_supervisor.py:242](../../../tests/core/test_supervisor.py#L242)).
- `loop_control.py` holds **no** enforcement or domain knowledge — it never names a loop and never compares against a cap; bounding is the engine's ([engine.py:355](../../../src/wastech_orchestrator/core/flow/engine.py#L355)).
- The global fix counter survives decomposition subtask resets; per-loop / inline counters do not ([run_state.py:69](../../../src/wastech_orchestrator/core/flow/run_state.py#L69)).
- Config guarantees termination is reachable: `agents.max_total_fix_iterations >= agents.max_fix_cycles` is validated, else rejected ([validation.py:87](../../../src/wastech_orchestrator/config/validation.py#L87)); defaults are `max_fix_cycles=15`, `max_total_fix_iterations=30`, `max_stage_attempts=3` ([loader.py:404](../../../src/wastech_orchestrator/config/loader.py#L404)).
- The reserved key name cannot collide with a named loop (operator-chosen) or a synthetic edge key (contains `"->"`) ([run_state.py:34](../../../src/wastech_orchestrator/core/flow/run_state.py#L34)).

## Dependencies

- **Uses:** B07 (`get_counters` / `save_counters` / `save_flow_checkpoint` persist the counters on the `tasks` row), B05 (`agents.max_fix_cycles` / `max_total_fix_iterations` supply the config caps).
- **Used by:** B28 (`_charge_rework` calls `record_rework` and caps the counters), B06 (`_sync_counters_from_run_state` mirrors the engine counters back into `LoopCounters`), B08 (the ledger entry and failure report read the counters), B01 (the CLI `status` line prints `fix_iterations`), B17 (the Router produces `stage_attempts`, persisted into this struct's field).

## Audit candidates

- `src/wastech_orchestrator/core/loop_control.py:31` — `LoopCounters.stage_attempts` is vestigial on the `tasks` row in the flow-engine era — see [the audit](../../backlog/2026-06-21-audit.md). The Router's per-run `stage_attempts` is persisted only into `node_runs` via `complete_node_run` ([agent.py:423](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L423), [evaluator.py:260](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L260)); `_sync_counters_from_run_state` never sets it ([orchestrator.py:938](../../../src/wastech_orchestrator/core/orchestrator.py#L938)), so the `tasks.stage_attempts` column read by `get_counters` and surfaced to operators is permanently `0`.

## Tests

- [tests/core/test_supervisor.py:242](../../../tests/core/test_supervisor.py#L242) (`test_record_rework_single_increment`) — the single-increment invariant: `record_rework` advances only the global counter and never double-counts. (The former dedicated `tests/core/test_loop_control.py` was removed with the `LoopController` in commit `79dfa37`; this is its surviving anchor.)
- [tests/state/test_state_store.py:123](../../../tests/state/test_state_store.py#L123) — round-trips a fully-populated `LoopCounters` through `save_counters` / `get_counters`.
- [tests/core/test_flow_engine.py:327](../../../tests/core/test_flow_engine.py#L327) — exercises the engine caps that consume these counters: named-loop cap, the global cap as a hard stop, and inline `budget` edges (B28 coverage, listed here for the counter contract).
