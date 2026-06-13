# Stage skip control (per-task and global)

Status: **implemented (2026-06-13)**
Date: 2026-06-13
Owner: Vladimir Makarevich

This document captures the design for allowing operators to skip individual pipeline stages,
either globally in `config.yaml` or per-task in task frontmatter. It is **now implemented** — see
[docs/task-authoring.md](../task-authoring.md#stages) and [docs/operations.md](../operations.md) for
the operator-facing docs, and the [CHANGELOG](../../CHANGELOG.md) `[Unreleased]` entry. The sections
below are the original design; the **Implemented shape** note that follows records where it diverged.
Nothing here overrides
[00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md), [CLAUDE.md](../../CLAUDE.md), or the
hard invariants in [docs/rules/](../rules/).

## Implemented shape (how it shipped vs. this design)

Three decisions diverged from the original sketch below:

1. **No new top-level `skip:` key.** Skip is expressed on the existing `stages:` block as
   `stages.<stage>.enabled: false` (consolidating with per-stage model/reasoning — §5 here), so the
   `stages:` validator now accepts a per-stage union of valid sub-keys: `model`/`reasoning` for
   agent-routed stages, `enabled` for skippable stages.
2. **`review` skip is gated** behind a new `agents.allow_review_skip` flag (fail-closed, required for
   a review skip from either the global list or a task) rather than allowed with only a warning.
3. **Audit** lives in `stage_runs.skipped`/`skip_reason` (the first real `state.db` migration,
   v1→v2, via `_migrate`), and per-stage skip intent now round-trips through `task.normalized.json`
   for crash recovery. The skipped set is appended to the PR body as `## Pipeline stages skipped`.

Everything else (the union resolution with no per-task opt-out, the skippable set, the per-stage
behaviours, the review+auto_merge double-warning) shipped as designed. A future per-task opt-out
(`force_stages`) is tracked in [follow_ups.md](follow_ups.md).

---

## 1. Motivation

The orchestrator's pipeline is currently fixed:

```
refinement → planning → [implementation → testing → review → fixing (loop)] → summary → publishing
```

`refinement` is already conditionally skippable via `refined: true` in the task frontmatter.
No other stage can be skipped.

This creates friction for common, well-understood scenarios:

| Scenario | What the operator wants to skip |
| --- | --- |
| Trivial cosmetic change (rename a label, fix a typo) | `planning`, `review` |
| Repository has no automated test suite | `testing` (check runner always fails; budget is wasted on fixing non-existent tests) |
| Task already carries a hand-written plan | `planning` |
| High-trust internal tooling, no review gate needed | `review` |
| Fast iteration; summary isn't needed | `summary` |
| Task is already refined/decomposed externally | `refinement` (already: `refined: true`) |

Without skip control the operator must either burn fixing budget on stages that will never
succeed, or accept the full pipeline overhead even when it adds no value.

---

## 2. Current code paths

- Pipeline driver: `core/orchestrator.py` `_drive` → `_refinement` → `_planning` →
  `_run_units_and_finish` → `_run_unit` (implement → test → review → fix loop) → `_summary` →
  `_publish`.
- `_refinement` (`orchestrator.py:475`) is the only stage with an existing skip path: it checks
  `p.task.refined or completeness is Completeness.COMPLETE`.
- `_planning` (`orchestrator.py:~530`) always runs; it also owns decomposition and writes
  `plan.md` — the artifact that all downstream stages reference as context.
- `_run_unit` (`orchestrator.py:~560`) is a while-loop over IMPLEMENTING → TESTING → REVIEWING
  → FIXING; none of these sub-stages have skip paths.
- `task/model.py:23` `ALLOWED_TASK_KEYS` — `"skip"` is not a recognized field today.

---

## 3. Skippability analysis

Not all stages are safe or meaningful to skip. The table below defines what is skippable and
what must never be skipped via this mechanism.

| Stage | Skippable? | Skip consequence | Notes |
| --- | --- | --- | --- |
| `refinement` | ✅ already | task description used as-is | Use `refined: true` (existing). |
| `planning` | ✅ proposed | stub `plan.md` written from task title + description; no decomposition (single-unit run) | Implementation has less structured context; suitable for trivial tasks only. |
| `testing` | ✅ proposed | check runner bypassed; goes directly from IMPLEMENTING → REVIEWING | Useful when the repo has no meaningful test suite. Must be clearly logged. |
| `review` | ✅ proposed | review agent bypassed; goes directly to commit | **High risk** — no agent quality gate. Require explicit opt-in; log prominently. |
| `fixing` | ✅ proposed | any test or review failure goes directly to MANUAL_REVIEW (0 fix iterations) | Effectively `max_fix_attempts: 0` — keeps the stage active but skips recovery. |
| `summary` | ✅ proposed | minimal stub summary written (task title + "no summary generated"); PR body reflects this | Low risk. |
| `implementation` | ❌ never | this is the core work; no skip | — |
| `publishing` | ❌ never | this is the output; use dry-run mode instead (separate backlog item) | — |

---

## 4. Proposed design

### 4.1 Task frontmatter: `skip` key

```yaml
id: task-042
title: "Rename 'Submit' button to 'Save'"
refined: true          # existing: skip refinement
skip:
  - planning           # no planning stage; stub plan used
  - review             # no review agent; go straight to commit
```

- `skip` is a list of canonical stage names (strings).
- Allowed values: `planning`, `testing`, `review`, `fixing`, `summary`.
- `refinement` is intentionally excluded — use the existing `refined: true` flag.
- `implementation` and `publishing` are excluded — they cannot be skipped.
- An empty list (`skip: []`) or absent key is equivalent — no stages skipped.
- Unknown stage names in `skip:` → task rejected at the validation gate (fail-closed).

### 4.2 Global config: `agents.skip_stages`

```yaml
# config.yaml
agents:
  skip_stages:           # stages to skip for every task (unless overridden per-task)
    - testing            # e.g. repo has no test suite
```

This applies to all tasks processed by this orchestrator instance. A task that explicitly
lists a stage in its own `skip:` also skips it; the effective skip set is the **union** of
the global list and the task list:

```
effective_skip = set(config.agents.skip_stages) | set(task.skip)
```

There is no per-task opt-out of a global skip (if the global config skips `testing`, a task
cannot re-enable it). This simplifies the resolution logic and avoids surprising behaviour
where a task silently runs a stage the operator disabled system-wide.

> If individual opt-out is needed in the future, extend with a `force_stages:` field — but
> scope that as a separate item.

### 4.3 Effective skip resolution

```python
def effective_skip(config: OrchestratorConfig, task: NormalizedTask) -> frozenset[Stage]:
    global_skip = {Stage(s) for s in config.agents.skip_stages}
    task_skip   = {Stage(s) for s in task.skip}
    return frozenset(global_skip | task_skip)
```

Call once per task in `_drive`; pass the frozenset down to each stage method.

### 4.4 Per-stage skip behaviour in the pipeline

#### `planning` skipped

Write a stub `plan.md` from the task title and description. Set `p.decomposition` to the
no-decomposition default (single-unit run). Log `"planning skipped — stub plan written"`.
Transition directly to IMPLEMENTING.

```python
def _planning(self, p: _Pipeline, skip: frozenset[Stage]) -> None:
    if Stage.PLANNING in skip:
        stub = f"# Plan (stub — planning stage skipped)\n\n{p.task.title}\n\n{p.task.description}"
        p.plan_path = self._write_artifact(p, "plan.md", stub)
        p.decomposition = _no_decomposition()
        self._log(p.task.id).warning("planning skipped — stub plan written")
        self._transition(p, Status.IMPLEMENTING)
        return
    # ... existing logic
```

#### `testing` skipped

In `_run_unit`, when status is IMPLEMENTING, after writing the diff, bypass the TESTING
block entirely and transition straight to REVIEWING.

```python
if p.status is Status.IMPLEMENTING:
    ...
    if Stage.TESTING in skip:
        self._log(p.task.id).warning("testing skipped — going directly to review")
        self._transition(p, Status.REVIEWING)
    else:
        self._transition(p, Status.TESTING)
```

#### `review` skipped

After TESTING passes (or is itself skipped), bypass the REVIEWING block and call
`_on_review_passed` directly.

```python
if p.status is Status.REVIEWING:
    if Stage.REVIEW in skip:
        self._log(p.task.id).warning("review skipped — committing without agent review")
        self._loops.on_review_pass(p.counters)
        self._save_counters(p)
        return self._on_review_passed(p, unit, is_last=is_last)
    # ... existing review logic
```

#### `fixing` skipped

When a test or review failure would normally enter the fix loop, check if `FIXING` is in the
skip set and route directly to `MANUAL_ACTION_REQUIRED` instead:

```python
def _enter_fixing(self, p: _Pipeline, loop: FixLoop, skip: frozenset[Stage]) -> PipelineResult | None:
    if Stage.FIXING in skip:
        self._log(p.task.id).warning("fixing skipped — going to MANUAL_REVIEW on first failure")
        return self._require_manual(p, f"fixing disabled; {loop.value} failed")
    # ... existing fix-entry logic
```

#### `summary` skipped

In `_summary`, when the stage is in the skip set, write a minimal stub:

```python
def _summary(self, p: _Pipeline, skip: frozenset[Stage]) -> None:
    if Stage.SUMMARY in skip:
        stub = f"# Summary\n\nTask `{p.task.id}`: {p.task.title}\n\n*(summary stage skipped)*"
        self._write_summary_stub(p, stub)
        self._log(p.task.id).info("summary skipped — stub written")
        return
    # ... existing logic
```

### 4.5 Audit trail

Every skipped stage must:
- Emit a `WARNING`-level structured log line: `"<stage> skipped"` with reason (task flag /
  global config / both).
- Record the skip in `state.db` — extend the `stage_runs` table with a `skipped: bool` column
  and a `skip_reason: str | None`; a skipped stage gets a row with `skipped=True` and no
  `run_id` or provider data.
- Include the effective skip set in the task's `summary.md` / PR body so reviewers know which
  stages ran.

---

## 5. Interaction with other backlog items

### `per_stage_model_reasoning.md` — `stages:` block convergence

The [per-stage model/reasoning backlog item](per_stage_model_reasoning.md) proposes a `stages:`
block in the task frontmatter:

```yaml
stages:
  review:
    model: claude-opus-4-8
    reasoning: high
```

`skip:` and `stages:` serve different concerns and can coexist as separate top-level keys.
In a future consolidation they could be merged:

```yaml
stages:
  planning:
    enabled: false     # skip
  review:
    model: claude-opus-4-8
    reasoning: high
    enabled: true      # explicit (default)
```

Track that consolidation as a follow-up; do not block either item on the other.

### `auto_merge_bypass.md`

If `review` is skipped and `auto_merge` is also enabled, the task reaches publishing with zero
human or agent review. The orchestrator should emit an explicit `WARNING` in this case —
`"review skipped AND auto_merge enabled — task will merge without any review gate"` — and
record it in the audit log.

### `ux_improvements.md` — `stop`/`restart`

No interaction. Stage skip state is fully persisted in `state.db`; a restart recovers correctly
because the skip set is re-derived from config + task frontmatter on every startup.

---

## 6. Implementation checklist (when scheduled)

- [ ] Add `skip: list[str]` to `NormalizedTask` (`task/model.py`).
- [ ] Add `"skip"` to `ALLOWED_TASK_KEYS` (`task/model.py:23`).
- [ ] Validate `skip` entries at the validation gate: unknown stage names → task rejected
      (`task/validation_gate.py`).
- [ ] Add `agents.skip_stages: list[str]` to `OrchestratorConfig` / config schema
      (`config/schema.py`, `config/loader.py`); validate entries the same way.
- [ ] Add `effective_skip(config, task) -> frozenset[Stage]` helper.
- [ ] Thread `skip` frozenset into `_drive`, `_planning`, `_run_unit`, `_enter_fixing`,
      `_summary` in `core/orchestrator.py`.
- [ ] Implement per-stage skip branches with stub artifacts and WARNING logs (see §4.4).
- [ ] Add `skipped` / `skip_reason` columns to `stage_runs` in `state_store.py`; bump
      `state.db` schema version.
- [ ] Include effective skip set in `summary.md` / PR body.
- [ ] Emit double-WARNING when `review` skipped + `auto_merge` enabled (see §5).
- [ ] Unit tests:
  - Each skippable stage skips correctly when flagged.
  - Task skip and global config skip both take effect independently.
  - Union of both skip sets is applied.
  - `implementation` and `publishing` cannot be added to `skip:` (validation rejects them).
  - Stub plan/summary artifacts are written with expected content.
  - `fixing` skip → first test/review failure routes to MANUAL_REVIEW.
- [ ] Update `docs/examples/task-001.example.md` with `skip:` key and inline comments.
- [ ] Update `docs/operations.md` with global `agents.skip_stages` config docs.
- [ ] Update `CHANGELOG.md` `[Unreleased]`.

---

## 7. Related items

- `core/orchestrator.py` `_drive`, `_refinement`, `_planning`, `_run_unit`, `_summary` —
  all stage-entry points that need skip checks.
- `task/model.py:23` `ALLOWED_TASK_KEYS` — add `"skip"`.
- `task/validation_gate.py` — validation for `skip:` list entries.
- `config/schema.py`, `config/loader.py` — add `agents.skip_stages`.
- `state_store.py` — `stage_runs` schema extension.
- `docs/examples/task-001.example.md` — update with `skip:` key.
- [[per_stage_model_reasoning]] — `stages:` block; future consolidation path.
- [[auto_merge_bypass]] — double-WARNING when review is skipped and auto_merge is enabled.
