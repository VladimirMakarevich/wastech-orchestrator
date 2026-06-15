# Backlog: Task workflow profiles

Status: **backlog / not scheduled** Date: 2026-06-13 Owner: Vladimir Makarevich

This document captures the product task of supporting several explicit task workflows with different stage graphs, permissions, output contracts, quality gates, and publishing behavior. It is a backlog item, not current runtime behavior. Nothing here overrides the canonical specification, [CLAUDE.md](../../CLAUDE.md), [AGENTS.md](../../AGENTS.md), or the hard invariants in [docs/rules/](../rules/).

## 0. Foundation dependency

This feature depends on the shared [workflow execution foundation](workflow_execution_foundation.md). That prerequisite owns `task_type` defaulting, the single `implementation` profile, immutable resolved-profile identity, workflow-stage IDs, the `run_kind` audit field, session scopes, and generic output/audit contracts.

This document owns the feature behavior above that foundation: `deep_research`, `security_audit`, their runners/stages, result schemas, network policy, output enforcement, private report storage, and publishing rules. It must extend the shared contracts rather than create a parallel profile or execution framework.

The [supervisor quality-gate](supervisor_quality_gate.md) lands immediately after the foundation and makes the single `implementation` profile require supervisor evaluation (`supervisor_policy: required`) from the start. There is no supervisor-disabled implementation mode and no profile-version cutover (greenfield MVP — no deployed state). New workflow types are added after that.

## 1. Background

The current orchestrator is designed around one primary outcome: implement a requested change, test and review it, then publish the result through Git.

Some valuable autonomous tasks require different outcomes:

- implementation should change code and tests;
- deep research should analyze the repository and external sources, then produce an actionable design document without implementing it;
- a security audit should inspect code, architecture, dependencies, and trust boundaries, then produce a sensitive findings report without modifying or publishing the target repository.

These are not merely different prompts. Each requires a different workflow, permission profile, definition of done, output schema, and publication policy.

## 2. Terminology

Use **task type** in task frontmatter and **workflow profile** for the orchestrator configuration that defines its execution.

Avoid the generic name `mode` because it can be confused with provider permission modes, sandbox modes, reasoning modes, auto mode, or CLI operating modes.

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

`implementation` should remain the default for backward compatibility when `task_type` is absent. Unknown task types must fail validation before branch creation or provider execution.

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

1. **Task type selection is deterministic.** The task explicitly declares its type. An agent must not silently select or change the workflow.
2. **Profiles are more than prompt variants.** Each profile has its own stage graph, output contract, permissions, and terminal behavior.
3. **Task metadata cannot escalate authority.** A task may request stricter limits, but cannot widen filesystem access, network access, or publishing permissions beyond the configured profile.
4. **Provider details stay in provider adapters.** Workflow selection does not permit Core to learn Codex or Claude CLI syntax.
5. **Every result is auditable.** The selected task type, profile version, routes, checks, result location, and terminal decision are persisted.
6. **Sensitive reports are not Git artifacts by default.** Security findings remain in the control workspace unless an operator deliberately exports them.
7. **A workflow succeeds only when its own output contract is satisfied.** A successful CLI exit without the required artifact is not a successful task.

## 5. Proposed profile contract

Conceptual configuration:

```yaml
workflows:
  default_task_type: implementation

  profiles:
    implementation:
      enabled: true
      supervisor_policy: required
      output_policy: code_change
      publishing: pull_request

    deep_research:
      enabled: true
      supervisor_policy: not_applicable # see §10 — research/audit use their own in-graph gates
      output_policy: repository_document
      publishing: documentation_pull_request

    security_audit:
      enabled: true
      supervisor_policy: not_applicable
      output_policy: private_control_workspace_report
      publishing: none
```

Conceptual normalized profile (this **extends** the foundation's `WorkflowProfile` / `ResolvedWorkflowProfile`; it does not redefine it):

```text
WorkflowProfile                     # foundation-owned base fields
  task_type
  profile_version
  runner_kind
  stages[]                          # workflow_stage_id list (foundation), not the implementation Stage enum
  permission_ceiling
  supervisor_policy
  output_policy                     # scalar identifier resolving to the foundation's ResolvedOutputPolicy
  publishing_policy                 # single source of truth (profile-level, per the foundation)
  + feature extensions:
      routes
      network_policy
      quality_gates[]
      result_storage
```

The `output_policy` scalar identifiers map onto the foundation's `ResolvedOutputPolicy`:

```text
code_change                      -> target_repository_writes: implementation_scoped, control_workspace_writes: normal_artifacts
repository_document              -> target_repository_writes: approved_document_only, control_workspace_writes: normal_artifacts
private_control_workspace_report -> target_repository_writes: none,                  control_workspace_writes: private_report
```

`publishing` maps to the foundation's `publishing_policy` (`pull_request` / `documentation_pull_request` / `none`); `local_artifact` (§13) is a feature extension of that vocabulary.

The profile registry belongs to the provider-neutral orchestration layer. Provider adapters receive ordinary `AgentRunRequest` values with the resolved stage, permissions, model, reasoning, and artifact paths.

## 6. Workflow: implementation

### Purpose

Implement a concrete change in the target repository.

### Suggested stages

```text
refinement
    -> planning
    -> implementation
    -> required supervisor evaluation
    -> testing
    -> review
    -> fixing
    -> required supervisor evaluation
    -> summary checkpoint (enabled: final supervisor handoff; disabled: audited no-output skip)
    -> publishing
```

The canonical statuses and deterministic Core transitions remain; the `implementation` profile adds required supervisor evaluations after successful `implementation`/`fixing` (supervisor is mandatory from the start — see [supervisor quality-gate](supervisor_quality_gate.md)).

Unless summary output is explicitly skipped, the `summary` lifecycle checkpoint is fulfilled by the final fresh supervisor pass: it synthesizes the complete accepted outcome and returns the structured handoff. The Core validates and writes summary artifacts. No separate summary provider call exists, and deterministic fallback remains non-blocking.

When summary output is skipped, the Core records the checkpoint as skipped and creates no `summary.md`, `summary.json`, task-sidecar summary, deterministic fallback, or PR summary body. Publishing and the task audit commit must support the absent optional artifact.

### Allowed outputs

- source-code changes;
- tests;
- required documentation;
- changelog entries where applicable;
- optional task summary when its output policy is enabled;
- orchestrator-managed commits, push, and Pull Request.

### Definition of done

- requirements are implemented;
- expected tests and checks pass;
- no blocking review or supervisor findings remain;
- output paths comply with security and scoped-staging policy;
- documentation is synchronized where behavior, configuration, CLI, or architecture changed;
- publishing is performed idempotently by the orchestrator.

## 7. Workflow: deep research

### Purpose

Analyze a proposed feature, integration, architecture decision, or technical problem using the existing repository as primary context and current authoritative external sources where needed.

The result is an implementation-ready research document, not code.

### Suggested stages

```text
refinement
    -> repository_analysis
    -> external_research
    -> architecture_design
    -> synthesis
    -> fact_verification      # role=verifier: deterministic citation-checker + agent fact-verifier
         -> rework: repeat_stage(synthesis)   # citation loop pinned to max 1 in v1
    -> critical_review        # role=critic: accept | rework
         -> accept: publishing
         -> rework: repeat_stage(synthesis)   # bounded critical-review loop (configurable budget)
    -> publishing
```

`external_research` may be skipped when the task is fully repository-local. The decision must be deterministic from task/profile settings, not silently made by the provider.

#### Bounded critical-review loop (ping-pong)

`critical_review` is an instance of the foundation's read-only evaluator primitive (`run_kind = evaluator`, `role = critic`); it reuses the shared immutable evaluation artifacts and `QualityAction` vocabulary, but not the implementation supervisor's fixing-edge semantics. Because it holds a multi-round dialogue across revisions, its session policy is `resume_own_lineage` — it resumes **its own** session between rounds (so it remembers what it already flagged) while staying independent of the author's session. It returns `accept` or `rework`:

- `accept` -> continue to `publishing`;
- `rework` -> the deterministic Core applies `repeat_stage(synthesis)`, re-running `synthesis` from the persisted reviewer feedback. The Core owns the transition; the reviewer never transitions directly.

The reviewer should run on an **independent provider/route** from the author stages (`architecture_design` / `synthesis`) so the loop is genuinely adversarial (see §11 and the §18 open question on independent providers).

The loop is **bounded** by a research revision budget — a local per-`(role, execution_unit)` limit plus the shared global cap — derived by counting applied `rework` evaluations, exactly like the supervisor's local limit. This budget is **per-evaluator and operator-configurable** (the `critic` may get a different number than other evaluators, by task complexity/type); it is **not** the citation-checker's v1 pin of 1 (see Verification below). On exhaustion the workflow does **not** `fail`: it publishes the document with the residual disagreements recorded in its **Open questions** section. This is the deliberate difference from `implementation`, where unresolved blocking findings stop the task.

#### Verification (`fact_verification`, role = verifier)

`fact_verification` checks that the report's claims are actually backed, in **two layers** (mirroring hybrid testing: a deterministic gate that is authoritative, plus an agent layer that judges):

**Layer 1 — deterministic citation-checker (authoritative, no LLM, no tokens).** It validates the machine-readable evidence manifest (`sources.json` + repo-evidence entries with path/line/snippet), not the prose:

- cited repository paths exist, line ranges are in range, quoted snippets match file content (normalized for whitespace/indentation);
- `sources.json` schema + date sanity (required fields present, valid format, no future dates);
- internal-link integrity within the research directory.

A _confirmed-broken_ citation is **blocking** for that claim (objective broken evidence). Robustness contract — the checker must not become a brittle gate:

1. validate the **structured manifest**, never parse citations out of free-form prose;
2. **per-citation isolation** — iterate the manifest, each entry a pure side-effect-free read (file exists / line in range / snippet match / JSON field); no subprocess, shell, or network, so one bad entry can never abort the run;
3. three outcomes — `verified` / `broken` (confirmed-invalid → blocking for that claim only) / `uncheckable` (the checker itself could not evaluate the entry → **soft `unverified`, never a task crash**).

**Layer 2 — agent fact-verifier (judgment, `role = verifier`).** Does the source actually support the claim; is it current and authoritative; semantic fact-check. Its findings feed `repeat_stage` into `synthesis` like the critic.

**Budgets.** The Layer-1 citation-driven rework loop is **pinned to a maximum of 1 iteration in v1** (cost guard while the logic is unproven; expandable after field testing). This pin applies **only** to the citation loop — Layer 2 and the critic keep their own independently configurable budgets.

**Exhaustion.** A claim whose citation cannot be made valid within budget is **removed or explicitly labeled `unverified`/inference** — never shipped as fact. Network URL reachability is out of scope for v1 (flaky, side-effecting, proves existence not content); it belongs to agent `external_research`. Layer 1 depends on the structured directory output so it has a machine-readable input.

### Permissions

- target repository source files: read-only;
- configured research output **directory**: write-only, bounded to that directory root;
- network: allowed only according to the configured research policy;
- Git lifecycle: the provider still cannot commit, push, or create a PR;
- writes anywhere outside the approved research directory: forbidden.

### Output

The result is a **directory**, not a single file (symmetric with `security_audit`'s `security-reports/<task-id>/`):

```text
docs/research/<task-id>/
  report.md            # required entry document — the output contract is not satisfied without it
  architecture.md      # optional supporting files (curated by synthesis)
  options.md
  sources.json         # machine-readable citations
  diagrams/            # optional assets
```

Path policy: the foundation's containment primitives bound every write to the directory root; any file inside is allowed, nothing outside. The **output contract requires the entry document** (`report.md`) to exist — a directory that is empty or missing the entry doc is not a successful task (§4 principle 7).

Only the **curated result** is committed to the documentation PR. The raw per-stage trail (each stage's intermediate output, reviewer feedback, rework history) stays as **control-workspace working artifacts** (auditable, never committed); `synthesis` explicitly promotes the files that belong in the published directory.

Suggested entry-document structure:

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

Claims about current vendor APIs, security advisories, pricing, limits, or software behavior must include dated authoritative sources. Inferences must be labeled as such.

### Review

The separate critical-review evaluator (see the bounded loop above) verifies:

- consistency with the current codebase and architecture rules;
- unsupported or outdated claims;
- missing failure modes;
- hidden security or migration costs;
- whether the recommendation is implementable and testable;
- whether alternatives were dismissed with sufficient evidence.

A `rework` verdict must carry bounded, structured feedback that `synthesis` consumes on the next pass. Each evaluation is an immutable artifact namespaced by its source run; recovery reuses it so a restart cannot double-count a revision or lose the loop position.

### Definition of done

- the required entry document (`report.md`) exists in the approved research directory, and every written file is contained within that directory;
- it cites repository evidence and external sources where relevant, and every cited reference passes the deterministic citation-checker (or the claim is labeled `unverified`/removed — never shipped as a fact with a broken citation);
- it includes an actionable recommendation and implementation plan;
- critical-review and verification findings are resolved, or the revision budget is exhausted and the residual findings are recorded in the Open questions section (the loops never run unbounded);
- no source-code files were modified, and only the curated result was committed.

### Publishing

The default is a documentation-only Pull Request created by the orchestrator. A future configuration may allow local-only research artifacts, but the task cannot enable Git publishing when the profile disables it.

## 8. Workflow: security audit

### Purpose

Evaluate the security of the repository, a proposed solution, or a defined subsystem and produce a structured, evidence-based report with prioritized remediation guidance.

The audit is separate from remediation. It must not change application code by default. Fixes should be created as linked `implementation` tasks referencing finding IDs.

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

Automated scanner output is evidence, not the final severity decision. The audit should evaluate reachability and repository-specific impact where practical.

### Permissions

- target repository: read-only;
- dependency scanners: read-only unless an explicitly approved isolated environment is required;
- network: allowed only for current advisory data under the configured policy;
- target repository report paths: no write access;
- commit, push, PR creation, and issue creation: disabled;
- security report directory in the control workspace: write access.

### Private report location

The security report must be stored outside the target repository in the **control workspace**, the directory containing the resolved `config.yaml`.

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

It must not be derived from the general `artifacts_root`, because with the default in-repo footprint the artifact root is the target repository while `config.yaml` remains in the sibling control workspace.

Required storage rules:

- `security-reports/` must resolve outside `repo.local_path`;
- path traversal through `task_id` or task metadata is rejected;
- reports are never included in code commits, task audit commits, PR bodies, or Git staging;
- the orchestrator does not automatically copy reports into `tasks/` or `logs/` inside the repo;
- file permissions should be restrictive where the platform supports them;
- raw secrets, credentials, full environment dumps, and unnecessary exploit payloads are redacted;
- report retention and deletion policy should be configurable;
- terminal notifications contain only a safe summary and local report path, not sensitive findings.

If the resolved `config.yaml` is located inside `repo.local_path`, `security_audit` must fail closed or require an explicitly configured external report root. It must never silently write a sensitive report into the target repository.

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

A high-severity claim with low confidence should remain clearly distinguishable from a confirmed finding.

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

- `implementation`: code diff, checks, review result, optional summary reference, PR reference;
- `deep_research`: report metadata, source list, recommendation, open questions;
- `security_audit`: private report path, severity counts, scan metadata, safe summary.

The State Store should persist only safe metadata for security audits. Detailed findings and evidence remain in the private control-workspace report directory.

## 10. Stage and state-machine design

Do not force all profiles into the existing implementation stages with renamed prompts. Each profile is selected by the foundation's `runner_kind` and its stages are validated `workflow_stage_id` values (e.g. `repository_analysis`, `threat_analysis`) — **not** entries in the implementation `Stage` enum. The foundation deliberately deferred any generic ordered-stage engine until these profiles prove the abstraction; two reasonable designs are:

1. a profile-owned ordered stage list using a common stage execution engine;
2. separate workflow state machines implementing a shared lifecycle contract.

### Quality gates and supervisor applicability

The mandatory `implementation` supervisor evaluates a **code diff** and routes `rework -> fixing`. `deep_research` and `security_audit` perform **no code edits** (their outputs are a document and a private report), so that exact mechanism does not apply. Their quality is enforced by their own in-graph evaluators — `fact_verification` (`role = verifier`) and `critical_review` (`role = critic`) for research, `finding_verification` (`role = verifier`) for audit. These are further instances of the foundation's shared **evaluator-loop primitive** (`run_kind = evaluator`, own session, `QualityAction`/bounded rework, immutable verdicts) — the same primitive the implementation `supervisor` and the optional `test_quality` evaluator use — but with `repeat_stage` back into their own graph instead of the implementation fixing edge. So the implementation supervisor _component_ does not curate them; the shared evaluator _primitive_ does.

Recommended: `supervisor_policy: not_applicable` for `deep_research` and `security_audit`. This is a **provisional decision recorded here and bound when each profile's phase (2/3) is designed in detail** — the exact stages, verification semantics, and revision loops above must be finalized then. See [open questions](#18-open-questions).

The first design is likely simpler if stages remain linear, but each stage still needs explicit:

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

Workflow-specific stage state must also be persisted so restart recovery resumes from the correct checkpoint without repeating scans, research, writes, commits, or notifications.

## 11. Provider routing and model selection

Each workflow may define different routing defaults:

- `implementation`: current Claude/Codex stage routes;
- `deep_research`: model/provider selected for source synthesis and long-context repository analysis, with an independent critical reviewer;
- `security_audit`: security-capable analysis route plus independent verification where available.

The configured provider allowlist remains authoritative. Task overrides may select only permitted providers and cannot weaken permission, output, or publishing policy.

Per-stage model and reasoning controls should integrate with the already-implemented [per-stage model and reasoning overrides](../implementation_stages/13_per_stage_model_reasoning.md) and the foundation's `ResolvedExecutionPolicy` (`model` / `reasoning`), not create a second incompatible mechanism.

## 12. Output guardrails

Each profile must have an allowlisted output surface:

| Task type | Target repository writes | Control-workspace writes | Git publishing |
| --- | --- | --- | --- |
| `implementation` | Scoped code/docs/tests | Normal artifacts | Pull Request |
| `deep_research` | Approved research directory only | Normal artifacts | Documentation Pull Request |
| `security_audit` | None | Private security report only | None |

After every provider stage, the orchestrator should compare the working tree and private artifact locations against the profile's allowed outputs.

Unexpected writes are policy failures, not quality findings. For read-only workflows, the orchestrator should preserve evidence, stop safely, and never publish unintended changes.

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

For `security_audit`, publishing is always `none` in the initial version. Exporting or disclosing a security report is a separate operator-controlled action and must not be enabled by task content.

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
- Security reports may themselves contain sensitive repository details and require redaction, restricted storage, and retention controls.
- External research must treat web content as untrusted data and resist prompt injection.
- Scanner commands use validated argv lists and mandatory timeouts.
- Vulnerability databases and advisories are current external data; source and retrieval time must be recorded.
- Security audit evidence should be minimized: store what proves a finding, not broad copies of secrets or private source.
- Private report paths and safe summaries may be stored in SQLite, but detailed findings should not be duplicated there.

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
- deep research writes only inside the approved research directory and produces the required entry document (`report.md`);
- deep research critical review rejects an incomplete or unsupported report and the Core re-runs `synthesis` from persisted feedback;
- the critical-review loop is bounded: on revision-budget exhaustion the report publishes with residual findings in Open questions, never unbounded and never `fail`;
- the deterministic citation-checker flags a hallucinated repo reference (missing path / out-of-range line / snippet mismatch) as `broken`, and a malformed manifest entry as `uncheckable` (soft `unverified`) without crashing the run;
- the citation-driven rework loop runs at most once in v1, and an unfixable claim is removed or labeled `unverified` rather than shipped as fact;
- only the curated result is committed; the raw per-stage trail stays in control-workspace artifacts;
- security audit runs fake scanners and writes only to the control workspace;
- security audit leaves the target repository byte-for-byte and Git-status clean;
- security report paths never enter scoped staging, audit commits, push, or PR creation;
- provider infrastructure errors use existing fallback rules;
- quality/output-contract failures do not incorrectly trigger provider fallback;
- restart resumes the correct workflow stage without duplicate artifacts.

### End-to-end tests

- one queue processes an implementation task, a deep-research task, and a security-audit task sequentially;
- each task produces its own type-specific result and terminal metadata;
- implementation creates one PR;
- deep research creates one documentation-only PR;
- security audit creates no branch, commit, push, or PR and stores its report beside `config.yaml` under `security-reports/<task-id>/`;
- a malicious task cannot redirect the security report into the repository;
- failed security scans produce a private partial report and safe operator notification.

## 17. Rollout plan

### Phase 1: shared foundation and implementation profile

To be implemented by the prerequisite [workflow execution foundation](workflow_execution_foundation.md):

- add `task_type` with `implementation` default;
- introduce the built-in profile registry (`profile_version` as a forward audit attribute, no version cutover);
- persist the immutable resolved-profile snapshot;
- the foundation slices are plumbing only and do not change pipeline behavior.

### Phase 1b: mandatory supervisor

Implemented by [supervisor quality-gate](supervisor_quality_gate.md), immediately after the foundation (greenfield MVP — no deployed state, no migration/cutover):

- the single `implementation` profile requires supervisor from the start (`supervisor_policy: required`); no no-supervisor profile ever ships;
- require supervisor evaluation after implementation/fixing;
- replace the summary provider run with the final supervisor handoff when summary output is enabled;
- retain no runtime selector for a no-supervisor implementation profile.

### Phase 2: deep research

- add read-only repository analysis and controlled external research;
- add the research output **directory** + entry-document contract, output guardrails (directory containment), the bounded critical-review loop (`repeat_stage(synthesis)`, independent reviewer, revision budget), and documentation-only publishing of the curated result.

### Phase 3: security audit

- add scope, scanners, threat analysis, verification, and structured findings;
- add external private report storage derived from `config.yaml`;
- enforce no repository writes and no publishing.

### Phase 4: workflow extensions

- add operator-configurable profile defaults;
- integrate per-stage model/reasoning settings;
- add linked follow-up task generation;
- evaluate additional types such as dependency upgrade assessment, performance audit, migration design, or documentation maintenance.

## 18. Open questions

- Should `deep_research` always publish a documentation PR, or support a control-workspace-only report mode?
- Should a security audit be allowed to create sanitized implementation tasks automatically, or only recommend them in its report?
- What retention and deletion defaults should apply to private security reports?
- Should report directories be encrypted at rest, or is restrictive filesystem access sufficient for the first version?
- How should critical findings notify operators without leaking details through Telegram or logs?
- Which workflow stages require independent providers rather than the same provider in a new run?
- Should workflow profiles be built-in only initially, or allow user-defined profiles after the contracts stabilize?
- Confirm `supervisor_policy: not_applicable` for `deep_research`/`security_audit` (§10): are their in-graph `critical_review` / `finding_verification` gates sufficient, and do they reuse the foundation evaluator plumbing? Bind this when each profile's phase is designed in detail.
- The `fact_verification` two-layer design (deterministic citation-checker + agent fact-verifier) is **accepted and specified in §7 (Verification)**, including its robustness contract and the v1 citation-loop pin of 1. Remaining tuning detail (not blocking): exact snippet-match strictness/normalization thresholds.

## 19. Acceptance criteria

- Tasks support explicit `implementation`, `deep_research`, and `security_audit` types.
- Missing `task_type` selects the single required-supervisor `implementation` profile.
- The executable implementation profile requires supervisor evaluation and has no disable control.
- `stages.summary.enabled: false` skips only final handoff/output: no supervisor handoff call, summary files, fallback summary, task sidecar, or PR summary body is created.
- Each type has a distinct persisted stage graph, permission ceiling, output contract, quality gates, and publishing policy.
- Deep research produces an actionable research directory (with a required entry document) without modifying source code.
- Deep research verifies its claims: the deterministic citation-checker rejects broken repository references (blocking) and degrades unprocessable entries to `unverified` without crashing; no claim ships as fact with a broken or missing citation.
- Security audit produces structured, evidence-based findings without modifying the target repository.
- Security reports are stored under `security-reports/<task-id>/` in the directory containing the resolved `config.yaml`.
- Security reports and evidence are never committed, pushed, or included in a Pull Request.
- Task content cannot redirect outputs, escalate permissions, or enable publishing.
- Recovery resumes every workflow idempotently from its persisted checkpoint.
- Existing provider abstraction, infrastructure-only fallback, secret handling, and orchestrator-only Git ownership remain intact.
