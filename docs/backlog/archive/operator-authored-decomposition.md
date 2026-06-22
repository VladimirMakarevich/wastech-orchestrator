# Operator-authored decomposition (`subtasks:` references) — one root task, operator-supplied split, one PR

Status: **done** (implemented 2026-06-22) Date: 2026-06-22 Owner: Vladimir Makarevich

> **As-built corrections to this design (the doc predated two refactors):** (1) the per-task disable knob is `nodes.<id>.enabled`, not `stages.<>.enabled`/`SKIPPABLE_STAGES`/`_build_stage_params` (the `Stage` enum was removed). (2) There is no "stub-plan step" — the engine's post-node hook fires only for _executed_ nodes, so a disabled `planning` node never reaches the agent-path materialization seam. The operator decision is therefore validated + materialized at the orchestrator's **pre-branch preflight** (a second pass in `run_task` after the IO-free gate, reusing `_reject` → `tasks/rejected/` + report), which works whether `planning` runs or is disabled. (3) The new `ValidationReason` members (`invalid_subtasks`, `invalid_subtask_path`, `subtask_file_missing`, `subtask_malformed`, `subtask_count_out_of_range`, `subtask_depends_forward`, `flow_cannot_decompose`) live in the gate enum but the IO-bearing ones are applied by that preflight pass, not inside the pure `ValidationGate`. Canonical behavior now lives in [task-authoring.md](../task-authoring.md#subtasks-operator-authored-decomposition) and the code.

Detail file for the operator-authored-decomposition backlog item. The operator writes one **root** task that carries the shared context plus an ordered list of references to per-subtask files, and the orchestrator runs them exactly like a planning-proposed decomposition: sequentially, on one task branch, into **one PR**. This is the inverse-source twin of the existing decomposition — the split is authored by the operator instead of proposed by the planning agent.

## Problem / motivation

Today an operator who already knows how to break a large, coherent change into ordered units cannot express that split. The two existing knobs are both the wrong shape:

- **One large task + planning-proposed decomposition** ([decomposition.py](../../src/wastech_orchestrator/core/decomposition.py), flow `decomposition:` block — [implementation.yaml:87-90](../../src/wastech_orchestrator/core/flow/packaged/implementation.yaml#L87-L90)). The split is decided by the planning **agent**; the operator only controls the gate (`agents.decomposition.enabled`) and `max_subtasks`, never the exact units. Good when you trust the agent to carve the work; useless when the operator has already carved it.
- **Several independent task files**. Each task is strictly one branch + one PR (`agent/<task-id>-<slug>`). N related task files become N PRs — the opposite of "ship this set together."

The gap: an operator-authored set of related units that ships as **one** reviewable PR. The execution machinery for exactly that already exists (it is what an accepted decomposition runs); only the **input path** is missing.

## Design: swap the source of the `DecompositionDecision`, reuse everything downstream

The whole decomposition pipeline keys off one value: the `DecompositionDecision` the orchestrator materializes at the `decomposition.proposed_by` node ([orchestrator.py:1177](../../src/wastech_orchestrator/core/orchestrator.py#L1177)). Today that value comes from reading the planning agent's `decompose`/`subtasks` contract ([postprocess.read_decomposition](../../src/wastech_orchestrator/core/flow/postprocess.py)). The entire design is: **when the root task carries an operator manifest, build the `DecompositionDecision` from it instead — and leave every downstream step untouched.**

Reused as-is (no change): `SubtaskSpec` / `DecompositionDecision` ([decomposition.py:46-64](../../src/wastech_orchestrator/core/decomposition.py#L46-L64)); the linear-dependency + `1..n` + `max_subtasks` validation ([decide_decomposition](../../src/wastech_orchestrator/core/decomposition.py#L106)); `write_subtask_artifacts` (writes `subtasks/index.json` + immutable `NN-<slug>.md` specs); region partition + per-subtask execution ([engine_driver.partition_decomposition](../../src/wastech_orchestrator/core/flow/engine_driver.py)); the `node_runs`/`subtasks` state rows and `reconcile_decomposed` recovery ([recovery.py](../../src/wastech_orchestrator/core/recovery.py)); single-PR publishing in the post region.

### Why this does **not** patch the graph (invariant check)

The clean-task invariant (CLAUDE.md; [task/model.py:21-27](../../src/wastech_orchestrator/task/model.py#L21-L27)) says a task carries identity/dispatch only, plus the two sanctioned exceptions (`stages.<>.enabled`, `auto_merge`). Operator-authored subtasks are a **third bounded exception of the same kind**, and the argument is precise: the operator supplies the _content_ of the split — the same `subtasks` data the planning agent supplies today — **not** the graph shape. The flow's `decomposition:` block (which node proposes, which nodes form the `sub_flow` region, the `shared_budget`) is still required and unchanged. The default `implementation` flow already has it, so operator decomposition works out of the box; a custom flow without a `decomposition:` block is rejected fail-closed at preflight with a clear message (the task names a flow that cannot host a split). The Core's deterministic gate validates the operator's units exactly as it validates the agent's, so the operator cannot weaken `max_subtasks` or smuggle a non-linear dependency.

## Why not `task_type: decomposed` (the better alternative to the original idea)

The first instinct is a `task_type: decomposed`. Don't: `task_type` is the **flow selector** ([task/model.py:26-27](../../src/wastech_orchestrator/task/model.py#L26-L27)) — it names _which_ pipeline runs (`implementation` / `deep_research` / `security_audit` / an operator flow). Decomposition is orthogonal: it is a capability of any flow that declares a `decomposition:` block, not a flow of its own. A `decomposed` task type would (a) collide with the real dispatch axis, (b) force a duplicate flow, and (c) wrongly imply decomposition is a pipeline rather than a region inside one.

Better: **the presence of a `subtasks:` list in the root task is itself the marker.** No new type, no duplicate flow, composes with whatever flow `task_type` already selects (as long as that flow can host a split).

## Root task shape

```yaml
---
id: epic-checkout
title: "Rework checkout into a multi-step flow"
subtasks: # NEW: ordered references; presence ⇒ operator-authored decomposition
  - subtasks/01-cart-model.md
  - subtasks/02-payment-step.md
  - subtasks/03-confirmation.md
---
## Description

Shared context for the whole change: the end-state, the cross-cutting constraints, the modules in play. This is the once-per-task framing every subtask inherits — the same role the planning plan plays for an agent split.
```

- `subtasks` is added to `ALLOWED_TASK_KEYS`. It is a list of **repository-relative paths** to subtask files. Order is **list position** (1..n) — the operator orders the list; no per-file `order` field to keep in sync.
- The root body is the shared context. The root is otherwise a normal task (its `id`/`title` name the branch and the single PR).
- A `subtasks` list of length < 2 is rejected (a split needs ≥ 2 units, mirroring the agent gate); length > `max_subtasks` is rejected.

## Subtask file shape (a spec, not a task)

Each referenced file is a **reduced manifest**, deliberately _not_ a standalone task (no `id`, so it could never be mistaken for one):

```yaml
---
title: "Add the cart line-item model"
depends_on: [] # optional; slugs of earlier subtasks (default: none)
---

## Acceptance criteria

- [ ] Concrete, testable unit-level criteria.
```

- `slug` is derived from `title` via the existing `slugify` (operator may override with an explicit `slug:`); it names the immutable `NN-<slug>.md` spec.
- `depends_on` lists **slugs of earlier subtasks** (operator-friendly), mapped at load to the existing int-order `depends_on` so [decide_decomposition](../../src/wastech_orchestrator/core/decomposition.py#L136-L143)'s linear/forward/cycle check runs unchanged. A forward or self reference is rejected.
- The file body (Description + Acceptance criteria) is materialized verbatim into `logs/<root-id>/subtasks/NN-<slug>.md` and injected as `{subtask_spec_path}` into the edit nodes — the same channel the agent split uses.

### Where subtask files live (no scanner change needed)

`select_pending` is **non-recursive** — it enumerates only top-level `.md`/`.json` in `tasks/pending/` ([cli.py:532-536](../../src/wastech_orchestrator/cli.py#L532-L536)). So subtask files placed in a **subfolder** (recommended: `tasks/pending/subtasks/…` relative to the root, or any non-top-level path) are naturally invisible to the scheduler — they never run as standalone tasks. No change to `select_pending`. (Belt-and-suspenders: the validation gate also rejects any path a root references that sits at the scanned top level, so a stray top-level subtask file is caught, not silently double-run.)

### Path safety

Reuse the project's fail-closed path discipline (`.agents/rules/security.md`; the `id` regex precedent — reject, never sanitize): each `subtasks` entry must be repo-relative, contain no `..` / absolute / traversal shape, and resolve under the tasks directory. A path that escapes is a terminal validation reject with a clear reason; a missing referenced file is `subtask_file_missing`.

## Interaction with the planning node

The `decomposition.proposed_by` node (planning, in the default flow) still **runs** — it produces the shared plan that becomes downstream context, and it must run because the region partition's `pre` phase ends at it ([orchestrator.py:979](../../src/wastech_orchestrator/core/orchestrator.py#L979), `partition_decomposition`). What changes: when the operator manifest is present, `_engine_materialize_decomposition` ignores the planning agent's `decompose`/`subtasks` proposal and uses the operator-built decision instead. The planning role prompt gains one conditional line ("the split is operator-fixed; produce only the shared plan") so the agent does not waste a proposal. If the operator also sets `stages.planning.enabled: false`, the stub-plan path applies and the operator decision is materialized at the stub step — same seam.

## Change list

- `task/model.py` — `NormalizedTask` gains `subtasks: tuple[str, ...]` (default empty); add `subtasks` to `ALLOWED_TASK_KEYS`.
- `task/parser.py` — parse the `subtasks` reference list; round-trip it in `write_normalized` / `load_normalized`. Add a small subtask-file reader (front matter `title`/`slug`/`depends_on` + body) reusing `split_frontmatter` / `extract_section` / `slugify`.
- `task/validation_gate.py` — new reasons: `subtask_file_missing`, `invalid_subtask_path` (traversal / top-level), `subtask_count_out_of_range`, `subtask_depends_forward` (folds into the existing linear rule), `subtask_malformed`. Shape + path validation only; the linear/range gate is reused from `decide_decomposition`.
- `core/decomposition.py` — extract the units-validation core (currently inside `decide_decomposition`) into a shared helper that takes a list of `SubtaskSpec` candidates and returns a `DecompositionDecision`; add `decide_operator_decomposition(specs, *, max_subtasks)` that feeds it. `decide_decomposition` (agent path) keeps its signature.
- `core/orchestrator.py` — in `_engine_materialize_decomposition`, branch on `task.subtasks`: present ⇒ build the decision from the operator manifest (slug→order mapping, then the shared validator), reason `operator_authored`; absent ⇒ today's `read_decomposition`. Everything after (persist, `write_subtask_artifacts`, `insert_subtasks`, fan-out) is unchanged.
- `core/preflight.py` — when a task carries `subtasks`, assert the selected flow has a `decomposition:` block; else fail-closed (`flow_cannot_decompose`).
- `core/flow/packaged/roles/planning.md` — one conditional line for the operator-fixed-split case.
- No new persistent schema: the existing `decomposition_*` columns + `subtasks` rows already model the result; only `decomposition_reason` carries a new value (`operator_authored`).

## Tests

- Root with 3 valid subtask refs ⇒ one branch, three sequential per-subtask commits, one PR; `decomposition_reason == operator_authored`.
- `subtasks` of length 1 ⇒ reject (`subtask_count_out_of_range`); length > `max_subtasks` ⇒ reject.
- `depends_on` referencing a later slug / itself / unknown slug ⇒ reject (linear rule, reused).
- Subtask path with `..` / absolute / top-level-of-pending ⇒ `invalid_subtask_path`; missing file ⇒ `subtask_file_missing`.
- Subtask files in a subfolder are **not** picked up by `select_pending` (scheduler ignores them).
- Task names a flow with **no** `decomposition:` block ⇒ `flow_cannot_decompose` at preflight, no branch created.
- `subtasks` present **and** `stages.planning.enabled: false` ⇒ operator decision materialized at the stub-plan step.
- Recovery: a crashed operator-decomposed task resumes at the active uncommitted subtask (reuse the existing `reconcile_decomposed` test shape).
- Round-trip: `write_normalized` / `load_normalized` preserve `subtasks`.

## Docs

- `docs/task-authoring.md` — new "Operator-authored decomposition" subsection under [Decomposition](../task-authoring.md#decomposition-operatorflow-controlled): the `subtasks:` reference list, the subtask-file shape, subfolder placement, path rules, and the "one root, one PR" outcome. Add `subtasks` to the front-matter table.
- `docs/how-it-works.md` / functional map — note the two decomposition sources (agent-proposed vs operator-authored) collapsing into the same execution region.
- Backlog README — move this row from candidate to accepted when scheduled.

## Acceptance

- `ruff`, `mypy`, `pytest` green.
- An operator can submit one root task referencing ≥ 2 subtask files and get a single PR whose branch carries one commit per subtask, in order, with dependencies respected.
- The operator split is validated by the same gate as the agent split (no weaker `max_subtasks`, no non-linear deps).
- A task whose flow cannot host a split, a bad subtask path, or a malformed subtask file is rejected fail-closed before any branch is created.
- Subtask files never run as standalone tasks.

## Out of scope (deferred)

- **Inline subtasks in the root** (subtask bodies in the root file instead of references). The reference form is what the operator asked for; inline is a trivial later extension to the same `decide_operator_decomposition` seam.
- **Parallel / graph subtasks, per-subtask branches.** This stays linear and sequential on one branch — same constraint as today's decomposition (see the "Parallel and graph decomposition" row in [README.md](README.md)).
- **Grouping N independent existing task files into one PR.** That is a different model (multiple `task-id`s on one branch) and is explicitly not this feature.
