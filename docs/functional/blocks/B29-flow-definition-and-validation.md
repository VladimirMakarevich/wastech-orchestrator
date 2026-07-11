# B29 — Flow Definition, Registry and Validation

> Reconstructed from code (`src/wastech_orchestrator/core/flow/schema.py`, `contracts.py`, `snapshot.py`, `registry.py`, `validator.py`, `packaged/*.yaml`) and tests (`tests/core/flow/`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `core/flow/schema.py`, `core/flow/contracts.py`, `core/flow/snapshot.py`, `core/flow/registry.py`, `core/flow/validator.py`, `packaged/flows/`

## Responsibility

This block defines what a flow **is** (the typed document), how a YAML file becomes an immutable snapshot, how a `task_type` resolves to a flow, and the fatal validation gate every flow must pass before any task runs. The pipeline is "the flow expressed as data": a flow YAML names nodes (the work), edges (the routing + fix loops), budgets, an optional decomposition block, and the flow-wide ceilings (`permission_ceiling`, `output_policy`, `publishing`, `network_policy`). The engine ([B28](B28-flow-engine.md)) consumes the snapshot; the node runners ([B30](B30-flow-node-runners.md)) execute the nodes.

## Public surface

- **Schema types** (`schema.py`): `FlowNode` union ([schema.py:116](../../../src/wastech_orchestrator/core/flow/schema.py#L116)) of `AgentNode` ([schema.py:47](../../../src/wastech_orchestrator/core/flow/schema.py#L47)), `EvaluatorNode` ([schema.py:73](../../../src/wastech_orchestrator/core/flow/schema.py#L73)), `ChecksNode` ([schema.py:90](../../../src/wastech_orchestrator/core/flow/schema.py#L90)), `HitlNode` ([schema.py:99](../../../src/wastech_orchestrator/core/flow/schema.py#L99)), `PublishNode` ([schema.py:108](../../../src/wastech_orchestrator/core/flow/schema.py#L108)); `Edge` ([schema.py:119](../../../src/wastech_orchestrator/core/flow/schema.py#L119)); `DecompositionConfig` ([schema.py:130](../../../src/wastech_orchestrator/core/flow/schema.py#L130)); `FlowDoc` ([schema.py:155](../../../src/wastech_orchestrator/core/flow/schema.py#L155)); `WhenPredicate` ([schema.py:25](../../../src/wastech_orchestrator/core/flow/schema.py#L25)); `HitlSettings` ([schema.py:33](../../../src/wastech_orchestrator/core/flow/schema.py#L33)).
- **Contracts** (`contracts.py`): `SessionScope` ([contracts.py:48](../../../src/wastech_orchestrator/core/flow/contracts.py#L48)), `PermissionProfile` ([contracts.py:61](../../../src/wastech_orchestrator/core/flow/contracts.py#L61)), `OutputPolicy` ([contracts.py:68](../../../src/wastech_orchestrator/core/flow/contracts.py#L68)), `PublishingPolicy` ([contracts.py:76](../../../src/wastech_orchestrator/core/flow/contracts.py#L76)), `NetworkPolicy` ([contracts.py:86](../../../src/wastech_orchestrator/core/flow/contracts.py#L86)), `RunKind`/`EvaluatorRole` ([contracts.py:26-45](../../../src/wastech_orchestrator/core/flow/contracts.py#L26)), `ExecutionUnit` ([contracts.py:97](../../../src/wastech_orchestrator/core/flow/contracts.py#L97)), `fingerprint` ([contracts.py:114](../../../src/wastech_orchestrator/core/flow/contracts.py#L114)).
- **Loader** (`snapshot.py`): `load_flow(path)` ([snapshot.py:134](../../../src/wastech_orchestrator/core/flow/snapshot.py#L134)) → `FlowSnapshot` ([snapshot.py:118](../../../src/wastech_orchestrator/core/flow/snapshot.py#L118)); `FlowLoadError` ([snapshot.py:47](../../../src/wastech_orchestrator/core/flow/snapshot.py#L47)).
- **Registry** (`registry.py`): `FlowRegistry` ([registry.py:49](../../../src/wastech_orchestrator/core/flow/registry.py#L49)), `resolve(task_type)` ([registry.py:66](../../../src/wastech_orchestrator/core/flow/registry.py#L66)), `validate_all()` ([registry.py:95](../../../src/wastech_orchestrator/core/flow/registry.py#L95)), `DEFAULT_TASK_TYPE` ([registry.py:40](../../../src/wastech_orchestrator/core/flow/registry.py#L40)), `FlowResolutionError` ([registry.py:45](../../../src/wastech_orchestrator/core/flow/registry.py#L45)).
- **Validator** (`validator.py`): `validate_flow(snapshot)` ([validator.py:80](../../../src/wastech_orchestrator/core/flow/validator.py#L80)), `validate_flow_against_config(snapshot, config)` ([validator.py:90](../../../src/wastech_orchestrator/core/flow/validator.py#L90)), `FlowValidationError` ([validator.py:68](../../../src/wastech_orchestrator/core/flow/validator.py#L68)), `Violation` ([validator.py:60](../../../src/wastech_orchestrator/core/flow/validator.py#L60)).

## Behavior

### Node kinds

A flow has five node kinds, each a frozen dataclass discriminated by `kind` ([schema.py:116](../../../src/wastech_orchestrator/core/flow/schema.py#L116)):

| Kind | Key fields | Runner ([B30](B30-flow-node-runners.md)) |
| --- | --- | --- |
| `agent` | `role_file`, `session_scope`, `lineage_affinity`, `permission_profile`, `provider`, `output_artifact`, `best_effort`, `hitl`, `when` | runs an author/editor through the router |
| `evaluator` | `role`, `role_file`, `blocking`, `max_rework_per_stage`, `permission_profile` (const read-only) | read-only verdict → `accept`/`rework` |
| `checks` | `checker` (`command_profile` / `citation` / `dependency_scan`), `when` | quality gate → `pass`/`fail` |
| `hitl` | `signal` (`question` / `approval`), `timeout_s` | bare durable human gate |
| `publish` | `policy` (a `PublishingPolicy`) | git publish (orchestrator-owned) |

An `Edge` ([schema.py:119](../../../src/wastech_orchestrator/core/flow/schema.py#L119)) carries `from_node`, `to`, an optional `outcome`, and an optional `budget` or `loop` (the YAML key `from` maps to `from_node`). A `WhenPredicate` ([schema.py:25](../../../src/wastech_orchestrator/core/flow/schema.py#L25)) marks a node skippable when `fact != equals`.

### Provider-neutral contracts

`contracts.py` defines the vocabulary shared by the schema, validator, engine, and store — defined once so slices do not invent parallel enums. `SessionScope` ([contracts.py:48](../../../src/wastech_orchestrator/core/flow/contracts.py#L48)) is `fresh_disposable` / `editing_lineage` (stage authors) / `resume_own_lineage` (a multi-round evaluator's own session). `PermissionProfile` ([contracts.py:61](../../../src/wastech_orchestrator/core/flow/contracts.py#L61)) is `read-only` / `workspace-write`. `OutputPolicy` ([contracts.py:68](../../../src/wastech_orchestrator/core/flow/contracts.py#L68)), `PublishingPolicy` ([contracts.py:76](../../../src/wastech_orchestrator/core/flow/contracts.py#L76)), and `NetworkPolicy` ([contracts.py:86](../../../src/wastech_orchestrator/core/flow/contracts.py#L86)) are the flow-wide ceilings (a flow grants network only by declaring `network_policy`; absence = no network). Enum string values deliberately avoid YAML 1.1 boolean/null tokens. `fingerprint` ([contracts.py:114](../../../src/wastech_orchestrator/core/flow/contracts.py#L114)) is the key-order-independent SHA-256 behind `flow_fingerprint`.

### Loading: fail-closed allowlists

`load_flow` ([snapshot.py:134](../../../src/wastech_orchestrator/core/flow/snapshot.py#L134)) reads the YAML, requires a top-level `flow:` key, computes the `flow_fingerprint` over the raw `flow` dict, and parses into a `FlowDoc`. Every mapping is checked against an explicit field allowlist — an unknown key is a **fatal** `FlowLoadError`, never silently ignored ([snapshot.py:51-98](../../../src/wastech_orchestrator/core/flow/snapshot.py#L51), `_reject_unknown` at [snapshot.py:111](../../../src/wastech_orchestrator/core/flow/snapshot.py#L111)). This is the structural `additionalProperties: false` gate that keeps operator YAML a closed allowlist. Additional load-time closed sets:

- `checker` ∈ `{command_profile, citation, dependency_scan}` ([snapshot.py:134](../../../src/wastech_orchestrator/core/flow/snapshot.py#L134)) — a flow may not invent a checker. A `checks` node is exactly `{id, kind, checker, when}` (`_CHECKS_FIELDS`, [snapshot.py:111](../../../src/wastech_orchestrator/core/flow/snapshot.py#L111)), so a stale `discovery:` key (removed with the checks-monorepo change) is now a fatal `FlowLoadError` — the flow never supplies check commands or discovery settings.
- `output_artifact` ∈ `{enriched_spec, plan, summary}` ([snapshot.py:93](../../../src/wastech_orchestrator/core/flow/snapshot.py#L93)) — the slot vocabulary is core-fixed.
- `when.fact` must be namespaced `derived.*` / `config.*` ([snapshot.py:98](../../../src/wastech_orchestrator/core/flow/snapshot.py#L98)) — a bare/typo'd fact is rejected at load.

The resulting `FlowSnapshot` ([snapshot.py:118](../../../src/wastech_orchestrator/core/flow/snapshot.py#L118)) holds the `FlowDoc`, the `nodes_by_id` and `adjacency` lookup tables (both `MappingProxyType`, immutable), and the `flow_fingerprint`.

### Registry: task_type → snapshot

`FlowRegistry.resolve(task_type)` ([registry.py:90](../../../src/wastech_orchestrator/core/flow/registry.py#L90)) maps a task's `task_type` to a validated snapshot from a **single source** — the operator's `<repo>/.worc/flows/<task_type>.yaml` ([registry.py:159](../../../src/wastech_orchestrator/core/flow/registry.py#L159)). The built-ins (`implementation`, `deep_research`, `security_audit`) ship under `packaged/flows/`, but that tree is **delivery-only**: `worc install` copies it into `.worc/flows/` ([B03](B03-installer-and-scaffolding.md)) and `resolve` never reads it — there is no packaged fallback in `_find`, so `.worc/flows/` is the whole truth for what runs.

`task_type=None` defaults to `implementation` ([registry.py:42](../../../src/wastech_orchestrator/core/flow/registry.py#L42)); a `task_type` with no file in `.worc/flows/` raises `FlowResolutionError` before any side effect, with a message naming the missing `<task_type>.yaml`, listing the operator's own flow names, and pointing at `worc install` ([registry.py:108](../../../src/wastech_orchestrator/core/flow/registry.py#L108)) — an explicit "not found", not a silent bundled-copy load. A YAML whose `flow.task_type` field does not match the lookup key raises the same error ([registry.py:115](../../../src/wastech_orchestrator/core/flow/registry.py#L115)). Every resolved snapshot passes `validate_flow` ([registry.py:119](../../../src/wastech_orchestrator/core/flow/registry.py#L119)), plus `validate_flow_against_config` when the registry was constructed with a config ([registry.py:121](../../../src/wastech_orchestrator/core/flow/registry.py#L121)). This dispatch-time `resolve` is the fatal safety net: a broken, unsafe, or missing flow that a task actually requests fails that task (→ failed/quarantine), not on a global gate. For on-demand diagnostics, `operator_flow_names()` enumerates only the operator's `.worc/flows/*.yaml` (packaged built-ins excluded) and `check_flows(names)` resolves + prompt-lints each without raising, returning a `FlowCheck(name, error, warnings)` per flow — the seam behind `worc validate-flow` ([B01](B01-cli-and-operator-commands.md)). Preflight no longer validates flows.

### Validation: three fail-closed layers

All violations are collected and reported together so an operator fixes everything in one pass ([validator.py:68-77](../../../src/wastech_orchestrator/core/flow/validator.py#L68)).

1. **Graph integrity** (`_check_graph`, [validator.py:122](../../../src/wastech_orchestrator/core/flow/validator.py#L122)) — edges resolve; outcome ⊆ the allowed set per node kind (evaluator → `accept`/`rework`, checks → `pass`/`fail`, others unconditional, `route:*` always allowed); every `rework`/`fail` edge declares a `budget` or `loop`; named loops declared in `budgets`; exactly one entry node; all nodes reachable from it; at least one terminal and every node can reach one; `lineage_affinity` targets an `editing_lineage` agent with no conflicting provider; decomposition references resolve **and the region is connected** — once `proposed_by`/`sub_flow` resolve, some edge from `proposed_by` must enter the region and some forward (non-rework) edge must exit it, else the partitioner (`partition_decomposition`) would crash with `StopIteration` at run time (the runtime path also carries a belt-and-suspenders `next(..., None)` → `EngineInternalError`).
2. **Security ceiling** (`_check_ceiling`, [validator.py:267](../../../src/wastech_orchestrator/core/flow/validator.py#L267)) — an evaluator is always `read-only` and never `editing_lineage`; every agent `permission_profile` ≤ `permission_ceiling`; `extra_args` pass the forbidden-flag check ([B25](B25-security-policy.md)); `role_file` contains no path traversal (`_check_path`, [validator.py:307](../../../src/wastech_orchestrator/core/flow/validator.py#L307)).
3. **Config-aware** (`validate_flow_against_config` → `_check_config_consistency`, [validator.py:318](../../../src/wastech_orchestrator/core/flow/validator.py#L318)) — every explicitly-pinned node `provider` ∈ `agents.allowed`; node `reasoning` is valid for the provider that will run the node; Codex is not routed to a `workspace-write` agent node that resolves `network_access: true`; `permission_ceiling` ≤ the capability of at least one configured allowed provider.

The config-aware layer validates **only the cases with no safe runtime fallback** ([validator.py:24-32](../../../src/wastech_orchestrator/core/flow/validator.py#L24)). Two related properties are deliberately **not** fatal because the orchestrator degrades them gracefully: flow `budgets` above the config caps (the engine clamps to `min(flow, cap)` at runtime) and `publishing` vs git config (`git.create_pull_request: false` runs any flow in local-commit mode). This is the "only fatal when no safe runtime fallback" principle.

### The packaged flows

- **`implementation`** (default, `task_type: implementation`) — the coding pipeline. See [flows/implementation.md](../flows/implementation.md) for the node-by-node graph. `permission_ceiling: workspace-write`, `output_policy: code_change`, `publishing: pull_request`; loops `test_fix`/`review_fix` and global `global_fix_iterations`; a decomposition block over `[implementation, testing, review, fixing]`.
- **`deep_research`** — research synthesis; `output_policy: repository_document`, a `citation` checks node, `network_policy: research`. See [flows/deep-research.md](../flows/deep-research.md).
- **`security_audit`** — advisory audit; a `dependency_scan` checks node, `network_policy: advisories`, `private_control_workspace_report` output. See [flows/security-audit.md](../flows/security-audit.md).

## Invariants & guarantees

- **Fail-closed parsing** — unknown YAML keys, checker kinds, output-artifact slots, and un-namespaced facts are fatal at load ([snapshot.py:111-116](../../../src/wastech_orchestrator/core/flow/snapshot.py#L111)).
- **Validate before side effects** — `resolve` validates before returning, so a flow never reaches the engine unvalidated; `check_flows` (behind `worc validate-flow`) runs the same validator on demand over operator flows.
- **Security only narrows** — the config-aware layer can only reject a flow that exceeds the configured ceiling; it never relaxes it ([validator.py:95-98](../../../src/wastech_orchestrator/core/flow/validator.py#L95)).
- **Deterministic identity** — `flow_fingerprint` is order-independent, so resume trusts the stored fingerprint rather than re-resolving from live config ([B10](B10-recovery-and-resume.md)).

## Dependencies

- **Uses:** [B25](B25-security-policy.md) (`find_forbidden_args`, `is_same_or_stricter`), [B05](B05-configuration.md) (`OrchestratorConfig` for the config-aware layer), [B18](B18-agent-providers.md) (`ProviderId`).
- **Used by:** [B28](B28-flow-engine.md) (consumes the snapshot), [B06](B06-orchestrator-pipeline.md) (resolves the flow per task), [B01](B01-cli-and-operator-commands.md) (`validate-flow` calls `check_flows`/`operator_flow_names`).

## Tests

- `tests/core/flow/` — loader allowlist/fail-closed cases, registry resolution + operator override, the three validation layers, packaged-flow validity, and `operator_flow_names`/`check_flows` (the `validate-flow` seam).
