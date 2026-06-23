# Backlog

Status: **inventory** Date: 2026-06-21 Owner: Vladimir Makarevich

This folder is the single place for backlog and deferred-product ideas for **wastech-orchestrator**. It is an inventory, not an implementation contract. The source of truth remains the code; the [Functional Map](../functional/index.md) is the code-derived reference.

The canonical reference is the [Functional Map](../functional/index.md). Backlog documents must not override the hard invariants from [../../CLAUDE.md](../../CLAUDE.md), [../../AGENTS.md](../../AGENTS.md), or [../../.agents/rules/](../../.agents/rules/).

Where to find the design detail:

- **Open items** keep their detailed design in a file in this folder (linked in [§ Open detail files](#open-detail-files-in-this-folder)).
- **Shipped items** are listed in [§ Shipped](#shipped-implemented) for traceability (their numbered design docs are in [archive/outdated/](archive/outdated/)).
- **Build-time tech-debt and implementation follow-ups** live in [follow_ups.md](follow_ups.md), not here.

## Other files in this folder

| Document | Purpose |
| --- | --- |
| [follow_ups.md](follow_ups.md) | **Open** implementation follow-ups / tech-debt discovered while building — distinct from product features. Recorded via `/sync-docs`. Completed/superseded entries are archived in [archive/follow_ups_history.md](archive/follow_ups_history.md). |
| [archive/follow_ups_history.md](archive/follow_ups_history.md) | Frozen historical log of completed and superseded `follow_ups.md` entries (as of 2026-06-22). Traceability only — not a source of truth. |
| [2026-06-21-audit.md](2026-06-21-audit.md) | Code-verified audit (bugs, dead/decorative config, DRY, fragility, stale comments) found while reconstructing `docs/functional/` purely from the code on 2026-06-21. |
| [stage-enum-removal.md](stage-enum-removal.md) | Detail file for the `follow_ups.md` task that deletes the `Stage` enum and re-founds per-task skip on flow node ids. Full design, change list, and inventory. |
| [gh-auth-status-warning.md](gh-auth-status-warning.md) | Detail file for the `follow_ups.md` task that adds a non-blocking `gh auth status` advisory at `run`/`watch`/`rerun` startup. |
| [task-dependencies.md](task-dependencies.md) | Detail file for the `follow_ups.md` task that adds a task-level `depends_on` field with non-blocking merge-gated scheduling (and folds in the real merge-SHA backfill). |
| [provider-config-cleanup.md](provider-config-cleanup.md) | Detail file for the `follow_ups.md` task that makes each provider's full-access mode operator-selectable under `strict_isolation: false` (Codex `danger-full-access` sandbox; Claude `bypassPermissions` / profile-escalating `--permission-mode`), deletes the unused `max_budget_usd` field, and ships explicit default model/reasoning for both providers (one config-version bump). |
| [per-node-network-access.md](per-node-network-access.md) | Detail file for the `follow_ups.md` task that adds an opt-in, operator-owned `network_access: true\|false` field to agent/evaluator flow nodes — a per-node override of the flow-wide `network_policy` default (default off; backward compatible). |
| [operator-authored-decomposition.md](operator-authored-decomposition.md) | Detail file for the operator-authored-decomposition feature (**shipped 2026-06-22**): one root task with a `subtasks:` list of references runs like a planning-proposed split (sequential, one branch, one PR), reusing the whole decomposition pipeline — only the source of the `DecompositionDecision` changes. |
| [checks-monorepo.md](checks-monorepo.md) | Detail file for the accepted direction to delete `checks.discovery` and support polyglot monorepos via operator-owned `command_sets` (named sets with `cwd`) selected deterministically by `git diff`. Records the rejected alternatives (commands-in-flow, agent/supervisor filtering) and the heterogeneous-toolchain host constraint. |
| [runtime_provider_capacity_gate.md](runtime_provider_capacity_gate.md) | Detailed backlog task for checking Codex and Claude capacity before autonomous `watch` admits a pending task. |
| [archive/token_optimization.md](token_optimization.md) | Detailed backlog task for measuring and reducing token usage. |
| [archive/outdated/](archive/outdated/) | Historical design documents for shipped items and the pre-implementation flow-engine engineering specs. DO NOT USE as a source of truth — the [Functional Map](../functional/index.md) is. |

## Open backlog

### Other deferred features

These are deferred by the v1 spec or described in architecture notes; not scheduled.

| Item | Summary | Source / constraint |
| --- | --- | --- |
| [Runtime provider capacity gate](runtime_provider_capacity_gate.md) | Before autonomous `watch` claims a pending task, query the capacity of the Codex/Claude accounts its resolved routes need and defer the task when configured headroom is unavailable. | Runtime admission control, not install preflight. Deferred tasks stay pending, consume no attempts, retried after provider reset. |
| [Token optimization](token_optimization.md) | Measure and reduce token consumption across stages. | Analysis + candidate levers; not part of v1 scope. |
| Config-level per-node `node_defaults` | The complement to the shipped per-task `nodes:` overrides: per-node `{model,reasoning}` defaults under each provider in `config.yaml`. | Tracked in [follow_ups.md](follow_ups.md). Touches schema (+version bump), loader, validation. |
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
| Vendor session transfer | Transfer an active vendor session between Codex and Claude Code. | Not supported; artifacts, not sessions, are the source of truth. (Per-provider durable resume shipped in the durable-sessions item.) |
| Multi-repo/project binding | Select a configured repository per task (`repo` field / project map). | Current config targets one repository. Requires validation and workspace isolation. |
| Agent instruction stubs in target repo | Seed or update target-repo `AGENTS.md`, `CLAUDE.md`, and skills before a run. | **Partial:** the skill-_reference_ half shipped (planning selects target-repo skills, `{skills_path}`); _authoring/managing_ the stubs themselves is still deferred (see [follow_ups.md](follow_ups.md)). |
| Custom `tool` nodes (P5) | Operator Python/executable as a typed `tool` node, run out-of-process under the ceiling. | Reserved seam exists in the flow engine. See [archive/outdated/flows/p5-custom-tool-nodes.md](archive/outdated/flows/p5-custom-tool-nodes.md). |

## Open detail files (in this folder)

| Item | Detail |
| --- | --- |
| Runtime provider capacity gate | [runtime_provider_capacity_gate.md](runtime_provider_capacity_gate.md) |
| Token optimization | [archive/token_optimization.md](token_optimization.md) |

## Rules

- Keep detailed analysis in a dedicated backlog file when the topic needs design context.
- Keep the short product inventory in this file.
- When a deferred feature is mentioned in another document, link back here instead of creating a new isolated backlog list.
- Mark status explicitly (`backlog / not scheduled`, `candidate`, `accepted`, or `done`) before implementation starts.
