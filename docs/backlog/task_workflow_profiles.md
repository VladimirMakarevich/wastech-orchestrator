# Backlog: Task workflow profiles

Status: **backlog / not scheduled**
Date: 2026-06-13
Owner: Vladimir Makarevich

This document captures the product task of supporting several explicit task workflows with
different stage graphs, permissions, output contracts, quality gates, and publishing behavior. It
is a backlog item, not current runtime behavior. Nothing here overrides the canonical
specification, [CLAUDE.md](../../CLAUDE.md), [AGENTS.md](../../AGENTS.md), or the hard invariants in
[docs/rules/](../rules/).

## 1. Background

The current orchestrator is designed around one primary outcome: implement a requested change,
test and review it, then publish the result through Git.

Some valuable autonomous tasks require different outcomes:

- implementation should change code and tests;
- deep research should analyze the repository and external sources, then produce an actionable
  design document without implementing it;
- a security audit should inspect code, architecture, dependencies, and trust boundaries, then
  produce a sensitive findings report without modifying or publishing the target repository.

These are not merely different prompts. Each requires a different workflow, permission profile,
definition of done, output schema, and publication policy.

## 2. Terminology

Use **task type** in task frontmatter and **workflow profile** for the orchestrator configuration
that defines its execution.

Avoid the generic name `mode` because it can be confused with provider permission modes, sandbox
modes, reasoning modes, auto mode, or CLI operating modes.

Initial task types:

```text
implementation
deep_research
security_audit
```

Example:

```yaml
---
id: task-123
title: Research runtime provider capacity checks
task_type: deep_research
---
```

`implementation` should remain the default for backward compatibility when `task_type` is absent.
Unknown task types must fail validation before branch creation or provider execution.

## 3. Goal

Allow one orchestrator installation to process several deterministic workflow profiles:

```text
task file
    -> validate task_type
    -> select configured workflow profile
    -> enforce its permissions and allowed outputs
    -> execute its stage graph
    -> validate its type-specific result
    -> store or publish the result according to its policy
```

The task type must determine:

1. the stages that run and their ordering;
2. provider routing, model, and reasoning defaults;
3. the maximum permissions available to each stage;
4. allowed output paths and artifact formats;
5. checks and review gates;
6. the definition of success;
7. whether Git publishing is allowed;
8. where the final result is stored.

## 4. Design principles

1. **Task type selection is deterministic.**
   The task explicitly declares its type. An agent must not silently select or change the workflow.
2. **Profiles are more than prompt variants.**
   Each profile has its own stage graph, output contract, permissions, and terminal behavior.
3. **Task metadata cannot escalate authority.**
   A task may request stricter limits, but cannot widen filesystem access, network access, or
   publishing permissions beyond the configured profile.
4. **Provider details stay in provider adapters.**
   Workflow selection does not permit Core to learn Codex or Claude CLI syntax.
5. **Every result is auditable.**
   The selected task type, profile version, routes, checks, result location, and terminal decision
   are persisted.
6. **Sensitive reports are not Git artifacts by default.**
   Security findings remain in the control workspace unless an operator deliberately exports them.
7. **A workflow succeeds only when its own output contract is satisfied.**
   A successful CLI exit without the required artifact is not a successful task.

## 5. Proposed profile contract

Conceptual configuration:

```yaml
workflows:
  default_task_type: implementation

  profiles:
    implementation:
      enabled: true
      output_policy: code_change
      publishing: pull_request

    deep_research:
      enabled: true
      output_policy: repository_document
      publishing: documentation_pull_request

    security_audit:
      enabled: true
      output_policy: private_control_workspace_report
      publishing: none
```

Conceptual normalized profile:

```text
WorkflowProfile
  task_type
  profile_version
  stages[]
  routes
  permission_ceiling
  network_policy
  output_contract
  quality_gates[]
  publishing_policy
  result_storage
```

The profile registry belongs to the provider-neutral orchestration layer. Provider adapters receive
ordinary `AgentRunRequest` values with the resolved stage, permissions, model, reasoning, and
artifact paths.

## 6. Workflow: implementation

### Purpose

Implement a concrete change in the target repository.

### Suggested stages

```text
refinement
    -> planning
    -> implementation
    -> testing
    -> review
    -> fixing
    -> summary
    -> publishing
```

This is the current workflow and should retain its existing behavior and invariants.

### Allowed outputs

- source-code changes;
- tests;
- required documentation;
- changelog entries where applicable;
- task summary;
- orchestrator-managed commits, push, and Pull Request.

### Definition of done

- requirements are implemented;
- expected tests and checks pass;
- no blocking review findings remain;
- output paths comply with security and scoped-staging policy;
- documentation is synchronized where behavior, configuration, CLI, or architecture changed;
- publishing is performed idempotently by the orchestrator.

## 7. Workflow: deep research

### Purpose

Analyze a proposed feature, integration, architecture decision, or technical problem using the
existing repository as primary context and current authoritative external sources where needed.

The result is an implementation-ready research document, not code.

### Suggested stages

```text
refinement
    -> repository_analysis
    -> external_research
    -> architecture_design
    -> critical_review
    -> synthesis
    -> publishing
```

`external_research` may be skipped when the task is fully repository-local. The decision must be
deterministic from task/profile settings, not silently made by the provider.

### Permissions

- target repository source files: read-only;
- configured research output path: write-only for the final document and bounded working
  artifacts;
- network: allowed only according to the configured research policy;
- Git lifecycle: the provider still cannot commit, push, or create a PR;
- code changes outside the approved report path: forbidden.

### Output

Recommended default path:

```text
docs/research/<task-id>.md
```

Suggested report structure:

```text
Executive summary
Background
Current repository behavior
Problem statement
Requirements
Constraints and invariants
Repository evidence
External capabilities and sources
Options considered
Trade-offs
Recommended architecture
Data model and configuration
Failure and recovery scenarios
Security implications
Testing strategy
Migration and rollout
Implementation phases
Acceptance criteria
Open questions
References
```

Claims about current vendor APIs, security advisories, pricing, limits, or software behavior must
include dated authoritative sources. Inferences must be labeled as such.

### Review

A separate critical-review stage should verify:

- consistency with the current codebase and architecture rules;
- unsupported or outdated claims;
- missing failure modes;
- hidden security or migration costs;
- whether the recommendation is implementable and testable;
- whether alternatives were dismissed with sufficient evidence.

### Definition of done

- the required Markdown artifact exists at the approved path;
- it cites repository evidence and external sources where relevant;
- it includes an actionable recommendation and implementation plan;
- critical-review findings are resolved or recorded as open questions;
- no source-code files were modified.

### Publishing

The default is a documentation-only Pull Request created by the orchestrator. A future
configuration may allow local-only research artifacts, but the task cannot enable Git publishing
when the profile disables it.

## 8. Workflow: security audit

### Purpose

Evaluate the security of the repository, a proposed solution, or a defined subsystem and produce a
structured, evidence-based report with prioritized remediation guidance.

The audit is separate from remediation. It must not change application code by default.
Fixes should be created as linked `implementation` tasks referencing finding IDs.

### Suggested stages

```text
scope
    -> repository_analysis
    -> dependency_scan
    -> threat_analysis
    -> finding_verification
    -> report
    -> private_storage
```

There is no normal Git `publishing` stage.

### Audit scope

Depending on the repository and task, inspect:

- trust boundaries and threat model;
- authentication and authorization;
- secret handling and credential exposure;
- command, argument, path, template, SQL, and code injection;
- subprocess construction and shell usage;
- sandbox and filesystem boundaries;
- unsafe deserialization and parser behavior;
- SSRF, XSS, CSRF, request forgery, and relevant web risks;
- network egress and external integrations;
- dependency and supply-chain vulnerabilities;
- dangerous defaults and configuration downgrade paths;
- logging of sensitive information;
- privilege escalation;
- recovery, idempotency, and race conditions with security impact;
- publishing and Git permissions;
- CI/CD and artifact integrity where included in task scope.

### Dependency checks

Use ecosystem-appropriate deterministic tools where available, for example:

```text
pip-audit
osv-scanner
npm audit
pnpm audit
yarn npm audit
cargo audit
govulncheck
```

An agent must not invent package vulnerabilities. Dependency findings must identify:

- package and installed/resolved version;
- advisory ID such as CVE, GHSA, OSV, or vendor ID;
- affected version range;
- fixed version when known;
- vulnerability database/source;
- scan timestamp;
- whether the dependency is direct, transitive, runtime, development-only, or unreachable.

Automated scanner output is evidence, not the final severity decision. The audit should evaluate
reachability and repository-specific impact where practical.

### Permissions

- target repository: read-only;
- dependency scanners: read-only unless an explicitly approved isolated environment is required;
- network: allowed only for current advisory data under the configured policy;
- target repository report paths: no write access;
- commit, push, PR creation, and issue creation: disabled;
- security report directory in the control workspace: write access.

### Private report location

The security report must be stored outside the target repository in the **control workspace**, the
directory containing the resolved `config.yaml`.

Recommended layout:

```text
<control-workspace>/
  config.yaml
  security-reports/
    <task-id>/
      report.md
      findings.json
      scan-summary.json
      evidence/
```

The location must be derived from the resolved configuration file path:

```text
control_workspace = parent(resolve(config_path))
report_root = control_workspace / "security-reports" / task_id
```

It must not be derived from the general `artifacts_root`, because with the default in-repo
footprint the artifact root is the target repository while `config.yaml` remains in the sibling
control workspace.

Required storage rules:

- `security-reports/` must resolve outside `repo.local_path`;
- path traversal through `task_id` or task metadata is rejected;
- reports are never included in code commits, task audit commits, PR bodies, or Git staging;
- the orchestrator does not automatically copy reports into `tasks/` or `logs/` inside the repo;
- file permissions should be restrictive where the platform supports them;
- raw secrets, credentials, full environment dumps, and unnecessary exploit payloads are redacted;
- report retention and deletion policy should be configurable;
- terminal notifications contain only a safe summary and local report path, not sensitive findings.

If the resolved `config.yaml` is located inside `repo.local_path`, `security_audit` must fail closed
or require an explicitly configured external report root. It must never silently write a sensitive
report into the target repository.

### Finding format

Each finding should have a stable ID and structured fields:

```text
id
title
severity
confidence
status
category
cwe
advisories[]
affected_components[]
affected_files[]
evidence
attack_preconditions
attack_scenario
impact
verification
recommended_remediation
recommended_tests
references[]
```

Suggested severities:

```text
critical
high
medium
low
informational
```

Suggested confidence:

```text
confirmed
high
medium
low
```

A high-severity claim with low confidence should remain clearly distinguishable from a confirmed
finding.

### Report structure

```text
Executive summary
Scope and exclusions
Repository and revision audited
Methodology and tools
Threat model
Findings by severity
Dependency findings
Positive security controls
Systemic risks
Recommended remediation plan
Verification and regression-test plan
Residual risks
Appendix: tool versions and source timestamps
```

### Definition of done

- the private report exists in the control workspace;
- structured findings validate against the security-audit schema;
- every material finding has evidence, confidence, impact, and remediation;
- dependency findings reference current authoritative advisory data;
- false positives and unverified hypotheses are labeled;
- the target repository has no modifications;
- no security report or sensitive evidence was staged, committed, pushed, or included in a PR.

## 9. Result contract

Introduce a provider-neutral workflow outcome:

```text
TaskOutcome
  task_id
  task_type
  profile_version
  status
  primary_artifact
  structured_result
  checks
  findings_summary
  sources
  follow_up_tasks
  publishing_result
```

Type-specific structured results:

- `implementation`: code diff, checks, review result, summary, PR reference;
- `deep_research`: report metadata, source list, recommendation, open questions;
- `security_audit`: private report path, severity counts, scan metadata, safe summary.

The State Store should persist only safe metadata for security audits. Detailed findings and
evidence remain in the private control-workspace report directory.

## 10. Stage and state-machine design

Do not force all profiles into the existing implementation stages with renamed prompts. Two
reasonable designs are:

1. a profile-owned ordered stage list using a common stage execution engine;
2. separate workflow state machines implementing a shared lifecycle contract.

The first is likely simpler if stages remain linear, but each stage still needs explicit:

- permissions;
- provider route;
- inputs;
- output schema;
- retry/fallback policy;
- quality gate;
- resumability behavior.

Common lifecycle states may remain:

```text
pending
validated
active
completed
failed
manual_action_required
```

Workflow-specific stage state must also be persisted so restart recovery resumes from the correct
checkpoint without repeating scans, research, writes, commits, or notifications.

## 11. Provider routing and model selection

Each workflow may define different routing defaults:

- `implementation`: current Claude/Codex stage routes;
- `deep_research`: model/provider selected for source synthesis and long-context repository
  analysis, with an independent critical reviewer;
- `security_audit`: security-capable analysis route plus independent verification where available.

The configured provider allowlist remains authoritative. Task overrides may select only permitted
providers and cannot weaken permission, output, or publishing policy.

Per-stage model and reasoning controls should integrate with
[per-stage model and reasoning overrides](per_stage_model_reasoning.md), not create a second
incompatible mechanism.

## 12. Output guardrails

Each profile must have an allowlisted output surface:

| Task type | Target repository writes | Control-workspace writes | Git publishing |
| --- | --- | --- | --- |
| `implementation` | Scoped code/docs/tests | Normal artifacts | Pull Request |
| `deep_research` | Approved research document only | Normal artifacts | Documentation Pull Request |
| `security_audit` | None | Private security report only | None |

After every provider stage, the orchestrator should compare the working tree and private artifact
locations against the profile's allowed outputs.

Unexpected writes are policy failures, not quality findings. For read-only workflows, the
orchestrator should preserve evidence, stop safely, and never publish unintended changes.

## 13. Publishing policies

Publishing must be explicit per workflow:

```text
pull_request
documentation_pull_request
local_artifact
private_control_workspace_report
none
```

Only the orchestrator may commit, push, or create a PR.

For `security_audit`, publishing is always `none` in the initial version. Exporting or disclosing a
security report is a separate operator-controlled action and must not be enabled by task content.

## 14. Validation

Task validation should reject:

- unknown or disabled `task_type`;
- task-level attempts to modify workflow stages or publishing policy;
- output paths outside the profile allowlist;
- `security_audit` when no external control-workspace report path can be proven;
- workflow/provider overrides outside `agents.allowed`;
- permissions broader than the workflow ceiling;
- a task type incompatible with configured Git or network policy.

The normalized task should record the resolved profile and profile version for recovery and audit.

## 15. Security considerations

- Workflow selection must not be inferred from untrusted task prose.
- Prompts cannot override the profile's filesystem, network, or publishing policy.
- Security reports may themselves contain sensitive repository details and require redaction,
  restricted storage, and retention controls.
- External research must treat web content as untrusted data and resist prompt injection.
- Scanner commands use validated argv lists and mandatory timeouts.
- Vulnerability databases and advisories are current external data; source and retrieval time must
  be recorded.
- Security audit evidence should be minimized: store what proves a finding, not broad copies of
  secrets or private source.
- Private report paths and safe summaries may be stored in SQLite, but detailed findings should not
  be duplicated there.

## 16. Testing requirements

### Unit tests

- missing `task_type` resolves to `implementation`;
- known profiles resolve deterministically;
- unknown/disabled profiles fail before side effects;
- task metadata cannot widen permissions or publishing;
- profile stage graph and route resolution;
- type-specific output schema validation;
- output-path guardrails for every profile;
- control workspace is derived from the resolved `config.yaml` parent;
- `security-reports/` must resolve outside `repo.local_path`;
- task ID normalization prevents report path traversal;
- security finding redaction and safe SQLite metadata;
- profile version persists for recovery.

### Integration tests

- implementation follows the existing code pipeline;
- deep research creates only the approved Markdown document;
- deep research critical review rejects an incomplete or unsupported report;
- security audit runs fake scanners and writes only to the control workspace;
- security audit leaves the target repository byte-for-byte and Git-status clean;
- security report paths never enter scoped staging, audit commits, push, or PR creation;
- provider infrastructure errors use existing fallback rules;
- quality/output-contract failures do not incorrectly trigger provider fallback;
- restart resumes the correct workflow stage without duplicate artifacts.

### End-to-end tests

- one queue processes an implementation task, a deep-research task, and a security-audit task
  sequentially;
- each task produces its own type-specific result and terminal metadata;
- implementation creates one PR;
- deep research creates one documentation-only PR;
- security audit creates no branch, commit, push, or PR and stores its report beside `config.yaml`
  under `security-reports/<task-id>/`;
- a malicious task cannot redirect the security report into the repository;
- failed security scans produce a private partial report and safe operator notification.

## 17. Rollout plan

### Phase 1: profile contract and implementation compatibility

- add `task_type` with `implementation` default;
- introduce profile registry and versioning;
- run the existing pipeline through the implementation profile without behavior changes.

### Phase 2: deep research

- add read-only repository analysis and controlled external research;
- add report schema, output guardrails, critical review, and documentation-only publishing.

### Phase 3: security audit

- add scope, scanners, threat analysis, verification, and structured findings;
- add external private report storage derived from `config.yaml`;
- enforce no repository writes and no publishing.

### Phase 4: workflow extensions

- add operator-configurable profile defaults;
- integrate per-stage model/reasoning settings;
- add linked follow-up task generation;
- evaluate additional types such as dependency upgrade assessment, performance audit, migration
  design, or documentation maintenance.

## 18. Open questions

- Should `deep_research` always publish a documentation PR, or support a control-workspace-only
  report mode?
- Should a security audit be allowed to create sanitized implementation tasks automatically, or
  only recommend them in its report?
- What retention and deletion defaults should apply to private security reports?
- Should report directories be encrypted at rest, or is restrictive filesystem access sufficient
  for the first version?
- How should critical findings notify operators without leaking details through Telegram or logs?
- Which workflow stages require independent providers rather than the same provider in a new run?
- Should workflow profiles be built-in only initially, or allow user-defined profiles after the
  contracts stabilize?

## 19. Acceptance criteria

- Tasks support explicit `implementation`, `deep_research`, and `security_audit` types.
- Missing `task_type` preserves current implementation behavior.
- Each type has a distinct persisted stage graph, permission ceiling, output contract, quality
  gates, and publishing policy.
- Deep research produces an actionable Markdown design document without modifying source code.
- Security audit produces structured, evidence-based findings without modifying the target
  repository.
- Security reports are stored under `security-reports/<task-id>/` in the directory containing the
  resolved `config.yaml`.
- Security reports and evidence are never committed, pushed, or included in a Pull Request.
- Task content cannot redirect outputs, escalate permissions, or enable publishing.
- Recovery resumes every workflow idempotently from its persisted checkpoint.
- Existing provider abstraction, infrastructure-only fallback, secret handling, and
  orchestrator-only Git ownership remain intact.
