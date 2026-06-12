# Product Backlog

Status: **backlog / not scheduled**
Date: 2026-06-12
Owner: Vladimir Makarevich

This document aggregates deferred and candidate functionality that was previously scattered across the repository. It is an inventory, not an implementation contract. The source of truth for v1 behavior remains [../orchestrator_final_plan.md](../orchestrator_final_plan.md).

## Sources Consolidated

- [../orchestrator_final_plan.md](../orchestrator_final_plan.md) sections 2 and 18.
- [../codex_git_orchestrator_architecture.md](../codex_git_orchestrator_architecture.md) sections
  4.7, 4.10, 4.11, 6, 11, and 12.
- [../implementation_stages/05_pipeline_and_recovery.md](../implementation_stages/05_pipeline_and_recovery.md)
  "Not in this phase".
- [../implementation_stages/06_security_and_observability.md](../implementation_stages/06_security_and_observability.md)
  "Not in this phase".
- Detailed backlog files in this folder.

## Canonical V2 Backlog

These are explicitly deferred by the v1 spec.

| Item | Summary | Notes |
|---|---|---|
| Human-in-the-loop stage wiring | Let agent stage outputs invoke the implemented `ask_human` primitive for clarifying questions and dangerous-action approvals. | The Telegram transport, timeout handling, and terminal notifications are implemented; typed stage signals and answer reinjection remain. Must not weaken security policy. |
| Per-task reasoning and complexity levels | Add `reasoning` / `complexity` task fields that map to provider model flags, timeouts, and fix budgets. | Current v1 uses global provider model and global limits. |
| Richer task parsing | Extract additional structured metadata beyond current `id`, `title`, `refined`, `decompose`, `agents`, and `contacts`. | Candidate fields: repo binding, commands/hints, priority, labels, issue links. Must stay fail-closed. |
| Parallel and graph decomposition | Support graph-shaped subtasks, per-subtask worktrees/branches, and parallel execution. | V1 decomposition is linear, sequential, and uses one task branch. |
| Supervisor-style planning layer | Revisit an LLM supervisor/manager on top of the deterministic Core. | Must remain auditable; Core/provider boundaries stay intact. |

## Additional Candidate Features

These were described in architecture notes or v1 exclusions but are not scheduled.

| Item | Summary | Source / constraint |
|---|---|---|
| [Automatic check discovery and environment resolution](automatic_check_discovery.md) | Detect repository quality gates, resolve the correct project environment, validate/probe candidates, and persist a reusable check profile; use a read-only agent only when deterministic evidence is insufficient. | **Accepted / not scheduled.** The Check Runner remains the deterministic authority; launch failures do not consume fixing budget; setup/bootstrap stays separately controlled. |
| PR template support | Generate PR bodies from a configurable template in addition to `summary.md`. | Should integrate with the existing summary stage and Git Manager. |
| GitHub Issues integration | Link tasks to issues, update issue status, and optionally close issues after PR creation/merge. | Requires GitHub auth through existing external credential model. |
| Concurrent task processing via worktrees | Process multiple independent tasks at once by assigning each task its own `git worktree`. | Must not share a mutable working copy between active agents. |
| Queue priorities | Prioritize pending tasks instead of processing purely by deterministic filename order. | Must preserve single-active-task invariant unless worktree concurrency is implemented. |
| Web UI for tasks and logs | Provide a browser UI for pending/running/done tasks, artifacts, logs, and stuck states. | Read-only first is safer; mutations need auth/audit. |
| Auto-retry on network errors | Retry transient network/provider failures before fallback or terminal failure. | Must be bounded and audited; should not retry quality failures. |
| Per-task budget limit | Allow task-level budget/time/token limits. | Must not let a task raise global security or cost ceilings. |
| Auto-close stale tasks | Detect stale pending/running/manual tasks and close, archive, or escalate them. | Needs explicit policy and audit trail. |
| Dry-run without push | Run through planning/check/review/publish preparation without pushing or opening a PR. | Must clearly mark that no publish operation occurred. |
| Lightweight project memory | Persist small project-level lessons or conventions between tasks. | Optional; must not store secrets or unbounded agent context. |
| Automatic PR merge | Merge successful PRs automatically. | Explicitly out of v1; CI/PR should remain the control layer. |
| Dynamic agent selection by model | Let a model choose provider/stage routing dynamically. | Must not bypass configured allowlist, route audit, or deterministic fallback rules. |
| Provider SDK/API backends | Add OpenAI API / Claude Agent SDK backends in addition to CLIs. | Must remain behind `AgentProvider`; Core must not learn provider syntax. |
| Automatic CLI installation/authorization | Install or authorize Codex/Claude/GitHub CLIs automatically. | Current policy keeps credentials and auth outside the orchestrator. |
| Vendor session transfer | Transfer an active vendor session between Codex and Claude Code. | Currently not supported; artifacts, not sessions, are the source of truth. |
| Multi-repo/project binding | Select a configured repository per task (`repo` field / project map). | Current config targets one repository. Requires validation and workspace isolation. |
| Agent instruction stubs in target repo | Seed or update target-repo `AGENTS.md`, `CLAUDE.md`, and skills before a run. | Packaged templates exist; runtime seeding needs scoped staging/audit rules. |

## Detailed Backlog Files

| Item | Detail |
|---|---|
| Automatic check discovery and environment resolution | [automatic_check_discovery.md](automatic_check_discovery.md) |
| Prompt template customization | [prompt_template_customization.md](prompt_template_customization.md) |
| Token optimization | [token_optimization.md](token_optimization.md) |

## Not Backlog

The following are already part of the v1 spec or current implementation plan and should not be
tracked here as future work unless their scope changes:

- provider abstraction through `AgentProvider`;
- infrastructure-only fallback;
- deterministic stage pipeline;
- validation gate before branch/provider runs;
- bounded fix loops and global fix-iteration budget;
- optional v1 decomposition as a sequential planning sub-phase;
- final summary stage and PR body handoff;
- git footprint modes and scoped staging;
- terminal cleanup and auto mode;
- state-store recovery and idempotent publishing.
