# Flows: the Executable Node Graphs

> Reconstructed from code (`src/wastech_orchestrator/core/flow/`, `packaged/flows/*.yaml`). The code is the only source of truth; this was rebuilt from the implementation, not from prose. Significant claims carry a `file:line` reference.

A **flow** is the pipeline expressed as data: a YAML document defining a graph of typed nodes and the edges (with fix loops) between them, plus flow-wide ceilings. A task's `task_type` selects its flow; the [flow engine](../blocks/B28-flow-engine.md) drives the graph; the [node runners](../blocks/B30-flow-node-runners.md) execute the nodes; the [orchestrator](../blocks/B06-orchestrator-pipeline.md) wraps it all. This replaced the former fixed eight-stage (S01–S08) pipeline — there is no longer a hardcoded stage loop, and the per-stage docs that described it are superseded by these per-flow docs.

## How a flow is selected and validated

`FlowRegistry.resolve(task_type)` ([registry.py:66](../../../src/wastech_orchestrator/core/flow/registry.py#L66)) maps the task's `task_type` (default `implementation`) to a validated `FlowSnapshot`, preferring an operator flow at `<repo>/.worc/flows/<task_type>.yaml` over the packaged built-in. Every flow passes the fatal three-layer validator (graph integrity, security ceiling, config-aware) before any task runs — see [B29](../blocks/B29-flow-definition-and-validation.md). At install/preflight, `validate_all` checks every resolvable flow.

## Node kinds

| Kind | What it does | Outcomes it emits |
| --- | --- | --- |
| `agent` | runs an author/editor through the router (optional embedded HITL, dangerous-diff guard, `editing_lineage` session, `output_artifact` slot) | `done` (unconditional) |
| `evaluator` | read-only verdict over a produced artifact; blocking gates, non-blocking self-caps | `accept` / `rework` |
| `checks` | a quality gate: `command_profile` (the resolved check commands), `citation`, or `dependency_scan` | `pass` / `fail` |
| `hitl` | a bare durable human gate | `route:approve`/`route:deny` (approval) or `done` (question) |
| `publish` | the orchestrator-owned git publish for the flow's `PublishingPolicy` | `done` |

Node fields, the `Edge` shape, and the flow-wide ceilings (`permission_ceiling`, `output_policy`, `publishing`, `network_policy`) are defined in [B29](../blocks/B29-flow-definition-and-validation.md); the runners in [B30](../blocks/B30-flow-node-runners.md).

## Routing, loops, and budgets

The engine routes on a node's outcome to the matching edge ([B28](../blocks/B28-flow-engine.md)). A `rework`/`fail` edge must declare a `loop` (a named consecutive-cycle counter) or an inline `budget: N`. Every such edge also charges the single global fix counter. Each cap is `min(flow_budget, config_cap)`: named loops clamp to `agents.max_fix_cycles`, the global counter (`budgets.global_fix_iterations`) clamps to `agents.max_total_fix_iterations`. Exhausting any limit ends the run at `manual_action_required` with a failure report. A node's `when: {fact: ...}` predicate (`derived.*` / `config.*`) deterministically skips it — this is how a per-task `stages.<stage>.enabled: false` toggle and the refinement-skip work.

## The supervisor layer (above every flow)

The whole-task **summary** and per-step advisory oversight are **not** a node and **not** a stage — they are the constant [supervisor layer](../blocks/B31-supervisor.md) that lives for the whole task under any flow shape, observes each completed step read-only, and writes `summary.md` at task close. This is why no packaged flow has a `summary` node.

## The packaged flows

- **[implementation](implementation.md)** (default) — the coding pipeline → Pull Request.
- **[deep-research](deep-research.md)** — research synthesis → documentation PR, with a citation gate and external research.
- **[security-audit](security-audit.md)** — advisory audit → a private control-workspace report (no git).

A decomposed task (only the `implementation` flow ships a `decomposition` block) runs its `sub_flow` region once per subtask — see [B11](../blocks/B11-task-decomposition.md).
