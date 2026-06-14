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
| [task_workflow_profiles.md](task_workflow_profiles.md) | Detailed backlog task for implementation, deep-research, and private security-audit workflows with distinct permissions, outputs, and publishing rules. |
| [task_rerun_command.md](task_rerun_command.md) | Detailed backlog task for a `rerun <task-id>` command that launches a fresh attempt of a terminal task (gate allowance, branch reset-to-base to avoid merge conflicts, ledger attempt chain). |
| [task_finalize_command.md](task_finalize_command.md) | Detailed backlog task for a `finalize <task-id> --as <done\|failed\|abandoned>` command that reconciles the bookkeeping of an operator-handled task (status, working tree/branch, file move, HITL close-out, ledger) without running the pipeline. |
| [task_install_templates_command.md](task_install_templates_command.md) | Detailed backlog task for a separate `install-templates` command that delivers the packaged `templates/` tree into an existing install **add-missing-only** (skip existing, add only what's absent), resolving the install location like `upgrade-docs`; fills the gap that only `init` copies templates and `install` never does. |

## Rules

- Keep detailed analysis in a dedicated backlog file when the topic needs design context.
- Keep the short product inventory in [product_backlog.md](product_backlog.md).
- When a deferred feature is mentioned in another document, link back here instead of creating a new
  isolated backlog list.
- Mark status explicitly (`backlog / not scheduled`, `candidate`, `accepted`, or `done`) before
  implementation starts.
