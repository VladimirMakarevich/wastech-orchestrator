# Backlog

This folder is the single place for backlog and deferred-product ideas for
**wastech-orchestrator**.

The canonical v1 contract is still [../orchestrator_final_plan.md](../orchestrator_final_plan.md).
Backlog documents must not override the hard invariants from [../../CLAUDE.md](../../CLAUDE.md),
[../../AGENTS.md](../../AGENTS.md), or [../rules/](../rules/).

## Index

| Document | Purpose |
|---|---|
| [product_backlog.md](product_backlog.md) | Aggregated product backlog collected from the spec, architecture notes, implementation-stage notes, and scattered v2/candidate lists. |
| [follow_ups.md](follow_ups.md) | Implementation follow-ups / tech-debt discovered while building (e.g. the schema migration runner) — distinct from product features. Recorded via `/sync-docs`. |
| [automatic_check_discovery.md](automatic_check_discovery.md) | Accepted detailed task for automatic repository check discovery, environment resolution, agent-assisted fallback, and persisted check profiles. |
| [prompt_template_customization.md](prompt_template_customization.md) | Detailed backlog task for user-overridable agent prompt templates. |
| [token_optimization.md](token_optimization.md) | Detailed backlog task for measuring and reducing token usage. |
| [runtime_provider_capacity_gate.md](runtime_provider_capacity_gate.md) | Detailed backlog task for checking Codex and Claude capacity before autonomous `watch` admits a pending task. |
| [task_workflow_profiles.md](task_workflow_profiles.md) | Detailed backlog task for implementation, deep-research, and private security-audit workflows with distinct permissions, outputs, and publishing rules. |

## Rules

- Keep detailed analysis in a dedicated backlog file when the topic needs design context.
- Keep the short product inventory in [product_backlog.md](product_backlog.md).
- When a deferred feature is mentioned in another document, link back here instead of creating a new
  isolated backlog list.
- Mark status explicitly (`backlog / not scheduled`, `candidate`, `accepted`, or `done`) before
  implementation starts.
