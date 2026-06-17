# Workflow execution foundation

Status: **accepted* — outdated. Date: 2026-06-14 Owner: Vladimir Makarevich

## Goal

Introduce the smallest provider-neutral workflow and execution contracts needed by these four backlog tasks:

1. [Task workflow profiles](task_workflow_profiles.md);
2. [Durable sessions and implementation/fixing affinity](durable_sessions_and_fixing_affinity.md);
3. [Hybrid agent testing](hybrid_agent_testing.md);
4. [Supervisor quality-gate](supervisor_quality_gate.md).

The foundation-only release must preserve the current `implementation` pipeline byte-for-byte at the behavioral boundary before any follow-on feature lands. It prepares stable extension points; it does not pre-implement the four feature tasks.

## Why this is a separate prerequisite

All four tasks need the Core to resolve and persist several concepts that currently exist only as implicit stage-specific behavior:

- which workflow owns the task;
- which execution role is running;
- which stage/session/output policy applies;
- which policy snapshot recovery must reuse;
- which component may write to the repository, artifacts, or control workspace;
- which deterministic action follows a quality result.

Implementing those concepts independently in each feature would create incompatible enums, schema columns, request builders, artifact layouts, and recovery rules. A single prerequisite is therefore justified, but its scope must remain narrow enough that it does not become a speculative rewrite of the orchestrator.

## Architectural decision

Do **not** build an arbitrary user-defined workflow graph engine in this task.

The foundation uses:

```text
task file
  -> structural validation
  -> task_type resolution
  -> built-in WorkflowProfileRegistry
  -> persisted ResolvedWorkflowProfile
  -> WorkflowRunner selected by runner_kind
  -> existing implementation pipeline
```

There is a single built-in `implementation` profile. Its `implementation` runner delegates to the current orchestrator pipeline and retains the canonical stages, state transitions, checks, Git lifecycle, fallback rules, and recovery behavior.

This is a greenfield MVP with no deployed installations and no production state, so the foundation does **not** carry a transitional/versioned cutover and never ships a no-supervisor implementation profile. The single `implementation` profile declares `supervisor_policy: required` from the start; the foundation contributes only the contract/plumbing layer, and the supervisor component (delivered by the [supervisor quality-gate](supervisor_quality_gate.md) task) makes the profile runnable end-to-end. `profile_version` is retained only as a forward-looking audit/identity attribute for future profile evolution and new task types — there is no protocol for migrating between profile versions and no second runtime profile.

Later profile work may add dedicated runners or a common ordered-stage engine after the `deep_research` and `security_audit` requirements prove which abstraction is actually needed.

This incremental design avoids two unsafe extremes:

- forcing research/audit stages into the current implementation `Stage` enum;
- replacing the working implementation state machine with a generic graph before another profile exists.

## Scope

### 1. Task type and built-in profile registry

Add normalized `task_type` handling:

```text
missing task_type -> implementation
known enabled task_type -> resolve built-in profile
unknown/disabled task_type -> fail validation before side effects
```

Introduce a provider-neutral built-in registry with explicit versions:

```text
WorkflowProfile
  task_type
  profile_version
  runner_kind
  stages[]
  permission_ceiling
  supervisor_policy
  output_policy
  publishing_policy
```

Initial entry:

```text
task_type: implementation
profile_version: implementation
runner_kind: implementation
supervisor_policy: required
```

Profiles are code/configuration owned by the operator and application. Task prose cannot define, replace, or mutate a profile.

`supervisor_policy` is `required` for the `implementation` profile from the start — no no-supervisor implementation profile ever ships. The foundation slices (F1–F4) deliver only the contract/plumbing layer and do not themselves invoke agents; an end-to-end runnable implementation profile additionally requires the supervisor component, delivered by the [supervisor quality-gate](supervisor_quality_gate.md) task. Future profiles (`deep_research`, `security_audit`) may declare a different `supervisor_policy`.

This delivers Phase 1 of [task workflow profiles](task_workflow_profiles.md#17-rollout-plan). That feature document retains ownership of `deep_research`, `security_audit`, and later profile extensions.

### 2. Resolved profile snapshot

Resolve one immutable, secret-free profile snapshot after task validation and before branch creation or provider execution:

```text
ResolvedWorkflowProfile
  task_type
  profile_version
  runner_kind
  resolved_stages[]
  resolved_routes
  permission_ceiling
  supervisor_policy
  output_policy
  publishing_policy
  profile_fingerprint
```

Persist the canonical snapshot as a row in `state.db` together with its `profile_fingerprint`. Recovery, `rerun --continue`, and all later stage decisions must reuse this persisted snapshot rather than silently re-resolving changed configuration midway through a task.

The snapshot must contain no secrets, raw session handles, full environment values, or sensitive security findings.

On restart, recovery **trusts the persisted snapshot and never re-resolves the profile from current configuration.** It performs only three checks:

- snapshot integrity: recompute and compare `profile_fingerprint`;
- profile/runner existence: reject an unknown or no-longer-registered runner/profile version;
- security capability: re-check current hard security capabilities and refuse to widen the saved policy.

Stop in `manual_action_required` only when one of those checks fails — i.e. the saved workflow can no longer be executed safely. A benign, unrelated configuration change must not invalidate an in-flight task. (In the foundation-only release this path is effectively dormant: the single `implementation` profile is always registered and security can only narrow.)

A fresh rerun creates a new resolved snapshot. A continue rerun preserves the previous one.

### 3. Provider-neutral execution descriptor

Introduce a normalized descriptor used when Core prepares an agent or deterministic execution:

```text
ResolvedExecutionPolicy
  workflow_stage_id
  run_kind
  route
  model
  reasoning
  permission_profile
  session_scope
  lineage_key
  input_artifacts[]
  output_policy
  quality_action
  timeout_seconds
```

The descriptor is an orchestration contract, not provider argv. Provider-specific command syntax remains exclusively in `providers/`.

#### Stage identity

Keep the current canonical `Stage` enum for the `implementation` profile. Add a separate validated workflow-stage identifier at the workflow boundary so future built-in profiles are not forced to pretend that `repository_analysis` or `threat_analysis` is an implementation stage.

For the initial profile:

```text
workflow_stage_id = Stage.value
```

Do not accept arbitrary task-provided stage IDs.

#### Run kind

`run_kind` identifies why a run exists without creating fake pipeline stages. There are exactly two coarse buckets:

```text
stage       # produces the deliverable (the pipeline author/editor: implementation, fixing, executor, …)
evaluator   # read-only; judges a produced artifact and returns a bounded verdict
```

This is the single canonical name and value set for the run-role audit field. The sibling documents consume `run_kind` with these exact values; they must not introduce a parallel `execution_role` field, a `pipeline_stage`/`supervisor`/`testing_agent` `run_kind` value, or a `Stage.SUPERVISOR`.

Every `evaluator` run also carries a fine-grained `role` discriminator naming its purpose, e.g.:

```text
role: supervisor | test_quality | critic | verifier
```

so audit and recovery treat all evaluators uniformly (read-only, own session, bounded loop, immutable verdict) while still distinguishing which evaluator produced a given record. (Supervisor additionally keeps its `evaluation_kind = stage_output | final_handoff` sub-field.)

##### The shared evaluator-loop primitive

All evaluators are instances of one primitive that the Core owns:

```text
independent read-only run -> validated verdict (accept | rework)
  accept -> Core continues
  rework -> Core applies a bounded QualityAction (enter_fixing | repeat_stage), counted against a budget
```

Parameters per instance:

- `role` (above);
- session policy — `fresh_each_pass` (the implementation `supervisor`) or `resume_own_lineage` (an evaluator that holds a multi-round dialogue, e.g. a research `critic`); an evaluator's session is **always its own and never the author's editing lineage**, so independence holds either way;
- the bounded rework budget — **per-instance and operator-configurable** (granular: one evaluator may allow 1 rework, another 3, another 10, by task complexity/type), derived by counting applied verdicts; plus the shared global `fix_iterations` cap. There is **no single shared rework number** across evaluators. (One deliberate v1 exception: the `deep_research` citation-checker loop is pinned to 1 — see that feature.)

The implementation `supervisor`, the optional `test_quality` evaluator, and the research `critic`/`verifier` are all configured instances of this primitive. The foundation introduces the contract and audit field; it does not itself invoke any evaluator — the implementation runner consumes the resolved `supervisor_policy: required`, and the supervisor component that satisfies it is delivered by the supervisor task.

The same workflow checkpoint may use a different run kind. After the supervisor change, the supervisor executes `workflow_stage_id = summary` with `run_kind = evaluator`, `role = supervisor` when summary output is enabled, replacing the old summary provider call while preserving the Core-owned `summarizing` lifecycle checkpoint. When summary is skipped, the checkpoint records a skip and creates no execution or output artifact.

#### Session scope

Use an explicit provider-neutral session scope:

```text
fresh_disposable
editing_lineage
```

`implementation` and `fixing` (the stage authors) resolve to `editing_lineage`. Evaluator runs use **their own** session, never the author's editing lineage: the implementation `supervisor` uses `fresh_disposable` (fresh each pass), while an evaluator that holds a multi-round dialogue may `resume_own_lineage` (its own resumable session, still independent of the author). `lineage_key` is meaningful only for a resumable scope.

In the foundation slice the `editing_lineage` scope is recorded as policy and backed by the **existing in-memory session continuity** (Claude `--resume`); the foundation does not yet build a durable lineage store or require a persisted, validated `lineage_key`. Durable, per-execution-unit lineage and the mandatory `lineage_key` are owned by [durable sessions](durable_sessions_and_fixing_affinity.md). Current implementation behavior must not accidentally become more durable than it already is merely because the contract exists.

### 4. Output and artifact policy

Define a reusable resolved output policy instead of scattering stage-specific path assumptions:

```text
ResolvedOutputPolicy
  target_repository_writes
  control_workspace_writes
  artifact_class
  allowed_path_policy
```

`publishing_policy` is **not** part of `ResolvedOutputPolicy`. It is a single profile-level field (on `WorkflowProfile` / `ResolvedWorkflowProfile`) because publishing is a terminal property of the whole workflow, not a per-stage write concern. `ResolvedOutputPolicy` covers only writes, paths, and artifact class. This keeps exactly one source of truth for publishing.

Initial policy identifiers should cover the known requirements without embedding feature logic:

```text
target_repository_writes:
  implementation_scoped
  test_owned_delta
  approved_document_only
  none

control_workspace_writes:
  normal_artifacts
  private_report
  none

publishing_policy:
  pull_request
  documentation_pull_request
  none
```

Only `implementation_scoped`, `normal_artifacts`, and `pull_request` are active in the foundation. The remaining identifiers reserve compatible vocabulary; their enforcement is implemented by the owning feature.

Provide common deterministic primitives for:

- path normalization and containment;
- before/after workspace snapshots;
- exact delta calculation;
- artifact destination resolution;
- immutable run artifact allocation.

Do not weaken or replace the existing scoped-staging, dangerous-diff, secret-redaction, or Check Runner controls. The implementation profile maps to those existing controls.

### 5. Quality action and budget vocabulary

Define the policy vocabulary needed to interpret quality outcomes:

```text
QualityAction
  continue
  enter_fixing
  repeat_stage
  stop_manual
  fail
```

Each action maps to canonical state-machine behavior so a follow-on feature cannot invent its own mapping:

```text
continue      -> normal next status for the stage
enter_fixing  -> the `implementing -> fixing` edge
repeat_stage  -> re-enter the same status from persisted feedback (no new self-loop edge)
stop_manual   -> manual_action_required
fail          -> failed
```

The deterministic Core remains the only component that applies an action. Agents, profiles, and providers may return validated facts or verdicts but cannot transition state directly.

The descriptor may reference a local budget key and the existing task-wide fix budget. This task does not add supervisor rework counters or change current fixing semantics; it only prevents the supervisor feature from inventing a separate incompatible action/budget model.

### 6. Configuration and override precedence

Resolve authority in this order:

```text
hard canonical/security invariants
  -> built-in workflow permission/publishing ceiling
  -> trusted operator configuration
  -> validated task overrides allowed by the profile
  -> persisted runtime route/affinity decision within those limits
  -> infrastructure-only fallback
```

Rules:

- a task can never widen filesystem, network, output, or publishing authority;
- route/model/reasoning overrides remain limited by existing provider and stage allowlists;
- runtime affinity may choose only an already-allowed provider;
- fallback cannot change workflow, output, permission, or publishing policy;
- profile resolution completes before branch creation and any provider invocation.

### 7. Audit and persistence baseline

Add only the common metadata that all four tasks need:

```text
task:
  task_type
  profile_version
  profile_fingerprint
  resolved_profile_reference

run:
  workflow_stage_id
  run_kind             # stage | evaluator
  role                 # evaluator discriminator: supervisor | test_quality | critic | verifier (NULL for run_kind=stage)
  execution_unit
  execution_policy_fingerprint
```

`execution_unit` is the foundation-owned identity of the thing being executed: the pair `(task_id, subtask_order)`, where `subtask_order` is `NULL` for the root task and the linear order for a decomposed subtask. It reuses the existing nullable `StageRunRow.subtask_order`. Durable sessions, hybrid testing, and supervisor key their per-unit state on this identity rather than redefining it.

Two fingerprints exist and are named distinctly: `profile_fingerprint` (the resolved profile snapshot) and `execution_policy_fingerprint` (a single `ResolvedExecutionPolicy` descriptor). There is no third fingerprint name.

Use the next available config/DB schema versions at implementation time. Do not reserve a numeric version in this document.

Feature-specific tables remain feature-owned:

- editing lineage belongs to durable sessions;
- testing checkpoints belong to hybrid testing;
- supervisor evaluations/applications belong to supervisor;
- research/audit outcomes belong to workflow profiles.

## Explicit non-goals

- no `deep_research` or `security_audit` execution;
- no arbitrary YAML-defined/custom workflow graph;
- no replacement of the current implementation state machine;
- no Claude/Codex resume implementation or session-ID persistence;
- no testing-agent or supervisor invocation;
- no supervisor verdict/rework schema;
- no testing-agent path policy enforcement;
- no private security-report storage;
- no changes to deterministic Check Runner authority;
- no changes to provider fallback semantics;
- no changes to Git publishing ownership;
- no broad renaming of existing statuses or stages;
- no transitional/versioned profile cutover or migration machinery (greenfield MVP).

## Delivery slices

The prerequisite should be implemented as four reviewable slices, not one large refactor.

### Slice F1: task type and profile selection

- parse `task_type`, defaulting to `implementation`;
- validate known/enabled profiles before side effects;
- add the built-in registry and the single `implementation` profile;
- persist task type, profile version, and `profile_fingerprint`;
- add compatibility tests.

Exit condition: tasks run exactly as before through the explicitly selected `implementation` profile. (Greenfield MVP: there is no deployed state to migrate.)

### Slice F2: resolved execution policy

- add `ResolvedWorkflowProfile` and `ResolvedExecutionPolicy`;
- centralize precedence and ceiling validation;
- map current implementation stages/routes/permissions into the descriptor;
- persist and recover the immutable snapshot;
- fail closed on fingerprint/version mismatch.

Exit condition: every current stage can explain its resolved workflow policy without changing its runtime behavior.

### Slice F3: shared execution and output primitives

- add execution-role and session-scope vocabulary;
- add provider-neutral artifact destination and immutable allocation helpers;
- add reusable snapshot/delta/path-containment primitives;
- add common audit metadata;
- keep all feature-specific components and tables absent.

Exit condition: each follow-on feature can consume common contracts without adding a parallel request/audit/output-policy framework.

### Slice F4: compatibility gate

- run parity tests for the existing implementation pipeline;
- verify restart, continue rerun, fresh rerun, fallback, checks, review, and publishing behavior;
- document extension points and ownership boundaries;
- update the canonical plan and public architecture/configuration docs in the implementation change.

Exit condition: no follow-on feature is installed yet and observable implementation behavior is unchanged.

## Dependency map

| Follow-on task | Foundation contract it consumes | Feature-owned work |
| --- | --- | --- |
| [Task workflow profiles](task_workflow_profiles.md) | Registry, profile snapshot/version, workflow stage IDs, output/publishing policy, runner selection. | `deep_research`, `security_audit`, their runners/stages, result schemas, network policy, private storage, and publishing behavior. |
| [Durable sessions](durable_sessions_and_fixing_affinity.md) | Execution role, session scope, lineage key, execution unit, persisted policy identity. | Vendor resume, raw handle storage/redaction, lineage transaction, affinity, provider-aware retry/fallback. |
| [Hybrid agent testing](hybrid_agent_testing.md) | `evaluator` run kind (`role = test_quality`), own session scope, immutable evaluation artifacts, quality-action/bounded-rework vocabulary, Check Runner mutation-guard primitive. | Read-only test-quality verdict schema, evaluation persistence, the rule that tests are authored by the `implementation` agent (the evaluator never writes files), and Check Runner remaining the sole publish gate. |
| [Supervisor quality-gate](supervisor_quality_gate.md) | `evaluator` run kind (`role = supervisor`), fresh-each-pass session scope, immutable run artifacts, quality-action vocabulary, budget key, run-kind/stage separation, persisted `supervisor_policy`, and optional output-artifact vocabulary. | Required verdict schema, evaluation/application persistence, local rework counters, stage integration, final handoff schema, Core-owned optional summary materialisation/skip behavior, read-only mutation guard, and editing the `implementation` profile in place to `supervisor_policy: required`. |

## Recommended implementation order

```text
workflow execution foundation (F1 -> F4)
  -> supervisor quality-gate
  -> durable sessions and fixing affinity
  -> hybrid agent testing
  -> task workflow profiles: deep_research
  -> task workflow profiles: security_audit
```

Supervisor lands **immediately after the foundation**: the `implementation` profile requires it to run end-to-end (`supervisor_policy: required`), so supervisor starts on ad-hoc fresh requests before the formal session-scope machinery exists. Durable sessions follows — it formalizes the fresh/disposable and editing-lineage scopes the supervisor already relies on and adds implementation/fixing affinity ordered around the now-existing supervisor evaluation. Hybrid testing follows durable sessions, and its flow likewise assumes the mandatory supervisor already runs. **Supervisor must therefore precede both durable sessions and hybrid testing.** `deep_research` and `security_audit` come last because they benefit from the same run-kind, fresh-session, output-policy, and evaluator contracts.

### Supervisor enablement (no cutover)

Because this is a greenfield MVP with no deployed state, there is no versioned profile cutover and no transitional no-supervisor profile. The single `implementation` profile requires supervisor from the start; the supervisor task delivers the component and wires the mandatory evaluation checkpoints. There is no `implementation-v1`/`implementation-v2` pair, no upgrade gate, and no "historical version" readability rule. The only ordering constraint is that the supervisor component must exist before the implementation profile can run end-to-end.

## Risks and required decisions

### Configuration drift during recovery

Re-resolving a profile from current configuration after restart can silently change route, permissions, output policy, or publishing behavior. Recovery must therefore use the persisted resolved snapshot and fail closed when it cannot be honored.

### Premature generic state machine

The current and proposed workflows have different terminal behavior and side effects. A generic graph engine before two real workflow implementations exist would encode assumptions that are expensive to undo. Keep one `implementation` runner behind a stable policy boundary.

### Stage enum pollution

Supervisor and testing agent are execution roles, not pipeline stages. Research/audit stage IDs are workflow-local built-in identifiers. Do not add all of them to the current implementation `Stage` enum merely to reuse routing or prompt maps.

### Security policy placeholders

Reserved output-policy identifiers are not enforcement. A profile cannot be enabled until every declared permission, output, result, and publishing policy has a deterministic implementation and tests.

### Schema ownership

The foundation owns common profile/run identity only. Adding empty feature-specific tables would couple migrations and make independent implementation harder.

## Minimum tests

- missing `task_type` selects the `implementation` profile;
- an unknown or disabled task type fails before branch/provider side effects;
- the registry is deterministic and rejects duplicate task type/version registration;
- task content cannot select `runner_kind`, stages, permissions, outputs, or publishing policy;
- the resolved profile snapshot is canonical and fingerprint-stable;
- restart and `rerun --continue` reuse the persisted snapshot;
- a fresh rerun resolves a new snapshot;
- unknown profile/runner version or corrupt fingerprint fails closed;
- a task cannot widen profile permissions or publishing authority;
- route/model/reasoning overrides preserve existing precedence and allowlists;
- implementation stages map to their existing `Stage.value`;
- `evaluator` runs (any `role`) do not become `Stage` values;
- `fresh_disposable` rejects a lineage key; `editing_lineage` carries an optional `lineage_key` in the foundation (durable sessions later makes it mandatory and validated);
- `QualityAction` maps to canonical statuses exactly as specified (no new self-loop edge for `repeat_stage`);
- path normalization rejects traversal and destinations outside their configured root;
- exact delta helpers distinguish pre-existing implementation changes from later auxiliary edits;
- existing implementation recovery, fallback, checks, review, summary, and publishing tests remain unchanged and green.

## Definition of done

- `implementation` is the single explicit built-in workflow profile and remains the default.
- Implementation tasks retain current behavior in the foundation-only release.
- Every active task has a persisted, immutable, secret-free resolved profile identity (a row in `state.db` with its `profile_fingerprint`).
- Provider-neutral `run_kind`, session scope, output policy, and quality-action contracts are available without provider CLI details entering Core.
- The four follow-on backlog tasks reference and consume these contracts instead of defining parallel foundations.
- No feature-specific behavior listed under non-goals is accidentally enabled.
- The `implementation` profile requires supervisor from the start; there is no transitional no-supervisor profile, no versioned cutover, and no dual-profile execution support.
- `ruff`, `mypy`, and `pytest` pass.
- The canonical plan, architecture/configuration docs, and backlog/follow-ups are updated in the same implementation change.
