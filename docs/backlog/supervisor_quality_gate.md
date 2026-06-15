# Supervisor quality-gate

Status: **accepted** — not scheduled.
Date: 2026-06-14
Updated: 2026-06-15
Owner: Vladimir Makarevich

## Goal

Add an AI supervisor that evaluates selected stage outputs and emits a bounded `accept` / `rework`
verdict. The supervisor is an evaluator feeding the deterministic Core, not a controller, router,
security boundary, test authority, or replacement for `review`.

Once this feature ships, the supervisor is a mandatory part of the `implementation` workflow. It
cannot be disabled globally or per task. When summary output is enabled, the same component also
owns a final fresh read-only handoff pass after successful review. It synthesizes the already
accepted task outcome into structured human documentation and replaces the provider invocation for
`summary`; the Core still owns the `summarizing` checkpoint, validates the payload, writes
`summary.md` / `summary.json`, and applies deterministic fallback.

This reverses the canonical v1 scope decision in
[the final plan](../implementation_stages/00_orchestrator_final_plan.md) §18.1. The canonical plan
must be amended in the same change that implements this feature, not before.

## Program context and sibling changes

This document is one independently implementable part of the
[agent quality and continuity program](README.md#agent-quality-and-continuity-program):

- [Workflow execution foundation](workflow_execution_foundation.md) is the prerequisite and owns
  the read-only `evaluator` run kind (`role = supervisor`), the `fresh_each_pass` session scope,
  immutable run-artifact allocation, the shared evaluator-loop primitive, quality-action vocabulary,
  and shared policy identity;
- **this document** owns supervisor verdicts, persistence, rework accounting, and state-machine
  integration;
- [Hybrid agent testing](hybrid_agent_testing.md) owns the optional workspace-writing testing agent
  and keeps the deterministic `CheckRunner` authoritative;
- [Durable sessions and implementation/fixing affinity](durable_sessions_and_fixing_affinity.md)
  owns provider resume, editing-session persistence, provider-aware fallback, and session
  redaction.

Cross-change contracts:

1. Supervisor calls always start in a **fresh independent session each pass** (session policy
   `fresh_each_pass` — the supervisor never resumes its own prior session and never reads or updates
   the implementation/fixing editing lineage). It reads context from artifacts, not a running
   conversation.
2. Supervisor and the test-quality evaluator may share low-level provider invocation, fallback,
   artifact allocation, schema validation, and audit helpers — both are instances of the foundation's
   read-only `evaluator` primitive (`role = supervisor` vs `role = test_quality`). They remain
   separate domain components because they evaluate different things; **neither writes the
   workspace** (tests are authored by the `implementation` agent, not by an evaluator).
3. The Core remains the only state-machine owner. Provider adapters remain the only place that
   knows CLI syntax.
4. Infrastructure fallback remains infrastructure-only. Supervisor criticism is a quality verdict,
   not a provider fallback trigger.
5. Database/config migrations use the next available schema version at implementation time. If
   this is not the first program item implemented, it must build on the versions already shipped
   by its sibling changes.
6. `refinement` remains an independent typed pipeline stage. The supervisor may evaluate its output
   when configured, but does not author the enriched task or own its HITL interaction.
7. When summary output is enabled, the final handoff pass is not a new blocking quality gate. It
   cannot reopen an accepted task, apply `rework`, or consume fix budgets after deterministic
   checks and review have passed.
8. Supervisor evaluation is mandatory after every successful `implementation` and `fixing` run.
   Neither operator config nor task metadata may remove these checkpoints.

Because the `implementation` profile requires supervisor from the start (greenfield "fold"),
supervisor lands **immediately after the foundation** — before durable sessions and hybrid testing.
It does so using fresh independent requests and domain-specific persistence (the formal
fresh/disposable session scope is then formalized by durable sessions). It must not expose a
temporary disabled mode at any point.

## Current reality

- There is no supervisor by design; `core/loop_control.py` records that v1 has no supervisor agent.
- `review` and the deterministic Check Runner are the current quality gates.
- The generic prompt-template store indexes pipeline stages, so a non-stage `supervisor.md` prompt
  needs an explicit extension.
- Current global fix accounting increments primarily when entering `fixing`; same-status supervisor
  re-runs would not automatically consume the global budget.
- `summary` is currently a separate agent-routed stage. Its provider call becomes redundant once a
  final supervisor evaluation already reads the complete task outcome.
- This is a greenfield MVP with no deployed installations, so there is no migration: the required
  supervisor configuration is added to the schema and example config as part of this change and is
  mandatory from the start. The `implementation` profile requires supervisor from the start and a
  no-supervisor implementation profile never ships.

## Proposed component

Add `core/supervisor.py`. `Supervisor` wraps provider-neutral invocation and:

- runs a bounded read-only evaluation against durable artifacts;
- returns a strictly validated verdict;
- does not mutate Core state directly;
- does not touch Git;
- does not select pipeline routes or reorder/skip stages;
- uses packaged `supervisor.md` and `supervisor-final.md` prompts through safe non-stage template
  paths;
- returns structured handoff content during an enabled final handoff pass but never writes summary
  files itself.

Preferred stage-evaluation output:

```json
{
  "verdict": "accept|rework",
  "findings": [
    {
      "severity": "low|medium|high",
      "reason": "bounded non-empty string",
      "paths": ["repository/relative/path"]
    }
  ]
}
```

The consumer validates exact keys, collection/string limits, and safe repository-relative paths.
`rework` requires at least one `medium` or `high` finding. Low findings are advisory and cannot
block. `accept` cannot contain blocking findings.

Preferred final-handoff output:

```json
{
  "handoff": {
    "what": "bounded non-empty string",
    "how": "bounded non-empty string",
    "integration": "bounded non-empty string",
    "why": "bounded non-empty string"
  }
}
```

The final prompt synthesizes persisted accepted facts; it does not request a new verdict. Invalid
or incomplete handoff output uses the deterministic minimal summary instead of re-running or
blocking publishing.

## Deterministic integration

After a supervised stage successfully materialises its candidate output, the Core evaluates that
output before applying its normal transition:

- `accept`: continue to the normal next state;
- `implementation` rework: persist feedback and transition `implementing -> fixing`;
- `fixing` rework: persist feedback and re-run `fixing` in the existing status;
- `refinement` or `planning` rework: re-run the same stage from persisted feedback without a new
  state-machine edge.

The supervisor never changes a deterministic Check Runner result. It cannot make a red check green
or a green check red. `testing` is not a blocking supervisor target in this feature; any future
evaluation of the testing-agent phase must be separately designed as advisory and cannot supersede
the Check Runner.

Mandatory blocking checkpoints:

```text
implementation succeeds
  -> supervisor accept: continue to guardrails/testing
  -> supervisor rework: enter fixing

fixing succeeds
  -> supervisor accept: continue to guardrails/testing
  -> supervisor rework: repeat fixing
```

`refinement` and `planning` may be added as trusted operator-configured extra targets. The
`implementation` and `fixing` checkpoints are fixed by the workflow and cannot be removed.

### Final handoff and summary ownership

After deterministic checks and `review` succeed, the Core enters the existing `summarizing`
checkpoint and, unless summary is explicitly skipped, runs one fresh supervisor request with
`evaluation_kind = final_handoff`.

That request receives:

- original and enriched task;
- accepted plan and decomposition/subtask results;
- final diff and diff stat;
- deterministic check results;
- review findings;
- prior supervisor findings and applied rework history;
- skipped-stage audit data.

Outcomes:

- valid handoff: Core writes `summary.md` and `summary.json`;
- invalid handoff: Core writes the deterministic minimal summary;
- provider infrastructure exhaustion: record `unavailable`, write the deterministic minimal
  summary, and continue publishing;
- workspace mutation: fail closed as a security/policy violation;
- effective summary skip: record the skipped `summary` checkpoint, make no supervisor request,
  create no final-handoff evaluation, create no `summary.md` / `summary.json` or task-sidecar
  summary, and continue publishing without a summary body.

The final-handoff path replaces `_run_stage(..., Stage.SUMMARY)` in the implementation
workflow. The enabled path invokes supervisor; the skipped path invokes no agent. There is no
runtime branch that invokes a separate summary provider.

`Stage.SUMMARY` and the `summarizing` state remain as workflow/lifecycle identifiers for
configuration, audit, recovery, and backward compatibility, but `summary` is removed from the
agent-routable stage set. Do not add `Stage.SUPERVISOR`.

An effective summary skip, from either `stages.summary.enabled: false` or the existing trusted
global stage-skip configuration, suppresses the final handoff completely. Unlike the current
`_summary` behavior (which writes a stub, registers `summary.json`, and falls back to a PR body),
the implementation workflow must not create a skipped-summary stub, deterministic fallback, artifact
registration, `logs/<task-id>/summary.{md,json}`, or
`tasks/<done|failed>/<task-id>.summary.md`.

Publishing must therefore accept an absent summary path. In audit-commit mode the task file is
still moved and committed, but no summary sidecar is created. The non-interactive PR command uses
an explicit empty body rather than prompting, loading a template, or synthesizing replacement
prose. The skipped checkpoint remains visible through the existing stage-run audit/status data.
The same no-file rule applies to successful and failed/finalized terminal paths; a generic
`ensure_summary` fallback must not override the resolved skip policy.

This does not affect the mandatory supervisor evaluations after `implementation` and `fixing`;
summary enablement is an output policy, not a supervisor enable/disable control.

### Integration order inside stages

- `refinement`: validate typed output, materialise candidate `task.enriched.md`, evaluate, then
  re-run or transition to planning.
- `planning`: validate and materialise the candidate plan/raw output, evaluate, and only after
  `accept` create decomposition state and subtask rows.
- `implementation` / `fixing`: materialise `current.diff`, evaluate, then after `accept` perform
  dangerous-diff classification/approval and continue.
- implementation rework: persist evaluation, consume budget, write/update `fixing-context.json`,
  then use the new `implementing -> fixing` edge. The successful implementation provider run has
  already persisted its editing lineage, so fixing can resume it.
- fixing rework: persist evaluation, consume budget, merge supervisor feedback with the original
  check/review trigger, and run fixing again without a `fixing -> fixing` edge. The just-completed
  fixing run updates editing lineage before supervisor evaluation.
- final handoff: after successful review, either record the effective summary skip and publish
  without summary artifacts, or synthesize the complete persisted accepted outcome in a fresh
  session; Core then materialises valid prose or uses deterministic fallback.

This requires splitting the current edit-and-guardrail helper at the post-edit boundary.

## Rework limits and termination

Every applied supervisor `rework`, including same-status re-runs, must atomically increment the
task-wide `fix_iterations` budget. Add a generic `record_rework(...)` decision in
`LoopController` or equivalent; merely adding `FixLoop.SUPERVISOR` is insufficient.

**Single accounting path (no double counting).** The existing `enter_fixing(loop)` already
increments `fix_iterations` for the test-driven and review-driven loops. Supervisor reworks are a
third, distinct trigger and must flow through `record_rework(...)` **only** — they must never also
call `enter_fixing`, or the global budget would be incremented twice for one rework. Concretely:

- test/review red results → `enter_fixing(FixLoop.TEST | FixLoop.REVIEW)` (unchanged);
- supervisor `rework` (implementation or fixing, including the same-status fixing re-run) →
  `record_rework(...)`;
- both paths increment the single task-wide `fix_iterations` counter, and only one path runs per
  rework.

The `implementing -> fixing` edge taken on an implementation rework is a state transition only; it
does not itself touch any counter (the `record_rework` call owns the increment).

**Local limit (derived, not a new counter).** The local limit is scoped by
`(target_stage, subtask_order)`, not one task-wide `supervisor_fix_cycles` value, and is **derived
by counting applied supervisor evaluations** (`applied = true`) for that target in the persisted
evaluations table — there is no separate mutable counter column to keep in sync. This is a third
local dimension alongside the existing per-subtask `test_fix_cycles` / `review_fix_cycles`; all
three feed the single global `fix_iterations`. The local sequence resolves when the output is
accepted. The global budget remains task-wide across subtasks. The `final_handoff` pass cannot emit
or apply `rework` and therefore consumes no local or global fix budget.

Exhaustion follows the existing deterministic terminal policy and cannot create an infinite
supervisor loop.

## Persistence and recovery

Persist one immutable evaluation per source run, keyed at least by:

```text
task_id
evaluation_kind       # stage_output | final_handoff
target_stage
subtask_order
source_stage_run_id
input_checksum
outcome              # accept | rework | completed | unavailable
feedback_path
result_path           # validated handoff when evaluation_kind = final_handoff
applied
created_at
```

Evaluation and application are separate idempotent facts. Applying a rework and consuming its
budget must be transactional so recovery cannot apply the same verdict twice.

**Recovery while still in `implementing` / `fixing`.** The supervisor has no status of its own (no
`Stage.SUPERVISOR`, no new state); evaluation runs inline after the edit succeeds but before the
Core applies its transition, so the task status is still `implementing` or `fixing` during
evaluation. On restart in those statuses, recovery must therefore first consult the evaluations
table keyed by the edit's `source_stage_run_id`: if a matching evaluation exists, resume from its
recorded `outcome`/`applied` state (do not re-run the edit); only if none exists does it run the
supervisor evaluation. This keeps the read-only evaluation idempotent without adding a "supervising"
status.

The completed final handoff pass persists its validated result before the Core transitions to
`ready_to_publish`. If a crash occurs after persistence but before `summary.md` / `summary.json`
materialisation, recovery reuses the immutable evaluation artifact and writes the files without
invoking the supervisor again.

For an effective summary skip, recovery reuses the persisted skipped stage-run record. It must not
create a final-handoff evaluation or summary artifacts on restart.

Supervisor provider attempts must be distinguishable from pipeline-stage execution. Use the
foundation's canonical `run_kind` audit field (`run_kind = stage | evaluator`); supervisor attempts
are `run_kind = evaluator` with `role = supervisor`. Do not introduce a parallel `execution_role`
field, a `run_kind = supervisor` value, or a `Stage.SUPERVISOR`.

For the final pass, `target_stage = summary` identifies the workflow checkpoint while
`evaluation_kind = final_handoff`, `run_kind = evaluator`, and `role = supervisor` identify the
actual execution semantics. Its input checksum must cover all final context artifacts so recovery
cannot reuse a handoff produced for an earlier accepted result.

If another program item changes `state.db` first, this feature takes the next schema version rather
than assuming a fixed version number.

## Context and artifacts

Use explicit provider-neutral context paths such as:

- `evaluated_output_path`;
- `supervisor_feedback_path`;
- final check/review and rework-history paths;
- existing task, plan, diff, check, review, and active-subtask paths as applicable.

Do not overload `human_input_path` or `review_artifacts_path`. A fixing request receives both the
original check/review trigger and supervisor feedback.

Each evaluation artifact is immutable and namespaced by the source run. A single overwritten
`supervisor.json` is not sufficient.

## Security and failure policy

- Supervisor uses a read-only permission profile and a fresh session each pass (`fresh_each_pass` —
  it never resumes a prior supervisor session).
- Capture workspace snapshots before and after evaluation.
- For mandatory `implementation`/`fixing` evaluations, provider infrastructure exhaustion or an
  invalid verdict after eligible fallback stops in `manual_action_required`; the Core cannot assume
  acceptance when the required gate produced no valid decision.
- For the non-blocking final handoff only, provider infrastructure exhaustion or invalid output
  writes the deterministic minimal summary and continues publishing.
- Any supervisor workspace mutation is a security/policy failure and stops fail-closed.
- Session IDs from supervisor calls are not threaded into any later stage.
- The final handoff is untrusted structured prose: Core enforces exact keys and size limits,
  redacts unsafe content, and writes the files itself.
- Final handoff unavailability or validation failure never blocks a task whose
  deterministic checks and review already passed.
- Secrets and raw session handles follow the sibling
  [session redaction contract](durable_sessions_and_fixing_affinity.md#security-and-redaction).

## Configuration

Add required trusted config under `agents.supervisor`:

```yaml
agents:
  supervisor:
    primary: claude
    fallback: codex
    model: null
    reasoning: low
    timeout_seconds: 300
    additional_stages: []  # optional: refinement, planning
    max_rework_per_stage: 1
```

There is deliberately no `enabled` field. Missing required supervisor configuration fails config
validation (this change makes the block mandatory; there is no prior release to migrate from).

The exact provider/model defaults are resolved from existing provider configuration. Mandatory
blocking targets are always `implementation` and `fixing`; `additional_stages` may contain only
`refinement` and `planning`. It cannot remove mandatory targets. `testing`, `review`, and `summary`
are invalid entries. The `final_handoff` execution kind always runs when summary is not skipped; it
is not a toggle, an entry in `additional_stages`, or supervision of a pre-existing summary output.

Any separate summary provider route/model is removed (it never ships). The supervisor's trusted
route/model configuration is used for the fresh final request. Task-level `agents.summary` and
`stages.summary.{model,reasoning}` are invalid; only
`stages.summary.enabled` remains valid as the summary-output policy. `false` skips the handoff and
all summary files; omitted/`true` enables the handoff and deterministic fallback. Task content
cannot disable or reconfigure the mandatory supervisor quality gates, resume an editing session,
or widen supervisor permissions.
Startup/preflight validation must also fail before task side effects when neither the primary nor
eligible fallback route can perform the required read-only supervisor invocation.

## Single supervised pipeline (no rollout/migration)

Do not maintain parallel supervisor/no-supervisor pipelines.

This is a greenfield MVP with no deployed state, so there is **no profile cutover and no migration**.
The `implementation` profile requires supervisor from the start (`supervisor_policy: required`); a
no-supervisor implementation profile never ships. There is no `implementation-v1`/`implementation-v2`
pair, no upgrade gate, no "historical version" readability rule, and no config key or task field that
can select a no-supervisor profile. The runtime contains exactly one implementation runner and one
active policy.

The example/default config ships the required `agents.supervisor` block, and any separate summary
provider route/defaults are simply never introduced (rather than removed by an upgrade step).

## Expected touchpoints

- `core/supervisor.py` (new domain component);
- `core/orchestrator.py` stage output/application boundaries;
- `core/loop_control.py` generic rework accounting;
- `core/state_machine.py` for `implementing -> fixing`;
- `state_store.py` for immutable evaluations, application markers, counters, and the
  `run_kind = evaluator` / `role = supervisor` audit fields;
- current summary checkpoint/writer to replace the old provider run;
- finalization and Git publishing interfaces so summary paths/PR bodies are optional on an
  effective summary skip;
- `core/prompts.py` plus `templates/prompts/supervisor.md` and `supervisor-final.md`;
- removal of the legacy `summary.md` prompt and summary agent route/model configuration;
- removal of `summary` from agent-routable stages while retaining its lifecycle checkpoint;
- provider-neutral request/context types;
- config schema/loader/validation/upgrade and example configuration;
- canonical plan, user docs, and `CHANGELOG.md`.

## Minimum tests

- crash/restart before and after verdict application does not duplicate evaluation or budget use;
- local limits are independent by stage/subtask while the global budget spans the task;
- fixing rework is same-status and preserves original check/review context;
- implementation rework uses the new deterministic edge;
- planning rejection creates no accepted decomposition rows or subtask commits;
- final input checksum changes after fixing and prevents reuse of a stale handoff;
- completed final pass writes the four-field handoff through Core, not the provider;
- crash after completed handoff persistence rematerialises summary files without another provider
  invocation;
- malformed handoff uses deterministic summary fallback without rework;
- final supervisor infrastructure exhaustion records `unavailable`, writes deterministic fallback,
  and remains publishable;
- no separate `Stage.SUMMARY` provider run exists;
- task-level summary route/model overrides cannot select or modify the final supervisor;
- task-level and global summary skips suppress the final handoff pass, persist a skipped checkpoint,
  and create no summary files, fallback, artifact registrations, or task-sidecar summary;
- publishing succeeds with an explicit empty PR body and audit-commit mode commits only the moved
  task file;
- success, failure, and finalization paths all honor the skip and never recreate summary artifacts;
- restart after a persisted summary skip does not invoke supervisor or create summary artifacts;
- final handoff cannot emit rework or consume local/global fix budgets;
- required supervisor config has no `enabled` key and cannot omit mandatory targets;
- missing required config and attempts to disable/reconfigure supervisor through task metadata fail
  validation;
- startup/preflight rejects configuration with no usable required supervisor route before task
  side effects;
- mandatory evaluation infrastructure exhaustion stops in `manual_action_required`;
- a supervisor rework increments the global `fix_iterations` exactly once (the `record_rework` path
  never also calls `enter_fixing`);
- the local rework limit is derived from the count of applied evaluations for
  `(target_stage, subtask_order)`, not a separate mutable counter;
- recovery in `implementing`/`fixing` consults the evaluations table before re-running the edit;
- read-only workspace mutation fails closed;
- malformed/unbounded verdicts are rejected;
- low findings cannot block and rework requires a blocking finding;
- supervisor never receives or updates implementation/fixing session lineage;
- enabled summary output remains publishable through deterministic fallback;
- provider attempts are audited under `run_kind = evaluator` / `role = supervisor` and artifacts are
  never overwritten.

## Definition of done

- Supervisor remains an evaluator and all transitions are Core-owned.
- Every applied rework is bounded, persisted, and restart-idempotent.
- The read-only and fresh-session contracts are enforced deterministically.
- Supervisor is mandatory for the implementation workflow and has no enable/disable control.
- When summary output is enabled, the final fresh pass owns handoff generation and there is no
  separate summary provider invocation.
- Core remains the owner of `summarizing`, optional summary validation/writes, fallback, skip
  auditing, and publishing.
- The `implementation` profile requires supervisor from the start; the codebase never ships and does
  not maintain a no-supervisor runtime path.
- `ruff`, `mypy`, and `pytest` pass.
- Canonical plan, configuration docs/examples, `how-it-works.md`, backlog/follow-ups, and
  `CHANGELOG.md` are updated in the same implementation change.
