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
| [prompt_template_customization.md](prompt_template_customization.md) | Detailed backlog task for user-overridable agent prompt templates. |
| [token_optimization.md](token_optimization.md) | Detailed backlog task for measuring and reducing token usage. |

## Rules

- Keep detailed analysis in a dedicated backlog file when the topic needs design context.
- Keep the short product inventory in [product_backlog.md](product_backlog.md).
- When a deferred feature is mentioned in another document, link back here instead of creating a new
  isolated backlog list.
- Mark status explicitly (`backlog / not scheduled`, `candidate`, `accepted`, or `done`) before
  implementation starts.
