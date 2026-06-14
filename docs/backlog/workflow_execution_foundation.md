# Workflow execution foundation

Status: **accepted — prerequisite, not scheduled**
Date: 2026-06-14
Owner: Vladimir Makarevich

## Goal

Introduce the smallest provider-neutral workflow and execution contracts needed by these four
backlog tasks:

1. [Task workflow profiles](task_workflow_profiles.md);
2. [Durable sessions and implementation/fixing affinity](durable_sessions_and_fixing_affinity.md);
3. [Hybrid agent testing](hybrid_agent_testing.md);
4. [Supervisor quality-gate](supervisor_quality_gate.md).

The foundation-only release must preserve the current `implementation` pipeline byte-for-byte at
the behavioral boundary before any follow-on feature lands. It prepares stable extension points;
it does not pre-implement the four feature tasks.

## Why this is a separate prerequisite

All four tasks need the Core to resolve and persist several concepts that currently exist only as
implicit stage-specific behavior:

- which workflow owns the task;
- which execution role is running;
- which stage/session/output policy applies;
- which policy snapshot recovery must reuse;
- which component may write to the repository, artifacts, or control workspace;
- which deterministic action follows a quality result.

Implementing those concepts independently in each feature would create incompatible enums, schema
columns, request builders, artifact layouts, and recovery rules. A single prerequisite is
therefore justified, but its scope must remain narrow enough that it does not become a speculative
rewrite of the orchestrator.

## Architectural decision

Do **not** build an arbitrary user-defined workflow graph engine in this task.

The initial foundation uses:

```text
task file
  -> structural validation
  -> task_type resolution
  -> built-in WorkflowProfileRegistry
  -> persisted ResolvedWorkflowProfile
  -> WorkflowRunner selected by runner_kind
  -> existing implementation pipeline
```

The first transitional profile version is `implementation-v1`. Its `implementation` runner
delegates to the current
orchestrator pipeline and retains the canonical stages, state transitions, checks, Git lifecycle,
fallback rules, and recovery behavior.

Later profile work may add dedicated runners or a common ordered-stage engine after the
`deep_research` and `security_audit` requirements prove which abstraction is actually needed.
The mandatory supervisor change upgrades the same runner through a one-way profile cutover to
`implementation-v2`; it does not keep `implementation-v1` as a selectable runtime path.

This incremental design avoids two unsafe extremes:

- forcing research/audit stages into the current implementation `Stage` enum;
- replacing the working implementation state machine with a generic graph before another profile
  exists.

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
  result_policy
```

Initial entry:

```text
task_type: implementation
profile_version: implementation-v1
runner_kind: implementation
supervisor_policy: transitional_absent
```

Profiles are code/configuration owned by the operator and application. Task prose cannot define,
replace, or mutate a profile.

`transitional_absent` exists only so the foundation can land without changing current runtime
behavior. The supervisor implementation replaces it with `required` in `implementation-v2` and
removes executable support for the transitional policy.

This delivers Phase 1 of
[task workflow profiles](task_workflow_profiles.md#17-rollout-plan). That feature document retains
ownership of `deep_research`, `security_audit`, and later profile extensions.

### 2. Resolved profile snapshot

Resolve one immutable, secret-free profile snapshot after task validation and before branch
creation or provider execution:

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
  result_policy
  policy_fingerprint
```

Persist the canonical snapshot or an immutable artifact plus its fingerprint. Recovery,
`rerun --continue`, and all later stage decisions must reuse the persisted snapshot rather than
silently re-resolving changed configuration midway through a task.

The snapshot must contain no secrets, raw session handles, full environment values, or sensitive
security findings.

On restart:

- load and verify the persisted snapshot fingerprint;
- reject an unknown runner/profile version;
- re-check current hard security capabilities without widening the saved policy;
- stop in `manual_action_required` when the saved workflow can no longer be executed safely.

A fresh rerun creates a new resolved snapshot. A continue rerun preserves the previous one.

### 3. Provider-neutral execution descriptor

Introduce a normalized descriptor used when Core prepares an agent or deterministic execution:

```text
ResolvedExecutionPolicy
  workflow_stage_id
  execution_role
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

The descriptor is an orchestration contract, not provider argv. Provider-specific command syntax
remains exclusively in `providers/`.

#### Stage identity

Keep the current canonical `Stage` enum for `implementation_v1`. Add a separate validated
workflow-stage identifier at the workflow boundary so future built-in profiles are not forced to
pretend that `repository_analysis` or `threat_analysis` is an implementation stage.

For the initial profile:

```text
workflow_stage_id = Stage.value
```

Do not accept arbitrary task-provided stage IDs.

#### Execution role

Execution role identifies why a run exists without creating fake pipeline stages:

```text
pipeline_stage
supervisor
testing_agent
```

The foundation introduces the extensible contract and audit field. It does not invoke supervisor
or testing agents. The implementation runner must nevertheless be able to consume a resolved
`supervisor_policy`; the follow-on cutover makes that policy unconditionally `required`.

The same workflow checkpoint may use a different execution role. In `implementation-v2`, the
supervisor executes `workflow_stage_id = summary` with `execution_role = supervisor` when summary
output is enabled, replacing the old summary provider call while preserving the Core-owned
`summarizing` lifecycle checkpoint. When summary is skipped, the checkpoint records a skip and
creates no execution or output artifact.

#### Session scope

Use an explicit provider-neutral session scope:

```text
fresh_disposable
editing_lineage
```

`lineage_key` is optional and is meaningful only for a resumable scope. The foundation records the
policy but does not implement vendor resume or durable lineage; that remains owned by
[durable sessions](durable_sessions_and_fixing_affinity.md).

Current implementation behavior must not accidentally become durable merely because the contract
exists.

### 4. Output and artifact policy

Define a reusable resolved output policy instead of scattering stage-specific path assumptions:

```text
ResolvedOutputPolicy
  target_repository_writes
  control_workspace_writes
  artifact_class
  allowed_path_policy
  publishing_policy
```

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

Only `implementation_scoped`, `normal_artifacts`, and `pull_request` are active in the foundation.
The remaining identifiers reserve compatible vocabulary; their enforcement is implemented by the
owning feature.

Provide common deterministic primitives for:

- path normalization and containment;
- before/after workspace snapshots;
- exact delta calculation;
- artifact destination resolution;
- immutable run artifact allocation.

Do not weaken or replace the existing scoped-staging, dangerous-diff, secret-redaction, or
Check Runner controls. The implementation profile maps to those existing controls.

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

The deterministic Core remains the only component that applies an action. Agents, profiles, and
providers may return validated facts or verdicts but cannot transition state directly.

The descriptor may reference a local budget key and the existing task-wide fix budget. This task
does not add supervisor rework counters or change current fixing semantics; it only prevents the
supervisor feature from inventing a separate incompatible action/budget model.

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
  execution_role
  execution_unit
  resolved_policy_fingerprint
```

Use the next available config/DB schema versions at implementation time. Do not reserve a numeric
version in this document.

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
- no broad renaming of existing statuses or stages.

## Delivery slices

The prerequisite should be implemented as four reviewable slices, not one large refactor.

### Slice F1: task type and profile selection

- parse `task_type`, defaulting to `implementation`;
- validate known/enabled profiles before side effects;
- add the built-in registry and transitional `implementation-v1`;
- persist task type, profile version, and fingerprint;
- add migration and compatibility tests.

Exit condition: old tasks run exactly as before through the explicitly selected implementation
profile.

### Slice F2: resolved execution policy

- add `ResolvedWorkflowProfile` and `ResolvedExecutionPolicy`;
- centralize precedence and ceiling validation;
- map current implementation stages/routes/permissions into the descriptor;
- persist and recover the immutable snapshot;
- fail closed on fingerprint/version mismatch.

Exit condition: every current stage can explain its resolved workflow policy without changing its
runtime behavior.

### Slice F3: shared execution and output primitives

- add execution-role and session-scope vocabulary;
- add provider-neutral artifact destination and immutable allocation helpers;
- add reusable snapshot/delta/path-containment primitives;
- add common audit metadata;
- keep all feature-specific components and tables absent.

Exit condition: each follow-on feature can consume common contracts without adding a parallel
request/audit/output-policy framework.

### Slice F4: compatibility gate

- run parity tests for the existing implementation pipeline;
- verify restart, continue rerun, fresh rerun, fallback, checks, review, and publishing behavior;
- document extension points and ownership boundaries;
- update the canonical plan and public architecture/configuration docs in the implementation
  change.

Exit condition: no follow-on feature is installed yet and observable implementation behavior is
unchanged.

## Dependency map

| Follow-on task | Foundation contract it consumes | Feature-owned work |
|---|---|---|
| [Task workflow profiles](task_workflow_profiles.md) | Registry, profile snapshot/version, workflow stage IDs, output/publishing policy, runner selection. | `deep_research`, `security_audit`, their runners/stages, result schemas, network policy, private storage, and publishing behavior. |
| [Durable sessions](durable_sessions_and_fixing_affinity.md) | Execution role, session scope, lineage key, execution unit, persisted policy identity. | Vendor resume, raw handle storage/redaction, lineage transaction, affinity, provider-aware retry/fallback. |
| [Hybrid agent testing](hybrid_agent_testing.md) | `testing_agent` role, fresh session scope, immutable run artifacts, exact delta/path primitives, output-policy hook. | Testing checkpoint, trusted test-path policy, optional invocation, partial-edit handling, Check Runner integration. |
| [Supervisor quality-gate](supervisor_quality_gate.md) | `supervisor` role, fresh session scope, immutable run artifacts, quality-action vocabulary, budget key, role/stage separation, persisted `supervisor_policy`, and optional output-artifact vocabulary. | Required verdict schema, evaluation/application persistence, local rework counters, stage integration, final handoff schema, Core-owned optional summary materialisation/skip behavior, read-only mutation guard, and the one-way `implementation-v2` cutover. |

## Recommended implementation order

```text
workflow execution foundation (F1 -> F4)
  -> durable sessions and fixing affinity
  -> supervisor quality-gate
  -> hybrid agent testing
  -> task workflow profiles: deep_research
  -> task workflow profiles: security_audit
```

Durable sessions may land before the required-supervisor cutover. Supervisor should then land
before hybrid testing so subsequent implementation-workflow work targets only
`implementation-v2`. `deep_research` and `security_audit` should follow because they benefit from
the same execution-role, fresh-session, output-policy, and evaluator contracts.

### One-way supervisor cutover

The foundation and supervisor are separate delivery tasks, but the resulting product must not
carry two implementation modes indefinitely:

```text
foundation release: implementation-v1 is the sole executable profile
supervisor release: require an empty non-terminal queue, migrate config,
                    make implementation-v2 the sole executable profile
```

Historical completed `implementation-v1` records remain readable. No config/task selector and no
runtime runner may start or resume `implementation-v1` after the cutover. This avoids duplicating
execution, recovery, summary, and test behavior.

The cutover must also define terminal-task recovery explicitly:

- `rerun --continue` rejects every historical `implementation-v1` attempt after the cutover;
- a fresh rerun resolves and persists `implementation-v2`;
- the upgrade refuses every non-terminal or unresolved `manual_action_required`
  `implementation-v1` task, because it could otherwise require the removed runtime policy;
- completed, failed, abandoned, or explicitly finalized v1 attempts remain immutable history.

## Risks and required decisions

### Configuration drift during recovery

Re-resolving a profile from current configuration after restart can silently change route,
permissions, output policy, or publishing behavior. Recovery must therefore use the persisted
resolved snapshot and fail closed when it cannot be honored.

### Premature generic state machine

The current and proposed workflows have different terminal behavior and side effects. A generic
graph engine before two real workflow implementations exist would encode assumptions that are
expensive to undo. Keep one `implementation` runner behind a versioned policy boundary.

### Stage enum pollution

Supervisor and testing agent are execution roles, not pipeline stages. Research/audit stage IDs are
workflow-local built-in identifiers. Do not add all of them to the current implementation `Stage`
enum merely to reuse routing or prompt maps.

### Security policy placeholders

Reserved output-policy identifiers are not enforcement. A profile cannot be enabled until every
declared permission, output, result, and publishing policy has a deterministic implementation and
tests.

### Schema ownership

The foundation owns common profile/run identity only. Adding empty feature-specific tables would
couple migrations and make independent implementation harder.

## Minimum tests

- missing `task_type` selects `implementation-v1`;
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
- supervisor/testing roles do not become `Stage` values;
- `fresh_disposable` rejects a lineage key and `editing_lineage` requires a validated lineage key;
- path normalization rejects traversal and destinations outside their configured root;
- exact delta helpers distinguish pre-existing implementation changes from later auxiliary edits;
- existing implementation recovery, fallback, checks, review, summary, and publishing tests remain
  unchanged and green.

The supervisor follow-on must additionally test that the cutover rejects non-terminal and
unresolved manual-action v1 tasks; rejects `rerun --continue` for historical v1 attempts; resolves
fresh reruns to `implementation-v2`; and exposes no runtime no-supervisor policy.

## Definition of done

- `implementation` is an explicit built-in workflow profile and remains the default.
- Existing implementation tasks retain current behavior in the foundation-only release.
- Every active task has a persisted, immutable, secret-free resolved profile identity.
- Provider-neutral execution role, session scope, output policy, and quality-action contracts are
  available without provider CLI details entering Core.
- The four follow-on backlog tasks reference and consume these contracts instead of defining
  parallel foundations.
- No feature-specific behavior listed under non-goals is accidentally enabled.
- The foundation leaves an explicit one-way path to mandatory `implementation-v2`; it does not
  require permanent dual-profile execution support.
- `ruff`, `mypy`, and `pytest` pass.
- The canonical plan, architecture/configuration docs, backlog/follow-ups, and `CHANGELOG.md` are
  updated in the same implementation change.
