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

The engine's `PostNodeHook` ([B28](B28-flow-engine.md)) calls `observe` after each executed (non-skipped) node, keyed by the step's `node_run_id`. The role prompt tells the supervisor that a task may be decomposed into sequential subtasks, so it frames each step as progress on its subtask and treats whole-task closure as reached only after the last subtask — keeping per-step notes from prematurely declaring the run "closed." `observe` ([supervisor.py:80](../../../src/wastech_orchestrator/core/supervisor.py#L80)) runs one read-only LLM turn on the supervisor's own session and records an immutable `supervisor_step` row in `evaluations` ([B07](B07-state-machine-and-store.md)), namespaced by `source_node_run_id` so a resumed run does not duplicate observations. Each turn's `AgentRunRequest` carries the **observed step's** `node_run_id` (finalize uses the `0` sentinel — it runs once per task), so successive supervisor turns write to distinct `stages/supervisor/run-NNNNNN/` artifact dirs instead of all colliding on `run-000000` (the artifact writer never overwrites). It is **best-effort**: a failed observation is logged and swallowed — it is advisory and must never fail or reroute the task ([supervisor.py:159-164](../../../src/wastech_orchestrator/core/supervisor.py#L159)) — and is recorded with `observation_failed=true` in the row payload, distinct from an empty advisory note ("nothing to add"), so a silent advisory layer is diagnosable.

### Whole-task finalize

`finalize` ([supervisor.py](../../../src/wastech_orchestrator/core/supervisor.py)) synthesizes a plain-language whole-task summary, writes the working `summary.{md,json}` under the task artifact dir, and records `supervisor_final`. The committed `summary.md` becomes the PR body. When the synthesis LLM call cannot run, `finalize` returns `None` and writes no `summary.md`, so the orchestrator's deterministic **minimal-summary fallback** applies ([B08](B08-ledger-and-failure-reports.md)) — the summary is _always_ written, by one path or the other. `summary.json` (local-only metadata) is always written. In the packaged `implementation` flow, `finalize` runs before the publish node so the summary rides the task's own audit commit.

The finalize turn's **structure** is data-driven and rides a **single LLM call** regardless of what is enabled (AC-W1): it is free-text when neither memory nor follow-ups are on (today's behavior — AC-S4); otherwise a structured `{summary, ...}` turn. When memory is enabled it also emits `memory_delta`; when the flow set `supervisor.emit_follow_ups` it also emits the **evidence-gated `follow_ups`** array (task 1 of the prompt-and-supervisor contract). Each `follow_ups` record is minimal and grounded — `title`, `rationale`, `paths`, `evidence`, `severity`, `action_hint` — and `parse_follow_ups` **drops any record without evidence**, so an ungrounded "refactor idea" cannot reach the summary. The records are written into `summary.json` and surfaced as a **"Technical debt / follow-ups"** section in `summary.md`. The `follow_ups` schema is **hardcoded in code** — a flow reshapes the supervisor's wording via its prompt files but can never change the parsed contract. `emit_follow_ups` is a **per-flow, code-oriented** opt-in (default off): the packaged `implementation` flow sets it; the research / prose flows do not. A missing/malformed structured output leaves today's `summary.md` fallback untouched (best-effort).

### The supervisor's own session

`_run` ([supervisor.py:136](../../../src/wastech_orchestrator/core/supervisor.py#L136)) runs one read-only turn through the router, continuing the supervisor's own `resume_own_lineage` session. The session is **durable**: `_resume_session` resumes the in-memory `_own_session_id`, or — on a fresh process after a restart — the persisted `node_lineage` row (gated by provider match, exactly like the `resume_own_lineage` evaluator in [B30](B30-flow-node-runners.md)); `_persist_session` writes the new session id back to `node_lineage` after each turn. The lineage is keyed by a reserved sentinel node id `__supervisor__` ([supervisor.py:42-46](../../../src/wastech_orchestrator/core/supervisor.py#L42)) — distinct from the routing identity `supervisor`, so it can never collide with a real flow node id — and the raw session id lives **only** in `state.db`. The request `permission_profile` is **forced `read-only`** — the supervisor never writes. The request carries the `supervisor` node identity but records `evaluations` rows (`node_id=None`), never `node_runs`.

### Flow-local prompts and their fallback chains

The supervisor's prompt **wording** is flow-local (prompt-and-supervisor contract, Cluster B): a flow declares a `supervisor:` block with `role_file` (observe lens) and `finalize_role_file` (finalize lens), both validated flow-dir-contained (fatal on traversal). Each has its own fallback chain, differing because only the observe lens has a global counterpart today:

- **Observe** (`_base_prompt`, used by per-step observation + the skill-map proposal): flow `role_file` → global `config.supervisor.role_file` → built-in string (three steps).
- **Finalize** (`_finalize_base`): flow `finalize_role_file` → built-in string (two steps; there is deliberately no global finalize prompt — YAGNI).

Each step is best-effort: a missing/bad/traversing candidate falls through to the next, so a bad prompt file never breaks the run. The packaged `implementation` flow owns `implementation/supervisor.md` (observe) and `implementation/summary.md` (finalize, reviving the previously-dead `roles/summary.md`); flows with no `supervisor:` block (e.g. `deep_research`, `security_audit`) use the global config observe lens + the built-in finalize. Only the wording moves into files — the structured schemas stay in code.

### Configuration and ceiling

Configured in `config.yaml` under `supervisor: {model, reasoning, role_file}` ([B05](B05-configuration.md)) — the global observe lens + executor — and, per flow, the `supervisor:` block above. Under the same ceiling as flow nodes: `read-only` here, `reasoning` in the allowlist (loader), every `role_file` path-contained.

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
