# Backlog: Per-stage model and reasoning overrides

Status: **§3 implemented** (per-task per-stage `stages:` overrides) · **§4 still backlog** (config-level `stage_defaults`) Date: 2026-06-13 Owner: Vladimir Makarevich

This document captures the design for adding stage-level granularity to the `model` and `reasoning` task parameters. The per-task per-stage portion (§3) is **implemented**; see the [Implementation notes](#7-implementation-notes-as-built) for the deltas from the original design. The config-level per-stage defaults (§4) remain a backlog item. Nothing here overrides [00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md), [CLAUDE.md](../../CLAUDE.md), or the hard invariants in [docs/rules/](../rules/).

> **Extended by stage-skip control (2026-06-13).** The `stages:` block now also carries a per-stage `enabled: false` toggle that **skips** a stage (skippable: `planning`, `testing`, `review`, `fixing`, `summary`). `StageParams` gained an `enabled` field and the `stages:` validator accepts a per-stage union of valid sub-keys (`model`/`reasoning` for agent stages, `enabled` for skippable ones — so `testing` accepts only `enabled`). See [docs/backlog/stage_skip_control.md](../backlog/stage_skip_control.md).

---

## 1. Motivation

The orchestrator already supports two levels of model/reasoning configuration:

| Level | Where | Current support |
| --- | --- | --- |
| **Provider default** | `config.yaml` → `providers.<name>.model` / `.reasoning` | ✅ implemented |
| **Task-wide override** | task frontmatter `model:` / `reasoning:` | ✅ implemented |
| **Per-stage override** | task frontmatter `stages.<stage>.model` / `.reasoning` | ❌ not yet |

A single task moves through very different stages — `planning`, `implementation`, `review`, `fixing`, `summary` — each with a fundamentally different cognitive demand:

- **`planning`** benefits from a high-reasoning, capable model (e.g. Opus + `reasoning: high`) to produce a correct, complete plan that downstream stages depend on.
- **`implementation`** is typically the heaviest stage by token volume and benefits from a fast, cost-effective model (e.g. Sonnet + `reasoning: medium`).
- **`review`** needs depth and skepticism — a higher-capability model with elevated reasoning is worth the extra cost here to catch subtle bugs before they merge.
- **`fixing`** iterates rapidly over small corrections; a lighter model with `reasoning: low` keeps fix cycles short.
- **`summary`** is a lightweight reformatting task; the cheapest capable model is fine.

With only a task-wide `model`/`reasoning`, the operator is forced to choose between overspending (apply the heaviest model everywhere) or under-serving (use a lighter model that struggles on planning and review). Per-stage configuration removes this trade-off.

### Analogy with existing `agents` field

The task frontmatter already supports per-stage **provider** selection via `agents:`:

```yaml
agents:
  refinement: claude
  planning: claude
  implementation: claude
  review: codex
  fixing: claude
```

Per-stage model/reasoning is the natural extension of this pattern — the operator can now also choose _how_ each provider runs, not just _which_ provider runs.

---

## 2. Current code paths

- `task/model.py:23` — `ALLOWED_TASK_KEYS` lists `"model"` and `"reasoning"` as top-level keys.
- `task/model.py:49-50` — `NormalizedTask.model: str | None` and `.reasoning: str | None`.
- `task/validation_gate.py:236-237` — parser reads `frontmatter.get("model")` and `frontmatter.get("reasoning")` into `NormalizedTask`.
- `core/orchestrator.py:966-967` — `AgentRunRequest` is built with `model=p.task.model or None` and `reasoning=p.task.reasoning` — the same value for every stage.
- `providers/codex.py:189-204` and `providers/claude.py:282-289` — resolution inside the provider: `request.model or config.model` (task → provider default).

All three levels share the same two fields; adding a per-stage layer only changes the resolution logic in the orchestrator before the request is built.

---

## 3. Proposed design

### 3.1 New task frontmatter key: `stages`

```yaml
# task frontmatter
id: my-task
title: "Implement OAuth flow"

model: claude-sonnet-4-6 # fallback for stages not listed in `stages:`
reasoning: low # fallback reasoning

stages:
  planning:
    model: claude-opus-4-8
    reasoning: high
  review:
    model: claude-opus-4-8
    reasoning: high
  fixing:
    model: claude-sonnet-4-6
    reasoning: medium
  # stages not listed here inherit from the top-level model/reasoning
```

Both `model` and `reasoning` inside a `stages.<name>` block are optional. If only `reasoning` is specified, `model` falls back to the task-wide or provider default (and vice versa).

**Allowed stage keys** inside `stages:`: the canonical stage names (`refinement`, `planning`, `implementation`, `testing`, `review`, `fixing`, `summary`). Unknown keys are rejected at validation time (fail-closed, same policy as unknown top-level keys).

### 3.2 Resolution order (per field, per stage)

```
stages.<stage>.model          # per-stage task override  (most specific)
  → task.model                # task-wide override
  → config.providers.<p>.model  # provider default
  → None (model flag omitted from CLI call)
```

Same chain for `reasoning`. The two fields are resolved independently — mixing them across levels is intentional and useful (e.g. use a specific model from the task default, but override reasoning only for the review stage).

### 3.3 Data model changes

**New dataclass in `task/model.py`**:

```python
@dataclass(frozen=True)
class StageParams:
    """Per-stage model / reasoning override (spec §5 extension)."""
    model: str | None = None
    reasoning: str | None = None
```

**`NormalizedTask` extension**:

```python
@dataclass(frozen=True)
class NormalizedTask:
    ...
    model: str | None = None
    reasoning: str | None = None
    stage_params: dict[Stage, StageParams] = field(default_factory=dict)  # new
```

**`ALLOWED_TASK_KEYS` extension**:

```python
ALLOWED_TASK_KEYS: frozenset[str] = frozenset(
    {"id", "title", "refined", "decompose", "agents", "contacts",
     "model", "reasoning", "stages"}   # "stages" added
)
```

### 3.4 Validation gate (`task/validation_gate.py`)

Extend the frontmatter validator to handle `stages`:

- `stages` must be a mapping (dict), or null/absent.
- Each key must be a known canonical stage name; unknown keys → `INVALID_FIELD_TYPE` rejection.
- Each value must be a mapping with optional `model` (string or null) and `reasoning` (one of `_REASONING_LEVELS` or null); unknown sub-keys → rejection.
- An empty `stages: {}` or `stages: null` is valid and treated as no per-stage overrides.
- Populate `NormalizedTask.stage_params` as `dict[Stage, StageParams]`.

### 3.5 Orchestrator change (`core/orchestrator.py`)

Replace the current flat lookup with a helper that resolves the effective values for the current stage:

```python
def _effective_model(task: NormalizedTask, stage: Stage) -> str | None:
    sp = task.stage_params.get(stage)
    if sp and sp.model:
        return sp.model
    return task.model or None

def _effective_reasoning(task: NormalizedTask, stage: Stage) -> str | None:
    sp = task.stage_params.get(stage)
    if sp and sp.reasoning:
        return sp.reasoning
    return task.reasoning
```

Use in `AgentRunRequest` construction (`orchestrator.py:966`):

```python
model=_effective_model(p.task, stage),
reasoning=_effective_reasoning(p.task, stage),
```

The provider adapters (`codex.py:189`, `claude.py:282`) continue to do `request.model or config.model` — no changes needed there.

### 3.6 Example task file update

Update `docs/examples/task-001.example.md` to document the new `stages:` key:

```yaml
model: null # optional: override model for all stages
reasoning: null # optional: low | medium | high | xhigh | max (all stages)
stages: # optional: per-stage model/reasoning overrides
  planning:
    model: null # e.g. "claude-opus-4-8"
    reasoning: null
  review:
    model: null
    reasoning: null
  # other stages: refinement, implementation, testing, fixing, summary
```

---

## 4. Config-level per-stage defaults (future extension)

A complementary enhancement (not in scope here) would add per-stage model/reasoning to `config.yaml` under each provider, so that operator-wide defaults can differ by stage without requiring every task to spell them out:

```yaml
providers:
  claude:
    model: claude-sonnet-4-6 # base default
    reasoning: low
    stage_defaults: # future
      planning:
        reasoning: high
      review:
        model: claude-opus-4-8
        reasoning: high
```

The resolution order would then become:

```
stages.<stage>.model (task)
  → task.model
  → config.providers.<p>.stage_defaults.<stage>.model (future)
  → config.providers.<p>.model
```

This is a separate, larger change that touches the config schema, loader, and provider adapters. Track it as a follow-up once per-task per-stage overrides are working.

---

## 5. Implementation checklist (§3 — done)

- [x] Add `StageParams` dataclass to `task/model.py`.
- [x] Add `stage_params: dict[Stage, StageParams]` field to `NormalizedTask`.
- [x] Add `"stages"` to `ALLOWED_TASK_KEYS` in `task/model.py`.
- [x] Extend `task/validation_gate.py` to parse and validate `stages:` block; populate `stage_params`.
- [x] Resolution helpers — **as `NormalizedTask.model_for(stage)` / `reasoning_for(stage)` methods** (not free functions in the orchestrator), used in `AgentRunRequest` construction (`orchestrator.py`, the per-stage `_run_stage`).
- [x] Update `docs/examples/task-001.example.md` with the new `stages:` key and inline comments.
- [x] Unit tests:
  - Per-stage override takes precedence over task-wide value.
  - Task-wide value used when stage not listed in `stages:`.
  - Provider config used when neither task-wide nor per-stage value set.
  - Unknown stage key in `stages:` → task rejected at validation gate.
  - Unknown sub-key inside a stage block → task rejected.
  - `model` and `reasoning` resolved independently (e.g. only `reasoning` overridden for a stage uses the task-wide `model`).
  - Plus: `stages.testing`/`stages.publishing`, non-mapping `stages`, non-mapping stage value, and an end-to-end orchestrator test that the resolved values reach the provider request.
- [x] Update `CHANGELOG.md` `[Unreleased]` entry.
- [x] Update `docs/task-authoring.md` and the §5 front-matter example in `00_orchestrator_final_plan.md`.

---

## 6. Related items

- `task/model.py:23` `ALLOWED_TASK_KEYS` — add `"stages"`.
- `task/model.py:49-50` `NormalizedTask.model` / `.reasoning` — source of the task-wide default.
- `task/validation_gate.py:236-237` — where per-stage parsing is added.
- `core/orchestrator.py:966-967` — `AgentRunRequest` construction; insert the new resolver.
- `providers/codex.py:189-204`, `providers/claude.py:282-289` — no changes needed here.
- `docs/examples/task-001.example.md` — example file to update.
- [[product_backlog]] — "Per-task reasoning and complexity levels" row: this item builds on that row and refines it to stage-level granularity.

---

## 7. Implementation notes (as-built)

§3 shipped largely as designed, with these deliberate refinements (the design above is kept for context; where it differs, this section is authoritative):

- **Reject reason.** Invalid `stages:` entries use a dedicated `ValidationReason.INVALID_STAGE_OVERRIDE` (not `INVALID_FIELD_TYPE` as §3.4 suggested), mirroring `agents`'s `INVALID_ROUTE_OVERRIDE` so a rejected task's `validation_report.json` is self-explanatory. (The top-level `model`/`reasoning` type checks still emit `INVALID_FIELD_TYPE`, unchanged.)
- **Allowed stage keys = `ROUTABLE_STAGES`.** Keys are validated against `config.schema.ROUTABLE_STAGES` (`refinement, planning, implementation, review, fixing, summary`), which excludes **both** `testing` and `publishing` (neither runs an agent). The original "exclude publishing" framing would have wrongly accepted `stages.testing`.
- **Resolution lives on `NormalizedTask`.** `model_for(stage)` / `reasoning_for(stage)` methods (frozen-dataclass methods, unit-testable in isolation) replace the proposed free `_effective_model` / `_effective_reasoning` functions in the orchestrator.
- **DRY validation.** A shared `_validate_model_reasoning(model, reasoning, *, reason, prefix)` helper in `validation_gate.py` validates both the top-level pair and each stage block.
- **Edge cases (fail-closed).** `stages: null|{}` and `stages.<stage>: null|{}` are accepted and mean "inherit"; non-mapping `stages`, non-mapping stage values, unknown sub-keys, unknown/non-routable stages, and invalid `reasoning` levels are all rejected.
- **Non-goal.** No model-name-vs-provider cross-validation (a `model` string is not checked against the stage's routed provider) — consistent with the existing task-wide `model`.

As-built code references (the line numbers in §2/§6 above are from the original draft and are now stale): `task/model.py` (`StageParams`, `stage_params`, `model_for`/`reasoning_for`, `ALLOWED_TASK_KEYS`); `task/validation_gate.py` (`INVALID_STAGE_OVERRIDE`, `_build_stage_params`, `_validate_model_reasoning`); `core/orchestrator.py` (`_run_stage` → `AgentRunRequest`).
