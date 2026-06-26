# Per-node model/reasoning/provider override in task front matter

Status: **proposed** (2026-06-26) Date: 2026-06-26 Owner: Vladimir Makarevich

This is a bounded, practical extension to the existing `nodes:` task key: add `model`, `reasoning`, and `provider` fields to `NodeOverride` alongside the already-shipped `enabled` toggle. The goal is to let a single default flow cover multiple model/effort variants without multiplying flow files or editing global config per run.

## The problem

Today, changing model or reasoning effort for a specific run requires one of three things: creating a separate flow file (e.g., `implementation-opus.yaml`), editing `config.yaml` globally before the run, or accepting that every task in the queue uses the same model/effort profile. All three are painful. Flow proliferation is the worst offender — every combination of (provider, model, reasoning) that any task ever needs becomes a separate YAML file in `.worc/flows/`. For experiments and one-off runs this is unacceptable friction.

## Constraints

- **The core does not know the CLI syntax.** Model/reasoning strings are already passed through the `AgentRunRequest` → provider adapter path; the override must stay in that path, never in Core.
- **No secrets in logs or SQLite.** Model/reasoning strings are not secrets, but the principle holds.
- **Watch-mode compat.** Tasks can be admitted automatically; a preflight-fatal error on a bad override would silently block the queue. Invalid overrides must degrade gracefully, not abort.
- **Per-task `nodes:` key is the sanctioned exception** (`ALLOWED_TASK_KEYS` already includes it). Extending it is additive; no new key, no task schema version bump needed.
- **`validate_flow_against_config` ceiling model:** budgets/ceiling violations clamp or warn at runtime; only fail preflight when there is no safe fallback (P4 invariant).

## Alternatives considered

| Alternative | Why rejected |
| --- | --- |
| Separate flow YAML per model variant | Exactly the problem being solved. N×M files for N providers × M effort levels. |
| CLI flag `--override implementation.model=…` | Ephemeral — not stored in the task file, invisible to `watch` automation, not reproducible from git. |
| Named `variants:` section in flow YAML | Moves the complexity to the flow file, doesn't reduce file count. Adds a variant-selection mechanism the task format doesn't have. Bigger schema change for less payoff. |
| Task-level global `model:/reasoning:` (one field for all nodes) | Per-node is what the user needs — e.g., high-reasoning for `implementation`, cheap model for `review`. A global override is a degenerate subset; add it later if needed. |
| Do nothing | Queue stagnates or operators manually edit config per run; incompatible with autonomous `watch` mode. |

## Decision

Extend `NodeOverride` with three optional fields: `model: str | None`, `reasoning: str | None`, `provider: str | None`. Apply them at the flow-engine execution seam — where the task's `disabled_nodes` are already consulted — by overlaying the task's per-node values onto the resolved `FlowNode` before `AgentNodeRunner._build_request()` is called. The resulting override chain is:

```
Task node override (best-effort)
  → Flow node declaration (flow YAML)
    → Provider config default
```

**Fallback on invalid override** (provider name not in config, unrecognised reasoning value, model name rejected by the ceiling or the provider): log a structured warning into the node's artifact log, skip the invalid field, and fall back to the flow's declared value. The task is not aborted. This is consistent with the P4 "clamp, don't fail" rule for non-fatal config mismatches and with watch-mode automation requirements.

**Ceiling for `reasoning`:** clamp to the operator's configured ceiling value + warn (same as today's reasoning ceiling logic). **Ceiling for `model`:** cannot be "downgraded" by name; if the task-requested model is not the same as or below the ceiling, skip the override + warn. No attempt to map model names to a tier ordering — that is fragile.

## Open questions

1. **Exact merge seam.** `disabled_nodes` is consumed by the engine after snapshot load; `model/reasoning/provider` overrides need to reach `AgentNodeRunner._build_request()`. The cleanest slot is a thin "apply task overrides" step in `engine_driver.drive_flow()` that patches the resolved node before passing it to the runner. Confirm this is cleaner than patching the snapshot at load time (which would require the snapshot builder to receive the task).
2. **Model ceiling semantics.** Is "skip override + warn" the right call when the task requests a model not within the ceiling? An alternative is to treat ceiling violation as a user error and move the task to `failed`; this would be consistent with other fatal misconfigurations but breaks watch-mode compat.
3. **EvaluatorNode.** The feature description covers `AgentNode`; `EvaluatorNode` has the same `model`/`reasoning` fields in the flow schema. Should task-level overrides apply to evaluator nodes too? Probably yes, but confirm.
4. **Audit visibility.** Ensure the effective (post-override) model/reasoning values appear in the prompt audit and `state.db` node_lineage row, not the flow-declared defaults.

## Implementation notes

- `src/wastech_orchestrator/task/model.py` — extend `NodeOverride` dataclass; update docstring (remove "only sanctioned per-node knob"); add parser validation (unknown `reasoning` values, empty strings).
- `src/wastech_orchestrator/core/flow/engine_driver.py` — add task-override application step; read `task.node_overrides` from the inputs context; for each active node, check for a matching override and apply valid fields before the runner receives the node.
- `src/wastech_orchestrator/core/flow/validator.py` / `validate_flow_against_config` — optionally extend to emit warnings (not errors) when task overrides violate configured ceilings; the runtime fallback is the load-bearing check.
- `src/wastech_orchestrator/providers/codex.py`, `claude.py` — no change; the override chain already collapses to `request.model or config.model` at the provider level.
- Tests: `tests/task/` — extend `NodeOverride` parse tests; `tests/flow/` — add engine-driver integration test that verifies a task override reaches `_build_request` and that an invalid override falls back gracefully.
