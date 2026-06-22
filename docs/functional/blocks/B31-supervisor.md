# B31 — Supervisor Oversight Layer

> Reconstructed from code (`src/wastech_orchestrator/core/supervisor.py`) and tests (`tests/core/test_supervisor.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `core/supervisor.py`

## Responsibility

The supervisor is a **constant orchestrator-level oversight layer that exists for every task under any flow shape** — even a single implement agent with no checks/review. It is **not a graph node** ([supervisor.py:2-8](../../../src/wastech_orchestrator/core/supervisor.py#L2)). It starts at task start, lives the whole cycle, observes each completed step read-only through its **own** session, and at whole-task close synthesizes the `summary` plus advisory caveats. It replaces the old summary provider and the removed blocking `supervise_impl` / `supervise_fix` nodes (the 2026-06-19 revision) — which is why there is no `summary` node in the packaged flows ([B29](B29-flow-definition-and-validation.md)) and why the pipeline no longer has a `summary` stage.

It is **advisory by construction**: it never reworks, reopens, or routes. Blocking is the job of the in-flow `review` / `test_quality` evaluators ([B30](B30-flow-node-runners.md)).

## Public surface

- `Supervisor` ([supervisor.py:51](../../../src/wastech_orchestrator/core/supervisor.py#L51)) — one instance per task (it carries its own session).
- `observe(...)` ([supervisor.py:80](../../../src/wastech_orchestrator/core/supervisor.py#L80)) — observe one completed step; record an advisory `supervisor_step` evaluation row.
- `finalize(...)` ([supervisor.py:108](../../../src/wastech_orchestrator/core/supervisor.py#L108)) — synthesize the whole-task summary (once, at close); record `supervisor_final`.
- `SupervisorStorePort` ([supervisor.py:45](../../../src/wastech_orchestrator/core/supervisor.py#L45)) — the slice of the state store it needs (append an immutable evaluation row).

## Behavior

### Per-step observation

The engine's `PostNodeHook` ([B28](B28-flow-engine.md)) calls `observe` after each executed (non-skipped) node, keyed by the step's `node_run_id`. `observe` ([supervisor.py:80](../../../src/wastech_orchestrator/core/supervisor.py#L80)) runs one read-only LLM turn on the supervisor's own session and records an immutable `supervisor_step` row in `evaluations` ([B07](B07-state-machine-and-store.md)), namespaced by `source_node_run_id` so a resumed run does not duplicate observations. It is **best-effort**: a failed observation is logged and swallowed — it is advisory and must never fail or reroute the task ([supervisor.py:159-164](../../../src/wastech_orchestrator/core/supervisor.py#L159)).

### Whole-task finalize

`finalize` ([supervisor.py:108](../../../src/wastech_orchestrator/core/supervisor.py#L108)) synthesizes a plain-language whole-task summary, writes the working `summary.{md,json}` under the task artifact dir, and records `supervisor_final`. The committed `summary.md` becomes the PR body. When the synthesis LLM call cannot run, `finalize` returns `None` and writes no `summary.md`, so the orchestrator's deterministic **minimal-summary fallback** applies ([B08](B08-ledger-and-failure-reports.md)) — the summary is _always_ written, by one path or the other. `summary.json` (local-only metadata) is always written ([supervisor.py:222-235](../../../src/wastech_orchestrator/core/supervisor.py#L222)). In the packaged `implementation` flow, `finalize` runs before the publish node so the summary rides the task's own audit commit.

### The supervisor's own session

`_run` ([supervisor.py:136](../../../src/wastech_orchestrator/core/supervisor.py#L136)) runs one read-only turn through the router, continuing the supervisor's own `resume_own_lineage` session. The session is **durable**: `_resume_session` resumes the in-memory `_own_session_id`, or — on a fresh process after a restart — the persisted `node_lineage` row (gated by provider match, exactly like the `resume_own_lineage` evaluator in [B30](B30-flow-node-runners.md)); `_persist_session` writes the new session id back to `node_lineage` after each turn. The lineage is keyed by a reserved sentinel node id `__supervisor__` ([supervisor.py:42-46](../../../src/wastech_orchestrator/core/supervisor.py#L42)) — distinct from the routing identity `supervisor`, so it can never collide with a real flow node id — and the raw session id lives **only** in `state.db`. The request `permission_profile` is **forced `read-only`** — the supervisor never writes ([supervisor.py:150](../../../src/wastech_orchestrator/core/supervisor.py#L150)). The role prompt is rendered from `config.supervisor.role_file` inside the flow dir, falling back to a minimal instruction if the role file is missing/unreadable. The request carries the `supervisor` node identity but records `evaluations` rows (`node_id=None`), never `node_runs`.

### Configuration and ceiling

Configured in `config.yaml` under `supervisor: {model, reasoning, role_file}` ([B05](B05-configuration.md)), under the same ceiling as flow nodes: `read-only` here, `reasoning` in the allowlist (loader), `role_file` path-contained.

## Invariants & guarantees

- **Advisory only** — every verdict is `advisory`; the engine never consumes a supervisor row to route ([supervisor.py:181-191](../../../src/wastech_orchestrator/core/supervisor.py#L181)).
- **Never fatal** — observation/synthesis failures are logged and swallowed; they can never fail or reroute the task ([supervisor.py:90-95](../../../src/wastech_orchestrator/core/supervisor.py#L90)).
- **Append-only audit** — `supervisor_step` / `supervisor_final` rows are immutable ([B07](B07-state-machine-and-store.md), `evaluations`).
- **Read-only** — the permission profile is forced `read-only`; the supervisor cannot edit code.
- **Durable own session** — `_own_session_id` is persisted to / hydrated from `node_lineage` under the `__supervisor__` sentinel, so a resumed task continues the supervisor's accumulated cross-step context (gated by provider match; raw session id stays in `state.db`).

## Dependencies

- **Uses:** [B17](B17-agent-router-and-fallback.md) (router, via `RouterPort`), [B07](B07-state-machine-and-store.md) (`evaluations`), [B15](B15-prompt-templates.md) (`render_role_prompt`), [B20](B20-artifact-layout.md) (`task_artifact_dir`, `summary.{md,json}`), [B05](B05-configuration.md) (`SupervisorConfig`).
- **Used by:** [B06](B06-orchestrator-pipeline.md) — the orchestrator constructs one `Supervisor` per task, wires `observe` into the engine's post-node hook, and calls `finalize` at whole-task close.

## Tests

- `tests/core/test_supervisor.py` — advisory rows are recorded; observation/synthesis failures never raise; `finalize` returns `None` so the minimal-summary fallback applies.
