# Documentation update stage

Status: **accepted** — outdated. Date: 2026-06-15 Owner: Vladimir Makarevich

## Goal

Add an optional finalizing **documentation stage** to the `implementation` workflow: a dedicated agent that updates the target repository's project documentation and all related `.md` files to reflect the change that was just implemented. It is enabled by default and can be turned off per task. It is an author (it writes docs), never a quality gate.

Distinguish two things that are easy to confuse:

- this stage updates the **target repository's** documentation as part of the delivered change;
- the orchestrator's own `/sync-docs` discipline (CLAUDE.md Stop gate) is about the orchestrator's _own_ repo during its development — unrelated to this feature.

It is also distinct from the supervisor **final handoff / summary**: the handoff is a read-only synthesis written to `summary.md` / the PR body about _what the task did_; this stage writes real, committed documentation _inside the target repo_.

## Foundation dependency

Builds on the [workflow execution foundation](workflow_execution_foundation.md): it is a new `implementation`-profile pipeline stage with `run_kind = stage` (an author/editor that produces a deliverable, not an evaluator), routed and prompted through the existing stage machinery, and guarded by the foundation's exact-delta / path-containment primitives. It slots into the post-supervisor implementation profile, so it is cleanest to land after [supervisor quality-gate](supervisor_quality_gate.md) (the summary/handoff ordering below assumes the supervisor change). It is otherwise independent of the rest of the quality program.

## Current reality

- The implementation DoD already expects "documentation is synchronized where behavior, configuration, CLI, or architecture changed", but there is no dedicated stage — it relies on the implementation agent doing it inline, with no separate prompt, route, model, or guardrail.
- The pipeline ends `... review -> summarizing -> ready_to_publish -> commit/push/PR`. There is no status or routable stage for documentation.
- The prompt template store is indexed by routable `Stage` values; a new stage needs an entry.

## Placement in the flow

The stage runs **after `review` (and the mandatory supervisor accept) succeed and before the final handoff/summary**, so it has the complete accepted picture and so the handoff and the PR reflect the doc changes:

```text
... review (supervisor accept)
  -> documenting              # new status; optional, default on
       enabled:  run the documentation agent, validate the doc-only delta, then continue
       disabled/unavailable:  record a skip/unavailable and continue
  -> summarizing              # final handoff sees the doc delta too
  -> ready_to_publish -> commit/push/PR   # doc changes are part of the same PR
```

State machine: add status `documenting` with edges `reviewing -> documenting -> summarizing`. When the stage is skipped, the existing `reviewing -> summarizing` edge is used (both edges must be allowed, mirroring the existing optional-stage skip pattern).

## Documentation agent component

A dedicated stage agent (`run_kind = stage`), not an evaluator and not a renamed summary:

- uses a packaged `documentation.md` prompt with operator override (default + template, replace/append), through the existing prompt template store; `Stage.DOCUMENTATION` is added to the agent-routable set;
- runs with `workspace-write`, **restricted to documentation paths** (see guardrails);
- receives the full accepted-outcome context (below) and read access to the existing repo docs it must update;
- updates project documentation and related `.md` files to match the implemented change (READMEs, `docs/`, changelog entries, configuration/CLI/architecture docs, etc.);
- never commits, pushes, publishes, or changes source code.

## Context (so it can update docs correctly)

The agent must receive enough to know _what was done_ and _which docs it affects_, via explicit provider-neutral context paths:

- the original and enriched task;
- the accepted plan and decomposition/subtask results;
- the final diff and diff stat (what actually changed);
- deterministic check results and review findings;
- the existing documentation tree it may read and update (within the allowed doc paths);
- optionally a docs map / hint of which docs relate to the changed areas.

This is the same accepted-outcome context the final handoff consumes, plus workspace read of the docs to edit.

## Permissions and deterministic guardrails

Because this agent writes the workspace, its delta is guarded deterministically (it is an author, so unlike the read-only evaluators there _is_ a delta to validate):

- **doc-path allowlist** — the exact agent delta must be contained in configured documentation paths (default: `*.md`, `docs/`, `README*`, `CHANGELOG*`; operator-configurable). Repository-specific extra doc paths belong in trusted operator config, never task content or agent output;
- **no source-code / config / CI / dependency / quality-gate edits** — an out-of-policy edit is a policy violation and fails closed (it cannot become acceptable merely because it is a `.md`-looking file outside the allowlist);
- run **dangerous-diff classification** over the doc delta as a second layer (deletions/renames of existing docs still get classified);
- re-run the deterministic **CheckRunner** over the resulting tree before publish so any configured markdown-lint / link-check still gates (cheap; doc edits rarely affect code checks). Whether the full profile re-runs or only doc-relevant checks is an operator/design choice.

## Optional and default-on semantics

- **Default enabled**, globally and per task.
- **Per-task opt-out** via the existing per-task stage control `stages.documentation.enabled: false` (consistent with `stages.testing` / `stages.summary`); a friendlier task alias such as `update_docs: false` may map to the same control.
- **Global disable** via `agents.documentation.enabled: false` or the existing global stage skip.
- An effective skip records an audited skipped `documentation` checkpoint and creates no doc delta; publishing proceeds normally.

## Configuration

```yaml
agents:
  documentation:
    enabled: true # default on
    primary: claude
    fallback: codex
    model: null
    reasoning: low
    timeout_seconds: 600
    allowed_doc_paths: [] # operator-trusted extra doc roots beyond the defaults
  routing:
    documentation:
      primary: claude
      fallback: codex
```

Per-task `stages.documentation.{enabled,model,reasoning}` overrides apply (model/reasoning integrate with the existing per-stage overrides). Task content cannot expand `allowed_doc_paths`, change the route beyond `agents.allowed`, or let the agent write outside the doc allowlist.

## Infrastructure and failure policy

- Provider infrastructure exhaustion while enabled: record the documentation checkpoint `unavailable`, emit an audited warning, and continue to publish **without** doc updates (non-blocking — it must not strand a finished, reviewed change). Fallback remains infrastructure-only.
- A guardrail violation (edit outside the doc allowlist, dangerous change) fails closed.
- The agent returns no accept/rework verdict — it is an author, not an evaluator; quality of the prose is not a state-machine gate.

## Recovery

Idempotent stage checkpoint keyed by execution unit. A crash after the doc delta is written but before the commit must not re-run the agent: recovery detects the completed `documenting` checkpoint and continues to summarizing/publishing. A fresh operator `rerun` may produce a new doc delta; an automatic continue reuses the persisted one.

## Publishing

Doc changes are ordinary repository changes: they are committed (in the scoped code/docs commit) and included in the same Pull Request. No separate publishing path.

## Expected touchpoints

- `core/state_machine.py` — new `documenting` status and `reviewing -> documenting -> summarizing` (plus the skip edge `reviewing -> summarizing`);
- `core/orchestrator.py` — documentation stage execution, guardrail, skip, and recovery;
- `providers/base.py` `Stage` + `config/schema.py` `ROUTABLE_STAGES` / `SKIPPABLE_STAGES` (`documentation` is routable and skippable, never `implementation`-style mandatory);
- doc-path exact-delta guard reusing the foundation path-containment primitives;
- `core/prompts.py` + `templates/prompts/documentation.md`;
- `config/schema.py`, loader, validation, upgrade, and example configuration (`agents.documentation`
  - routing);
- provider-neutral accepted-outcome context paths;
- canonical plan, `how-it-works.md`, configuration docs/examples, and backlog/follow-ups.

## Minimum tests

- default-on: with no override, the documentation stage runs after review and before summarizing;
- per-task `stages.documentation.enabled: false` skips it; the run records an audited skip and publishing proceeds with no doc delta;
- global disable skips it identically;
- the agent delta is constrained to the doc allowlist; an edit to source/config/CI/dependency files fails closed;
- deletion/rename of existing docs is classified by dangerous-diff;
- the resulting tree re-runs the deterministic checks before publish;
- doc changes are committed in the same PR;
- infrastructure exhaustion records `unavailable` and still publishes the reviewed change without doc updates;
- restart after the doc delta is written does not re-run the agent or duplicate edits;
- the final handoff/summary reflects the doc delta (documentation runs before summarizing);
- task content cannot expand `allowed_doc_paths` or route outside `agents.allowed`.

## Open questions

- Should the documentation delta also be evaluated by the supervisor (a late workspace edit currently bypasses the supervisor's implementation/fixing checkpoints), or are the deterministic doc-path + dangerous-diff + check re-run guards sufficient?
- Should the full check profile re-run after doc edits, or only doc-relevant checks (lint/links)?
- Default `allowed_doc_paths` set, and handling of non-`.md` doc formats (e.g. OpenAPI YAML, generated docs).
- How best to give the agent a "which docs relate to the changed code" map without over-coupling.
- Should a separate documentation-only PR mode exist (analogous to deep_research), or is the in-PR doc update the only mode?

## Definition of done

- `documentation` is an optional, default-on finalizing stage of the `implementation` workflow with its own prompt (default + override), route, and model/reasoning settings.
- It updates the target repo's docs from the full accepted-outcome context and writes nothing outside the configured doc allowlist.
- It is skippable per task and globally; a skip is audited and never strands publishing.
- Deterministic doc-path and dangerous-diff guards plus a check re-run keep the publish gate honest; guard violations fail closed.
- Execution is restart-idempotent; doc changes ship in the same PR.
- `ruff`, `mypy`, and `pytest` pass.
- Canonical plan, configuration docs/examples, `how-it-works.md`, and backlog/follow-ups are updated in the same implementation change.
