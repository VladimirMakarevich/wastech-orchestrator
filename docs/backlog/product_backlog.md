# Product Backlog

Status: **backlog / not scheduled**
Date: 2026-06-12
Owner: Vladimir Makarevich

This document aggregates deferred and candidate functionality that was previously scattered across
the repository. It is an inventory, not an implementation contract. The source of truth remains
[00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md).

## Sources Consolidated

- [00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md) sections 2 and 18.
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
| --- | --- | --- |
| [Per-stage model and reasoning overrides](per_stage_model_reasoning.md) | Extend the existing task-level `model`/`reasoning` fields with a `stages:` block that sets them independently per stage (`planning`, `review`, `fixing`, etc.), allowing a high-capability model for review/planning and a lighter one for fixing/summary. | **Per-task `stages:` overrides shipped (2026-06-13, §3).** Remaining: config-level `stage_defaults` (§4). See linked detail file. |
| [Stage skip control (per-task and global)](stage_skip_control.md) | Add a `skip:` list to task frontmatter and a global `agents.skip_stages` config key to bypass individual pipeline stages (`planning`, `testing`, `review`, `fixing`, `summary`). Useful for trivial tasks, repos without test suites, or high-trust automated flows. `implementation` and `publishing` cannot be skipped. | Every skipped stage is audited in `state.db` and logged as WARNING. Interacts with [[auto_merge_bypass]] — double-warning when review is skipped AND auto_merge is on. See linked detail file. |
| [Task workflow profiles](task_workflow_profiles.md) | Add explicit `implementation`, `deep_research`, and `security_audit` task types with different stage graphs, permissions, output contracts, quality gates, and publishing behavior. | Security audits are read-only against the target repo and store private reports beside the resolved `config.yaml`, outside the repository, without commit/push/PR. |
| Richer task parsing | Extract additional structured metadata beyond current `id`, `title`, `refined`, `decompose`, `agents`, `contacts`, `model`, and `reasoning`. | Candidate fields: repo binding, commands/hints, priority, labels, issue links. Must stay fail-closed. |
| Parallel and graph decomposition | Support graph-shaped subtasks, per-subtask worktrees/branches, and parallel execution. | V1 decomposition is linear, sequential, and uses one task branch. |
| Supervisor-style planning layer | Revisit an LLM supervisor/manager on top of the deterministic Core. | Must remain auditable; Core/provider boundaries stay intact. |

## Additional Candidate Features

These were described in architecture notes or v1 exclusions but are not scheduled.

| Item | Summary | Source / constraint |
| --- | --- | --- |
| PR template support | Generate PR bodies from a configurable template in addition to `summary.md`. | Should integrate with the existing summary stage and Git Manager. |
| GitHub Issues integration | Link tasks to issues, update issue status, and optionally close issues after PR creation/merge. | Requires GitHub auth through existing external credential model. |
| Concurrent task processing via worktrees | Process multiple independent tasks at once by assigning each task its own `git worktree`. | Must not share a mutable working copy between active agents. |
| Queue priorities | Prioritize pending tasks instead of processing purely by deterministic filename order. | Must preserve single-active-task invariant unless worktree concurrency is implemented. |
| Web UI for tasks and logs | Provide a browser UI for pending/running/done tasks, artifacts, logs, and stuck states. | Read-only first is safer; mutations need auth/audit. |
| Auto-retry on network errors | Retry transient network/provider failures before fallback or terminal failure. | Must be bounded and audited; should not retry quality failures. |
| Per-task budget limit | Allow task-level budget/time/token limits. | Must not let a task raise global security or cost ceilings. |
| [Runtime provider capacity gate](runtime_provider_capacity_gate.md) | Before autonomous `watch` claims a pending task, query the capacity reported by the Codex and Claude accounts required by its resolved routes and defer the task when configured headroom is unavailable. | Capacity checks are runtime admission control, not installation preflight or a completion guarantee. Deferred tasks remain pending, consume no attempts, and are retried automatically after provider reset. |
| Auto-close stale tasks | Detect stale pending/running/manual tasks and close, archive, or escalate them. | Needs explicit policy and audit trail. |
| Dry-run without push | Run through planning/check/review/publish preparation without pushing or opening a PR. | Must clearly mark that no publish operation occurred. |
| Lightweight project memory | Persist small project-level lessons or conventions between tasks. | Optional; must not store secrets or unbounded agent context. |
| [Auto-merge bypass flags (global and per-task)](auto_merge_bypass.md) | ⚠️ Opt-in flags — both off by default — to skip the manual PR-approval gate: a global `publishing.auto_merge` setting in `config.yaml`, and a per-task `auto_merge` field in task metadata. Full design, safety guardrails, audit trail, and implementation checklist in the linked detail file. | Security-sensitive; see [auto_merge_bypass.md](auto_merge_bypass.md). `extra_args` cannot set this flag. Merge-API failures fall back to `MANUAL_REVIEW`, never force-push. |
| [UX improvements: stop/restart, WAL gitignore, gh check, worc alias](ux_improvements.md) | Four small independent operator-UX items: `stop`/`restart` commands for the watch loop (PID-file based); suppress `state.db-shm`/`state.db-wal` from `git status` via `.git/info/exclude`; hard pre-flight `gh` check before publish stage; short `worc` alias in `pyproject.toml`. | All additive, no Core changes. See linked detail file for per-item effort estimate and implementation notes. |
| Dynamic agent selection by model | Let a model choose provider/stage routing dynamically. | Must not bypass configured allowlist, route audit, or deterministic fallback rules. |
| Provider SDK/API backends | Add OpenAI API / Claude Agent SDK backends in addition to CLIs. | Must remain behind `AgentProvider`; Core must not learn provider syntax. |
| Automatic CLI installation/authorization | Install or authorize Codex/Claude/GitHub CLIs automatically. | Current policy keeps credentials and auth outside the orchestrator. |
| Vendor session transfer | Transfer an active vendor session between Codex and Claude Code. | Currently not supported; artifacts, not sessions, are the source of truth. |
| Multi-repo/project binding | Select a configured repository per task (`repo` field / project map). | Current config targets one repository. Requires validation and workspace isolation. |
| Agent instruction stubs in target repo | Seed or update target-repo `AGENTS.md`, `CLAUDE.md`, and skills before a run. | Packaged templates exist; runtime seeding needs scoped staging/audit rules. |

## Detailed Backlog Files

| Item | Detail |
| --- | --- |
| Prompt template customization | [prompt_template_customization.md](prompt_template_customization.md) |
| Token optimization | [token_optimization.md](token_optimization.md) |
| Runtime provider capacity gate for autonomous watch mode | [runtime_provider_capacity_gate.md](runtime_provider_capacity_gate.md) |
| Task workflow profiles | [task_workflow_profiles.md](task_workflow_profiles.md) |
| Auto-merge bypass flags (global and per-task) | [auto_merge_bypass.md](auto_merge_bypass.md) |
| UX improvements (stop/restart, WAL gitignore, gh check, worc alias) | [ux_improvements.md](ux_improvements.md) |
| Per-stage model and reasoning overrides | [per_stage_model_reasoning.md](per_stage_model_reasoning.md) |
| Stage skip control (per-task and global) | [stage_skip_control.md](stage_skip_control.md) |

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
- state-store recovery and idempotent publishing;
- Telegram terminal notifications, typed HITL for `refinement`/`planning`, durable interaction
  recovery, and deletion/dependency diff approvals (see
  [Stage 08](../implementation_stages/08_telegram_integration.md));
- automatic check discovery and environment resolution (see
  [Stage 09](../implementation_stages/09_automatic_check_discovery.md)).
