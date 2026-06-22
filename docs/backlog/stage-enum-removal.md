# Remove the `Stage` enum — re-found per-task skip on flow node ids

Status: **accepted** Date: 2026-06-22 Owner: Vladimir Makarevich

Detailed design for the [follow_ups.md](follow_ups.md) item "Flow-engine: delete the `Stage` enum (per-task skip → flow node ids)". This supersedes sub-point (b) of the older "Stage-enum-removal minor follow-ups" row (relocating the enum) — the enum is deleted outright, not moved.

## Decisions (locked)

1. **No skippability guard.** Any node present in the resolved flow may be disabled per-task. Which nodes are safe to disable is the operator's responsibility (they author the flow and run the tasks). There is no `skippable` field and no `SKIPPABLE_STAGES` allowlist. The only hard check is "does this node id exist in the task's flow".
2. **`agents.allow_review_skip` is removed entirely** — config field, loader handling, the validation-gate `REVIEW_SKIP_NOT_ALLOWED` rejection, and the `review`-specific auto-merge warning. There is no `review`-special-case anywhere in the core.
3. **Task front-matter syntax:** `nodes: <node-id>: { enabled: false }` (the `stages:` block is renamed to `nodes:`; keys are flow node ids; the only sub-key stays `enabled`).

## Why

Today the `Stage` enum (`providers/base.py`) is no longer the provider-run identity — that is the flow `node_id`. It survives only as the closed vocabulary of the per-task stage-skip feature, and that coupling produces two real defects against any flow other than the packaged `implementation` one:

- **False reject.** A custom flow with a node `code_review` cannot be skipped per-task: `code_review` is not a `Stage`, so `INVALID_STAGE_OVERRIDE` rejects the task even though the node exists.
- **Silent no-op.** A task may set `review.enabled: false` against a flow that has no `config.review_enabled` guard (or no `review` node). Validation passes (review is in the enum) but nothing in the graph consults the skip, so the node runs anyway with no error.

The binding "task says X=false" ⇒ "node X is skipped" currently rests on two string coincidences: the name must be a `Stage` member, **and** the flow author must hand-author `when: { fact: config.<same-name>_enabled }` on the node. There is no check that the node exists in the flow the task actually runs. The correct vocabulary for a node-based engine is the node ids of the resolved flow.

## How it works today (the chain being torn out)

1. Task `stages.review.enabled: false` → validated against `Stage` + `SKIPPABLE_STAGES` in the gate, **before** the flow is resolved ([validation_gate.py:284-338](../../src/wastech_orchestrator/task/validation_gate.py#L284-L338)).
2. → `NormalizedTask.stage_params: dict[Stage, StageParams]` → `effective_skip(task)` → `_Pipeline.skip: frozenset[Stage]` ([orchestrator.py:216](../../src/wastech_orchestrator/core/orchestrator.py#L216), [orchestrator.py:276](../../src/wastech_orchestrator/core/orchestrator.py#L276)).
3. Flow node carries `when: { fact: config.review_enabled }` ([implementation.yaml:60](../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml#L60)).
4. Engine `_should_skip` evaluates `when` via the fact resolver; `config.review_enabled` → `Stage("review") not in p.skip` ([engine.py:306-310](../../src/wastech_orchestrator/core/flow/engine.py#L306-L310), [orchestrator.py:1131-1136](../../src/wastech_orchestrator/core/orchestrator.py#L1131-L1136)).

Key ordering fact: the gate runs at [orchestrator.py:343](../../src/wastech_orchestrator/core/orchestrator.py#L343); the flow is resolved later at [orchestrator.py:870](../../src/wastech_orchestrator/core/orchestrator.py#L870)/895. The gate cannot see the flow, so node-existence cannot be checked there.

## Target design

Two-tier validation (the gate cannot see the flow; flow resolution can):

- **Gate (shape only).** The `nodes:` block must be a mapping; each value is a mapping whose only key is `enabled` (a bool). No vocabulary check. Bad shape → reject (`INVALID_NODE_OVERRIDE`).
- **Flow resolution (existence).** After the flow snapshot resolves, every disabled node id must be in `snapshot.nodes_by_id`; otherwise raise `PipelineFailed` → terminal `failed` (the task moves to `failed/`). This is the "controlled error" the operator asked for, and it runs before any side effect (branch prep), matching the existing `_resolve_flow` fail-closed contract.

Skip mechanism in the engine: the disabled-node-id set is handed to `FlowEngine`; `_should_skip(node)` returns true when `node.id` is in that set (in addition to the existing `when` predicate, which keeps serving `derived.*` / `config.external_research`). A skipped node yields its pass-through outcome and the engine takes the forward edge — exactly today's `when`-false path. The `config.*_enabled` fact family is deleted; the `when` guards on the four packaged nodes are deleted (skip is now driven directly by node id, not a fact).

The disabled set is re-derived from front-matter on every run/resume (as `p.skip` is today — not persisted), so resume needs no new state.

## Change list

### A. Flow engine + packaged flow

- `core/flow/engine.py` — `FlowEngine` gains a `disabled_nodes: frozenset[str]` (constructor arg, default empty). `_should_skip(node)` → `node.id in self._disabled_nodes or (when is not None and self._facts(when.fact) != when.equals)`. `_skip_reason` distinguishes the two causes (`disabled by task: nodes.<id>.enabled=false` vs the existing `when` message).
- `core/orchestrator.py` `_engine_facts` — delete the `config.*_enabled` branch ([orchestrator.py:1131-1136](../../src/wastech_orchestrator/core/orchestrator.py#L1131-L1136)). Keep `derived.needs_refinement` and `config.external_research`. Pass `p.skip` (now `frozenset[str]`) into the engine where it is constructed (`engine_driver`/`_engine_run`).
- `core/flow/packaged/implementation.yaml` — remove `when: { fact: config.*_enabled }` from `planning`, `testing`, `review`, `fixing` (lines 43/53/60/67). `refinement`'s `when: derived.needs_refinement` stays.
- `security_audit.yaml` / `deep_research.yaml` — **no change** (no `config.*_enabled` guards; `deep_research` keeps `derived.needs_refinement` + `config.external_research`).
- `core/flow/engine_driver.py` — thread the disabled set into the engine construction.

### B. Task model + parser

- `task/model.py` — `StageParams` → rename `NodeOverride` (field `enabled: bool | None` unchanged). `stage_params: dict[Stage, StageParams]` → `node_overrides: dict[str, NodeOverride]`. `disabled_stages()` → `disabled_nodes() -> frozenset[str]`. Drop the `Stage` import.
- `task/parser.py` — front-matter read: `Stage(stage): StageParams(...)` → `str(node_id): NodeOverride(...)` (no enum coercion). `write_normalized` / `_stage_params_json`: emit `"nodes"` keyed by node id. `load_normalized`: read back `"nodes"` as str keys. Drop the `Stage` import. (Greenfield: no need to read the old `"stages"` key.)

### C. Validation gate

- `task/validation_gate.py` — delete `_STAGE_BY_KEY` ([validation_gate.py:97](../../src/wastech_orchestrator/task/validation_gate.py#L97)) and the `Stage` import. `_build_stage_params` → `_build_node_overrides`: validate the `nodes:` block shape only (mapping; values mappings; only `enabled`, bool). Remove the `SKIPPABLE_STAGES` membership check, the `Stage.REVIEW` special-case, and the `allow_review_skip` check. `ValidationReason.INVALID_STAGE_OVERRIDE` → `INVALID_NODE_OVERRIDE`; delete `REVIEW_SKIP_NOT_ALLOWED`.

### D. Flow-resolution existence check (new)

- `core/orchestrator.py` `_resolve_flow` (or the first call site that has both the snapshot and `p.task`) — after resolving the snapshot, for each id in `p.task.disabled_nodes()` not in `snapshot.nodes_by_id`, raise `PipelineFailed(f"task disables unknown node {id!r} (flow has: {...})")`. Runs on fresh run and resume (resume re-validates), before side effects.

### E. Config schema + loader

- `config/schema.py` — delete `SKIPPABLE_STAGES` ([schema.py:69-76](../../src/wastech_orchestrator/config/schema.py#L69-L76)) and `AgentsConfig.allow_review_skip` ([schema.py:148-157](../../src/wastech_orchestrator/config/schema.py#L148-L157)). Drop the `Stage` import. Bump `CONFIG_SCHEMA_VERSION` 12 → 13.
- `config/loader.py` — remove `allow_review_skip` from the `_check_keys` allowed set and add it to `tolerated` so a stale config still loads fail-open and `upgrade-config` strips it; delete the `_bool(...)` read and the constructor arg ([loader.py:408-422](../../src/wastech_orchestrator/config/loader.py#L408-L422)).

### F. Orchestrator skip plumbing + reporting

- `effective_skip(task) -> frozenset[str]` (returns `task.disabled_nodes()`); `_Pipeline.skip: frozenset[str]` ([orchestrator.py:216](../../src/wastech_orchestrator/core/orchestrator.py#L216), [orchestrator.py:276](../../src/wastech_orchestrator/core/orchestrator.py#L276)).
- Delete the `review`-specific auto-merge warning branch `if pr_url and Stage.REVIEW in p.skip and ...` ([orchestrator.py:1257-1262](../../src/wastech_orchestrator/core/orchestrator.py#L1257-L1262)). The generic auto-merge audit/behaviour is unchanged.
- `_skip_section_md` ([orchestrator.py:1487-1492](../../src/wastech_orchestrator/core/orchestrator.py#L1487-L1492)) — `s.value` → `s` (now str); heading "## Pipeline stages skipped" → "## Pipeline nodes skipped" (and the function/var names). Drop the `Stage` import from `orchestrator.py`.

### G. Delete the enum

- `providers/base.py` — delete `class Stage(StrEnum)` and its docstring ([providers/base.py:17-32](../../src/wastech_orchestrator/providers/base.py#L17-L32)). `ProviderId`, `RunStatus`, `ErrorClass` stay. After A–F there are no remaining importers.

## Test impact

- `tests/task/test_validation_gate.py` — drop `Stage` import; `stage_params` → `node_overrides`; rewrite the `INVALID_STAGE_OVERRIDE` cases as `INVALID_NODE_OVERRIDE` (shape only); delete the `allow_review_skip` / `REVIEW_SKIP_NOT_ALLOWED` tests and the `_allow_review_skip()` helper.
- `tests/task/test_parser.py`, `tests/task/test_model.py` — `Stage`/`StageParams` → str ids / `NodeOverride`; `disabled_stages` → `disabled_nodes`; round-trip uses the `nodes:` key.
- `tests/core/test_orchestrator.py` — the `FakeProvider` fixtures key `outputs`/`infra_fail` by `Stage`; re-key by node id string (or the fixture's own enum-free key). Drop `allow_review_skip` config kwargs.
- `tests/core/test_flow_snapshot.py` — `test_when_config_fact` uses `config.planning_enabled`; retarget to `config.external_research` (a fact that still exists) or drop, since `config.*_enabled` is gone. The namespace tests stay (`config.` namespace is still valid).
- New tests: (1) a task disabling a real node id skips that node; (2) a task naming a node id absent from the flow → terminal `failed` (in `failed/`); (3) a custom flow with a non-`implementation` node id (e.g. `code_review`) can be disabled — the case that is impossible today.

## Docs impact

- `docs/task-authoring.md` — replace the `stages:` section and the skippable-stages list with the `nodes:` block (keys = flow node ids); remove the `allow_review_skip` prerequisite and the DANGER-review note (skip safety is now the operator's flow-authoring responsibility); state the failure mode (unknown node id → terminal `failed`).
- `docs/operations.md` — remove the `allow_review_skip` operator knob and the skippable-stages list; describe per-task node disable + the operator-owns-it stance.
- `src/wastech_orchestrator/templates/config.example.yaml` — delete the `allow_review_skip` line ([config.example.yaml:29](../../src/wastech_orchestrator/templates/config.example.yaml#L29)).
- `src/wastech_orchestrator/worc/decision-guide.md` — rewrite the "Skipping review" guidance (no `allow_review_skip`; node-id disable).
- `CLAUDE.md` — the "Canonical names → Stages: refinement, planning, …" line now describes the default flow's node ids, not an enum; reword to avoid implying a `Stage` type.
- Functional map / likec4 — fold into the already-pending "Full re-sync of functional-map `file:line` refs" pass.

## Risks / out of scope

- **Routing soundness when an arbitrary node is disabled.** Per Decision 1 this is the operator's responsibility. A skipped node reuses the existing pass-through-outcome → forward-edge path, so any node with a normal forward edge routes fine. A node whose skip-outcome matches no declared edge would raise `EngineInternalError` today. Optional hardening (not required for v1): at flow resolution, after the existence check, verify each disabled node's skip-outcome resolves to an edge and fail closed with a clear message — turning a potential engine crash into a controlled `failed`. This is **not** a skippability policy (it never forbids disabling a given node); it only makes a stranded-graph case a clean error. Decide at implementation time.
- **Config version.** Bumps to v13; an old config carrying `allow_review_skip` loads fail-open (tolerated) and is stripped by `upgrade-config`.
- **Fix-loop behaviour is untouched.** This task only changes how a node-skip is _decided_ (node id instead of the `config.*_enabled` fact), not what the graph does after a skip. Disabling `fixing` still spins the `test_fix`/`review_fix` loop to its cap before terminal `MANUAL` (the skipped `fixing` returns to `testing` via its forward edge). Making that an immediate `MANUAL` is the separate "`fixing`-skipped → immediate `MANUAL` as a conditional flow edge" item in [follow_ups.md](follow_ups.md); do not conflate it with the enum removal.
- This closes sub-point (b) of the older "Stage-enum-removal minor follow-ups" row. Sub-points (a) `hitl.node_interaction_path` unification and (c) the supervisor `node_run_id=0` `create_attempt_dir` collision are independent and remain in that row.

## Acceptance

- `ruff`, `mypy`, `pytest` green; no remaining importers of `Stage` (grep clean).
- A task with `nodes: { review: { enabled: false } }` against the packaged `implementation` flow runs without the review node and without any `allow_review_skip` config.
- A task naming a node id absent from its resolved flow ends `failed` (in `failed/`) with a controlled message, before any branch/PR side effect.
- A custom flow node id (not one of the legacy stage names) can be disabled per-task.
