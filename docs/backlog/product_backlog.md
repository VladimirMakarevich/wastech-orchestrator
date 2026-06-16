# Product Backlog

Status: **inventory** Date: 2026-06-16 Owner: Vladimir Makarevich

This document aggregates deferred and candidate functionality for **wastech-orchestrator**. It is an inventory, not an implementation contract. The source of truth remains the code; the [Functional Map](../functional/index.md) is the code-derived reference.

Where to find the design detail:

- **Open items** keep their detailed design in a file in this folder (linked below).
- **Shipped items** are recorded in [CHANGELOG.md](../../CHANGELOG.md) (their numbered design docs have been removed). They are listed in [§ Shipped](#shipped-implemented) for traceability.
- **Build-time tech-debt and implementation follow-ups** live in [follow_ups.md](follow_ups.md), not here.

## Sources Consolidated

- The [Functional Map](../functional/index.md) (code-derived reference) and [../worc_architecture.md](../worc_architecture.md) sections 4.7, 4.10, 4.11, 6, 11, and 12.
- The original v1 spec (§2, §18) and the phase docs' "Not in this phase" sections — since removed from `docs/`.
- Detailed backlog files in this folder.

## Open backlog

### Agent quality and continuity program

Five backlog items built on one narrow shared prerequisite. See [README.md § Agent quality and continuity program](README.md#agent-quality-and-continuity-program) for the full ownership/dependency map, shared contracts, recommended order, and the target implementation loop.

| Item | Summary | Status |
| --- | --- | --- |
| [Workflow execution foundation](workflow_execution_foundation.md) | Shared prerequisite: single `implementation` profile selection, immutable resolved-profile identity, execution roles/session scopes, the reusable evaluator-loop primitive, and common output/audit/path/delta contracts. Must preserve current behavior and must not pre-implement the features built on it. | accepted (prerequisite, not scheduled) |
| [Supervisor quality-gate](supervisor_quality_gate.md) | Mandatory read-only, fresh-session LLM evaluator that emits bounded `accept`/`rework` verdicts into Core-owned rework loops and, when summary output is enabled, returns the final structured handoff. No enable/disable mode; does not replace `review`; Core owns all transitions. | accepted (not scheduled) |
| [Durable sessions and implementation/fixing affinity](durable_sessions_and_fixing_affinity.md) | Persist per-unit Claude/Codex editing lineage; make `fixing` prefer the provider/session of the successful `implementation` run; provider-aware resume/fallback. Artifacts stay authoritative; evaluator sessions cannot contaminate the lineage. | accepted (not scheduled) |
| [Hybrid agent testing](hybrid_agent_testing.md) | Optional read-only test-quality evaluator (`role = test_quality`) before the authoritative deterministic Check Runner, with once-per-unit checkpoints and a test-only diff guard. Tests are authored by the `implementation` agent, never by the evaluator. | accepted (not scheduled) |
| [Task workflow profiles](task_workflow_profiles.md) | Explicit `implementation`, `deep_research`, and `security_audit` task types with distinct stage graphs, permissions, output contracts, quality gates, and publishing behavior. Security audits are read-only and store private reports outside the repo (no commit/push/PR). | backlog / not scheduled |
| [Documentation update stage](documentation_update_stage.md) | Optional, default-on finalizing `implementation` stage: a dedicated doc agent (own prompt/model/reasoning) updates the target repo's docs from the accepted-outcome context, doc-path-guarded, before summary/publishing. Slots in after the supervisor change. | accepted (not scheduled) |

### Other deferred features

These are deferred by the v1 spec or described in architecture notes; not scheduled.

| Item | Summary | Source / constraint |
| --- | --- | --- |
| [Runtime provider capacity gate](runtime_provider_capacity_gate.md) | Before autonomous `watch` claims a pending task, query the capacity of the Codex/Claude accounts its resolved routes need and defer the task when configured headroom is unavailable. | Runtime admission control, not install preflight. Deferred tasks stay pending, consume no attempts, retried after provider reset. |
| [Token optimization](token_optimization.md) | Measure and reduce token consumption across stages. | Analysis + candidate levers; not part of v1 scope. |
| Config-level per-stage `stage_defaults` | The §4 complement to the shipped per-task `stages:` overrides: per-stage `{model,reasoning}` defaults under each provider in `config.yaml`. | [CHANGELOG.md](../../CHANGELOG.md) (per-task `stages:` overrides, §3); tracked in [follow_ups.md](follow_ups.md). Touches schema (+version bump), loader, validation. |
| Richer task parsing | Extract structured metadata beyond `id`, `title`, `refined`, `decompose`, `agents`, `contacts`, `model`, `reasoning`, `stages`, `pr_title`, `auto_merge`. | Candidate fields: repo binding, commands/hints, priority, labels, issue links. Must stay fail-closed. |
| Parallel and graph decomposition | Graph-shaped subtasks, per-subtask worktrees/branches, parallel execution. | V1 decomposition is linear, sequential, on one task branch. |
| Concurrent task processing via worktrees | Process multiple independent tasks at once by giving each its own `git worktree`. | Must not share a mutable working copy between active agents. Breaks the single-active-task invariant only behind worktree isolation. |
| Queue priorities | Prioritize pending tasks instead of deterministic filename order. | Must preserve single-active-task invariant unless worktree concurrency lands. |
| PR template support | Generate PR bodies from a configurable template in addition to `summary.md`. | Should integrate with the existing summary stage and Git Manager. |
| GitHub Issues integration | Link tasks to issues, update status, optionally close on PR creation/merge. | Requires GitHub auth through the existing external credential model. |
| Web UI for tasks and logs | Browser UI for pending/running/done tasks, artifacts, logs, stuck states. | Read-only first; mutations need auth/audit. |
| Auto-retry on network errors | Retry transient network/provider failures before fallback or terminal failure. | Must be bounded and audited; never retry quality failures. |
| Per-task budget limit | Task-level budget/time/token limits. | Must not let a task raise global security or cost ceilings. |
| Auto-close stale tasks | Detect and close/archive/escalate stale pending/running/manual tasks. | Needs explicit policy and audit trail. |
| Dry-run without push | Run planning/check/review/publish prep without pushing or opening a PR. | Must clearly mark that no publish operation occurred. |
| Lightweight project memory | Persist small project-level lessons/conventions between tasks. | Optional; must not store secrets or unbounded agent context. |
| Dynamic agent selection by model | Let a model choose provider/stage routing dynamically. | Must not bypass the configured allowlist, route audit, or deterministic fallback rules. |
| Provider SDK/API backends | OpenAI API / Claude Agent SDK backends in addition to CLIs. | Must remain behind `AgentProvider`; Core must not learn provider syntax. |
| Automatic CLI installation/authorization | Install or authorize Codex/Claude/GitHub CLIs automatically. | Current policy keeps credentials and auth outside the orchestrator. |
| Vendor session transfer | Transfer an active vendor session between Codex and Claude Code. | Not supported; artifacts, not sessions, are the source of truth. (Per-provider durable resume is the [durable sessions](durable_sessions_and_fixing_affinity.md) item, not cross-provider transfer.) |
| Multi-repo/project binding | Select a configured repository per task (`repo` field / project map). | Current config targets one repository. Requires validation and workspace isolation. |
| Agent instruction stubs in target repo | Seed or update target-repo `AGENTS.md`, `CLAUDE.md`, and skills before a run. | **Partial:** the skill-_reference_ half shipped (planning selects target-repo skills, `{skills_path}`); _authoring/managing_ the stubs themselves is still deferred (see [follow_ups.md](follow_ups.md)). |

## Shipped (implemented)

These were backlog items and have shipped. The changes are recorded in [CHANGELOG.md](../../CHANGELOG.md) (the numbered design records have been removed). Kept here for traceability.

| Item | Shipped | Notes |
| --- | --- | --- |
| UX improvements (stop/restart, runtime-file excludes, `gh` pre-flight, `worc` alias) | 2026-06-13 | — |
| Auto-merge bypass flags (global + per-task) | 2026-06-13 | — |
| Prompt template customization (core `prompts:` block) | 2026-06-13 | — |
| Per-stage model/reasoning — per-task `stages:` overrides (§3) | 2026-06-13 | §4 `stage_defaults` still open (above) |
| Stage skip control (per-task + global) | 2026-06-13 | — |
| Post-test-run review improvements | 2026-06-14 | — |
| `install-templates` command (add-missing-only) | 2026-06-14 | — |
| `rerun` command (fresh + `--continue`) | 2026-06-14 | — |
| `finalize` command (`--as done\|failed\|abandoned`) | 2026-06-14 | — |
| Prompt templates simplification (prompts-only, auto-detect, schema v6) | 2026-06-14 | — |

## Open detail files (in this folder)

| Item | Detail |
| --- | --- |
| Workflow execution foundation | [workflow_execution_foundation.md](workflow_execution_foundation.md) |
| Supervisor quality-gate | [supervisor_quality_gate.md](supervisor_quality_gate.md) |
| Durable sessions and implementation/fixing affinity | [durable_sessions_and_fixing_affinity.md](durable_sessions_and_fixing_affinity.md) |
| Hybrid agent testing | [hybrid_agent_testing.md](hybrid_agent_testing.md) |
| Task workflow profiles | [task_workflow_profiles.md](task_workflow_profiles.md) |
| Documentation update stage | [documentation_update_stage.md](documentation_update_stage.md) |
| Runtime provider capacity gate | [runtime_provider_capacity_gate.md](runtime_provider_capacity_gate.md) |
| Token optimization | [token_optimization.md](token_optimization.md) |

## Not Backlog

The following are already part of the v1 spec / current implementation and should not be tracked here as future work unless their scope changes:

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
- Telegram terminal notifications, typed HITL for `refinement`/`planning`, durable interaction recovery, and deletion/dependency diff approvals (see [telegram.md](../telegram.md));
- automatic check discovery and environment resolution (see [configuration.md](../configuration.md)).
