# Backlog

This folder is the single place for backlog and deferred-product ideas for
**wastech-orchestrator**.

The canonical contract is
[00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md).
Backlog documents must not override the hard invariants from [../../CLAUDE.md](../../CLAUDE.md),
[../../AGENTS.md](../../AGENTS.md), or [../rules/](../rules/).

## Index

| Document | Purpose |
|---|---|
| [product_backlog.md](product_backlog.md) | Aggregated product backlog collected from the spec, architecture notes, implementation-stage notes, and scattered v2/candidate lists. |
| [follow_ups.md](follow_ups.md) | Implementation follow-ups / tech-debt discovered while building (e.g. the schema migration runner) — distinct from product features. Recorded via `/sync-docs`. |
| [prompt_template_customization.md](prompt_template_customization.md) | Detailed backlog task for user-overridable agent prompt templates. |
| [token_optimization.md](token_optimization.md) | Detailed backlog task for measuring and reducing token usage. |
| [runtime_provider_capacity_gate.md](runtime_provider_capacity_gate.md) | Detailed backlog task for checking Codex and Claude capacity before autonomous `watch` admits a pending task. |
| [workflow_execution_foundation.md](workflow_execution_foundation.md) | Shared prerequisite for built-in workflow selection, immutable resolved-profile identity, execution roles/session scopes, and reusable output/audit contracts. |
| [task_workflow_profiles.md](task_workflow_profiles.md) | Detailed backlog task for implementation, deep-research, and private security-audit workflows with distinct permissions, outputs, and publishing rules. |
| [durable_sessions_and_fixing_affinity.md](durable_sessions_and_fixing_affinity.md) | Durable Claude/Codex editing lineage, provider-aware resume/fallback, and implementation/fixing provider-session affinity. |
| [hybrid_agent_testing.md](hybrid_agent_testing.md) | Optional test-authoring agent before the authoritative deterministic Check Runner, with once-per-unit checkpoints and test-only diff guardrails. |
| [supervisor_quality_gate.md](supervisor_quality_gate.md) | Mandatory read-only AI quality evaluation that emits bounded verdicts into Core-owned rework loops and optionally generates the final handoff without becoming a controller. |
| [task_rerun_command.md](task_rerun_command.md) | Detailed backlog task for a `rerun <task-id>` command that launches a fresh attempt of a terminal task (gate allowance, branch reset-to-base to avoid merge conflicts, ledger attempt chain). |
| [task_finalize_command.md](task_finalize_command.md) | Detailed backlog task for a `finalize <task-id> --as <done\|failed\|abandoned>` command that reconciles the bookkeeping of an operator-handled task (status, working tree/branch, file move, HITL close-out, ledger) without running the pipeline. |
| [task_install_templates_command.md](task_install_templates_command.md) | Detailed backlog task for a separate `install-templates` command that delivers the packaged `templates/` tree into an existing install **add-missing-only** (skip existing, add only what's absent), resolving the install location like `upgrade-docs`; fills the gap that only `init` copies templates and `install` never does. |
| [task_prompt_templates_simplification.md](task_prompt_templates_simplification.md) | Detailed backlog task to simplify prompt templates: deliver **only** `prompts/` (drop the inert `skills/`/`AGENTS.md`/`CLAUDE.md`/`task.md`) and resolve prompts by convention — a `<stage>.md` in `templates_dir` auto-wins, packaged defaults become a per-stage fallback, `mode` defaults to `replace`, and `prompts.overrides` is removed. Also bundles (Part C) a maximal `task-rich.md` example exercising every task front-matter field (`pr_title`, `auto_merge`, all stages, per-stage model/reasoning/enabled) and actualizes the `worc/` README/best-practices/decision-guide. |

## Agent quality and continuity program

Workflow profiles and the management-requested supervisor, testing-agent, and session-continuity
changes are separate backlog items built on one narrow shared prerequisite:

| Work item | Owns | Must not own |
|---|---|---|
| [Workflow execution foundation](workflow_execution_foundation.md) | Transitional `implementation-v1` selection, immutable resolved-profile identity, execution role/session/output vocabulary, common audit and path/delta primitives. | New workflows, vendor resume, testing checkpoints, supervisor verdicts, or feature-specific policy tables. |
| [Task workflow profiles](task_workflow_profiles.md) | `deep_research` and `security_audit` runners/stages, result contracts, profile-specific output, network, storage, and publishing behavior. | Duplicate execution/session/output foundations or changes to provider CLI boundaries. |
| [Durable sessions and fixing affinity](durable_sessions_and_fixing_affinity.md) | Claude/Codex resume, editing lineage, provider-aware fallback, recovery/rerun semantics, session redaction, implementation/fixing affinity. | Supervisor verdicts, testing-agent edit policy, deterministic test outcomes. |
| [Hybrid agent testing](hybrid_agent_testing.md) | Optional testing-agent checkpoint, test-only diff delta, approved-profile context, integration before `CheckRunner`. | Check-command authority, editing lineage, supervisor rework policy. |
| [Supervisor quality-gate](supervisor_quality_gate.md) | Mandatory read-only verdict persistence/application, local/global rework budgets, one-way `implementation-v2` cutover, state-machine integration, and optional final handoff generation. | Provider routing, deterministic test authority, testing-agent edits, editing lineage, direct summary-file writes, or a runtime quality-gate disable mode. |

Shared contracts:

- the deterministic Core owns every transition;
- provider CLI syntax remains inside provider adapters;
- provider fallback remains infrastructure-only;
- artifacts remain the primary recovery/context path;
- recovery reuses the persisted resolved-profile snapshot instead of silently applying changed
  workflow policy;
- only `implementation`/`fixing` may use the active editing lineage;
- mandatory supervisor and optional testing-agent calls start fresh and cannot update editing
  lineage;
- implementation/fixing output cannot continue without a valid required supervisor verdict;
- supervisor and testing may reuse low-level invocation/audit plumbing but remain separate domain
  components and security policies;
- each independently implemented change uses the next available DB/config schema version rather
  than assuming another program item has not already migrated it.

Recommended order is foundation, sessions/affinity, supervisor, hybrid testing, deep research,
then security audit. After the foundation, sessions, testing, and supervisor are not a hard
dependency chain at the code-contract level, but the supervisor release performs the one-way
mandatory `implementation-v2` cutover before subsequent workflow expansion.

The target implementation loop is:

```text
implementation provider succeeds
  -> atomically persist/update editing lineage
  -> supervisor evaluates implementation in a fresh read-only session
  -> accept: deterministic dangerous-change guard
  -> optional testing agent runs once for the execution unit in a fresh write session
  -> deterministic CheckRunner
  -> review

fixing provider succeeds
  -> atomically update editing lineage
  -> supervisor evaluates fixing in a fresh read-only session
  -> accept: deterministic dangerous-change guard
  -> testing-agent checkpoint is reused (agent does not run again)
  -> deterministic CheckRunner

review succeeds
  -> summary enabled: final supervisor synthesizes the accepted outcome in a fresh read-only
     session; Core writes the structured handoff (or deterministic fallback)
  -> summary disabled: record skip; no supervisor call and no summary files/body
  -> publishing
```

Supervisor `rework` uses the already-persisted editing lineage so fixing can resume the provider
conversation that produced the rejected edit. A supervisor/testing call can never replace that
lineage. The supervisor-owned final pass replaces the separate `summary` provider call but not the
Core-owned `summarizing` checkpoint. When summary is enabled, invalid/unavailable handoff output
uses deterministic fallback. Supervisor has no enable/disable control; only the hybrid testing
phase remains optional. An explicit summary skip suppresses handoff generation, fallback, and all
summary files/body, but never the mandatory implementation/fixing quality gates. The foundation
should also reserve compatible auxiliary audit roles
(`supervisor`, `testing_agent`) in the
[workflow execution foundation](workflow_execution_foundation.md), even if only one feature lands
first.

## Rules

- Keep detailed analysis in a dedicated backlog file when the topic needs design context.
- Keep the short product inventory in [product_backlog.md](product_backlog.md).
- When a deferred feature is mentioned in another document, link back here instead of creating a new
  isolated backlog list.
- Mark status explicitly (`backlog / not scheduled`, `candidate`, `accepted`, or `done`) before
  implementation starts.
