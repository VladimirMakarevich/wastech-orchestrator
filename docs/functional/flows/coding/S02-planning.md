# S02 — planning stage

## Purpose

The agent builds a plan (`plan.md`) and optionally recommends splitting the task into subtasks. Skills are also resolved here. The stage can be skipped — in that case a stub plan is written and the task proceeds as a single unit.

## Responsibility

- On skip — write a stub plan, **disable** decomposition, transition to implementation ([orchestrator.py:1068-1088](../../../../src/wastech_orchestrator/core/orchestrator.py#L1068)).
- On run — execute the agent, collect `plan.md` (+ skills section), apply the decomposition acceptance rule, and write artifacts/subtask rows ([orchestrator.py:1089-1137](../../../../src/wastech_orchestrator/core/orchestrator.py#L1089)).

## Step boundaries

### Within this step's responsibility

- Running/skipping the stage; assembling `plan.md`; invoking the decomposition decision and skill resolution; writing subtasks; transitioning to implementation.

### Outside this step's responsibility

- **Decomposition acceptance rule and subtask artifacts** — [B11](../../blocks/B11-task-decomposition.md).
- **Skill inventory/selection and deduplication** — [B13](../../blocks/B13-skill-selection.md).
- **Typed output validation and HITL** — [B12](../../blocks/B12-hitl-and-typed-output.md); **`gate_on` resolution** (`decomposition.enabled` + per-task) — [B06 `_decomposition_gate_on`](../../blocks/B06-orchestrator-pipeline.md).
- **Agent execution** — [B17](../../blocks/B17-agent-router-and-fallback.md)/[B18](../../blocks/B18-agent-providers.md); **prompt** — [B15](../../blocks/B15-prompt-templates.md).

## Entry points

- `_planning(p)` ([orchestrator.py:1068](../../../../src/wastech_orchestrator/core/orchestrator.py#L1068)) — called from `_drive`.
- `_resolve_and_render_skills(p, proposed)` ([orchestrator.py:1139](../../../../src/wastech_orchestrator/core/orchestrator.py#L1139)) — skill resolution ([B13](../../blocks/B13-skill-selection.md)) and the section in `plan.md`.

## Input data and state

Typed agent output (`decompose`, `subtasks[]`, `skills`); `gate_on`; `max_subtasks`. Status: `refining`/`preparing` → `planning` → `implementing`. Artifacts — `plan.md`, `subtasks/index.json`, `NN-<slug>.md`; `subtasks` rows in [B07](../../blocks/B07-state-machine-and-store.md).

## Main scenario

1. **Skip** (`planning` in `skip`): stub plan from task, `DecompositionDecision(accepted=False, reason="planning_skipped")`, `record_skip`, transition to implementation (decomposition requires structured planning output, so it is not possible without it).
2. **Run**: `_run_typed_stage` → `plan.md` = content + skills section ([B13](../../blocks/B13-skill-selection.md)); `decide_decomposition` ([B11](../../blocks/B11-task-decomposition.md)) with `gate_on`/`max_subtasks`; on acceptance — `write_subtask_artifacts` + `insert_subtasks` ([B07](../../blocks/B07-state-machine-and-store.md)); transition to implementation.

```mermaid
flowchart TB
    start(["entry: planning"]) --> skip{"planning skipped?"}
    skip -->|yes| stub["stub plan; decomposition OFF; record_skip"]
    skip -->|no| run["agent → plan.md + skills section (B13)"]
    run --> dec["decide_decomposition (B11) with gate_on/max_subtasks"]
    dec -->|accepted| subs["subtask artifacts + insert_subtasks (B07)"]
    dec -->|single unit| impl
    subs --> impl["→ S03 implementation"]
    stub --> impl
```

## Checks and constraints

- `planning` is included in `SKIPPABLE_STAGES` ([schema.py:55-63](../../../../src/wastech_orchestrator/config/schema.py#L55)); when skipped, decomposition is forcibly disabled.
- The agent **proposes** a split — the core accepts it according to the deterministic rule §5.1 ([B11](../../blocks/B11-task-decomposition.md)); the agent cannot relax `max_subtasks`/routes.
- May request human input (HITL) — refinement/planning ([B12](../../blocks/B12-hitl-and-typed-output.md)); a dangerous diff is not fixed by the plan, but its approval may cover the diff of subsequent stages ([S03](./S03-implementation.md)).

## Result / transition

Transition to [S03 implementation](./S03-implementation.md). Artifacts: `plan.md` (+ skills); on decomposition — `subtasks/index.json` and specs; `decomposition_*`/`subtask_count`/`active_subtask` in [B07](../../blocks/B07-state-machine-and-store.md).

## Side effects

- Writing `plan.md`, subtask artifacts; `subtasks` rows and task fields in [B07](../../blocks/B07-state-machine-and-store.md).
- Via delegates: agent execution ([B18](../../blocks/B18-agent-providers.md)), HITL ([B26](../../blocks/B26-notifications-telegram.md)), reading the skill inventory ([B13](../../blocks/B13-skill-selection.md)).

## Errors and edge cases

- HITL failure → `manual_action_required` (fail-closed).
- Malformed decomposition output structure → single unit with a reason code (not an exception, [B11](../../blocks/B11-task-decomposition.md)).

## Relationships

### Uses

- [B11](../../blocks/B11-task-decomposition.md), [B13](../../blocks/B13-skill-selection.md), [B12](../../blocks/B12-hitl-and-typed-output.md), [B15](../../blocks/B15-prompt-templates.md), [B17](../../blocks/B17-agent-router-and-fallback.md)/[B18](../../blocks/B18-agent-providers.md), [B07](../../blocks/B07-state-machine-and-store.md).

### Used by

- [S03 implementation](./S03-implementation.md) — next stage (per unit); [B06](../../blocks/B06-orchestrator-pipeline.md) — driver.

## Position in the flow

Second stage. Determines how many work units there will be (one or subtasks) and what reference material (skills) subsequent stages will see. See [flow overview](./index.md).

## Code confirmation

- [orchestrator.py:1068-1137](../../../../src/wastech_orchestrator/core/orchestrator.py#L1068) — `_planning` (skip, decomposition, subtasks).
- [orchestrator.py:1139-1194](../../../../src/wastech_orchestrator/core/orchestrator.py#L1139) — `_resolve_and_render_skills` / skills section.
- Tests: [tests/core/test_decomposition.py](../../../../tests/core/test_decomposition.py), [tests/core/test_skills.py](../../../../tests/core/test_skills.py), [tests/core/test_orchestrator.py](../../../../tests/core/test_orchestrator.py).
