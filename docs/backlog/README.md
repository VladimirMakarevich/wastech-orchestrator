# Backlog

Status: **inventory** Date: 2026-06-21 Owner: Vladimir Makarevich

This folder is the single place for backlog and deferred-product ideas for **wastech-orchestrator**. It is an inventory, not an implementation contract. The source of truth remains the code; the [Functional Map](../functional/index.md) is the code-derived reference.

The canonical reference is the [Functional Map](../functional/index.md). Backlog documents must not override the hard invariants from [../../CLAUDE.md](../../CLAUDE.md), [../../AGENTS.md](../../AGENTS.md), or [../../.agents/rules/](../../.agents/rules/).

Where to find the design detail:

- **Open items** keep their detailed design in a file in this folder (linked in [§ Open detail files](#open-detail-files-in-this-folder)).
- **Shipped items** and their history are recorded in [archive/follow_ups_history.md](archive/follow_ups_history.md) for traceability.
- **Build-time tech-debt and implementation follow-ups** live in [follow_ups.md](follow_ups.md), not here.

## Other files in this folder

| Document | Purpose |
| --- | --- |
| [implementation-roadmap.md](implementation-roadmap.md) | Cross-ADR **build order** for the 14 open ADRs — sequences them by shared seam (config, CLI, task-scan, watch loop, provider errors, Telegram, supervisor) to minimize rework and conflicts. Ordering only, not a new design. |
| [improvements.md](improvements.md) | Aggregated intake of improvement ideas from real `worc` usage, processed one item at a time into bounded tasks. |
| [follow_ups.md](follow_ups.md) | **Open** implementation follow-ups / tech-debt discovered while building — distinct from product features. Recorded via `/sync-docs`. Completed/superseded entries are archived in [archive/follow_ups_history.md](archive/follow_ups_history.md). |
| [archive/follow_ups_history.md](archive/follow_ups_history.md) | Frozen historical log of completed and superseded `follow_ups.md` entries (as of 2026-06-22). Traceability only — not a source of truth. |
| [runtime_provider_capacity_gate.md](archive/runtime_provider_capacity_gate.md) | Detailed backlog task for checking Codex and Claude capacity before autonomous `watch` admits a pending task. |
| [archive/concurrent-task-worktrees.md](archive/concurrent-task-worktrees.md) | Decision record for processing tasks in parallel via `git worktree`: chosen as the v2 concurrency primitive (not for isolation), gated on a mandatory provider capacity gate. |
| [archive/token_optimization.md](archive/token_optimization.md) | Detailed backlog task for measuring and reducing token usage. |

## Open backlog

### Other deferred features

These are deferred by the v1 spec or described in architecture notes; not scheduled.

| Item | Summary | Source / constraint |
| --- | --- | --- |
| [Runtime provider capacity gate](archive/runtime_provider_capacity_gate.md) | Before autonomous `watch` claims a pending task, query the capacity of the Codex/Claude accounts its resolved routes need and defer the task when configured headroom is unavailable. | Runtime admission control, not install preflight. Deferred tasks stay pending, consume no attempts, retried after provider reset. |
| [Token optimization](archive/token_optimization.md) | Measure and reduce token consumption across stages. | Analysis + candidate levers; not part of v1 scope. |
| Config-level per-node `node_defaults` | The complement to the shipped per-task `nodes:` overrides: per-node `{model,reasoning}` defaults under each provider in `config.yaml`. | Tracked in [follow_ups.md](follow_ups.md). Touches schema (+version bump), loader, validation. |
| Richer task parsing | Extract structured metadata beyond `id`, `title`, `refined`, `decompose`, `agents`, `contacts`, `model`, `reasoning`, `stages`, `branch_name`, `auto_merge`. | Candidate fields: repo binding, commands/hints, priority, labels, issue links. Must stay fail-closed. |
| Parallel and graph decomposition | Graph-shaped subtasks, per-subtask worktrees/branches, parallel execution. | V1 decomposition is linear, sequential, on one task branch. |
| [Concurrent task processing via worktrees](archive/concurrent-task-worktrees.md) | Process multiple independent tasks at once by giving each its own `git worktree`. | Must not share a mutable working copy between active agents. Breaks the single-active-task invariant only behind worktree isolation. Deferred v2; isolation alone is solved by a dedicated clone. **Decision record.** |
| [Queue priorities](task-priority.md) | `priority: low \| mid \| high` field in task files; scheduler picks highest-priority eligible task first; `mid` default; `depends_on` always wins over priority. | Must preserve single-active-task invariant unless worktree concurrency lands. **Proposed.** |
| PR template support | Generate PR bodies from a configurable template in addition to `summary.md`. | Should integrate with the existing summary stage and Git Manager. |
| GitHub Issues integration | Link tasks to issues, update status, optionally close on PR creation/merge. | Requires GitHub auth through the existing external credential model. |
| Web UI for tasks and logs | Browser UI for pending/running/done tasks, artifacts, logs, stuck states. | Read-only first; mutations need auth/audit. |
| [Auto-retry on network errors](transient-provider-failure-recovery.md) | Retry transient network/provider failures before fallback or terminal failure. | Must be bounded and audited; never retry quality failures. Investigation + options in the detail file (**proposed**). |
| Per-task budget limit | Task-level budget/time/token limits. | Must not let a task raise global security or cost ceilings. |
| Auto-close stale tasks | Detect and close/archive/escalate stale pending/running/manual tasks. | Needs explicit policy and audit trail. |
| Dry-run without push | Run planning/check/review/publish prep without pushing or opening a PR. | Must clearly mark that no publish operation occurred. |
| Lightweight project memory | Persist small project-level lessons/conventions between tasks. | Optional; must not store secrets or unbounded agent context. The minimal first slice of [Orchestrator memory](orchestrator-memory.md). |
| [Orchestrator memory (cross-task reuse, defrag, autodream)](memory/index.md) | Persistent, repo-scoped memory across tasks. Entry point = the [task hub](memory/index.md) (problem / requirements / design / acceptance / plan); design distilled from the research-backed [architecture blueprint](memory/research/memory-architecture-blueprint.md) (files-first `.worc/memory/`; short-term / long-term / entity tiers; **narrow supervisor + deterministic MemoryService/PacketBuilder/CleanupJob/DerivedIndex**; precision-first per-stage packets; trust/quarantine/audit/rollback; SQLite→embeddings→graph roadmap gated by an eval plan). Consolidates two deep-research reports; supersedes the exploratory [orchestrator-memory.md](orchestrator-memory.md). **V1 design accepted ([ADR-0001](memory/adr-0001-memory-subsystem-v1.md)); build pending.** | No secrets in memory; not an unbounded context dump; advisory only (Core decides, no enforcement channel); memory ≠ derived index; bounded + audited autonomy that never touches an active task. |
| Dynamic agent selection by model | Let a model choose provider/stage routing dynamically. | Must not bypass the configured allowlist, route audit, or deterministic fallback rules. |
| Provider SDK/API backends | OpenAI API / Claude Agent SDK backends in addition to CLIs. | Must remain behind `AgentProvider`; Core must not learn provider syntax. |
| Automatic CLI installation/authorization | Install or authorize Codex/Claude/GitHub CLIs automatically. | Current policy keeps credentials and auth outside the orchestrator. |
| Vendor session transfer | Transfer an active vendor session between Codex and Claude Code. | Not supported; artifacts, not sessions, are the source of truth. (Per-provider durable resume shipped in the durable-sessions item.) |
| Multi-repo/project binding | Select a configured repository per task (`repo` field / project map). | Current config targets one repository. Requires validation and workspace isolation. |
| Agent instruction stubs in target repo | Seed or update target-repo `AGENTS.md`, `CLAUDE.md`, and skills before a run. | **Partial:** the skill-_reference_ half shipped (whole-repo skill discovery, operator pins + the supervisor proposal, `{skills_path}`; see [skills-selection-rework.md](skills-selection-rework.md)); _authoring/managing_ the stubs themselves is still deferred (see [follow_ups.md](follow_ups.md)). |
| Custom `tool` nodes (P5) | Operator Python/executable as a typed `tool` node, run out-of-process under the ceiling. | Reserved seam exists in the flow engine. See [p5-custom-tool-nodes.md](p5-custom-tool-nodes.md). |
| [Windows / cross-platform support](windows-cross-platform-support.md) | Full Windows support: stop-file IPC for graceful `watch`/`stop`, Python-launcher `fake_cli` in tests (no `.cmd`), `windows-latest` CI runner. Core Python is already clean; gaps are daemon IPC and test infra. **Proposed.** | Confirmed no `sh -c` operator commands in prod. Three targeted changes, no new dependencies. |
| [Interactive operator console](cli-upgrade.md) | Attended console as a client over the `watch` daemon: `worc top` (read-only live monitor) + `worc shell` (prompt_toolkit REPL), plus a shared stop ladder for `worc stop`/`restart`. | `prompt_toolkit` ships as an optional `[shell]` extra; the daemon never imports it. No new task status / schema bump. **Proposed.** |
| [Task discovery: `worc list` + shell completion](cli-task-list-and-completion.md) | One-shot, dependency-free `worc list` (active + `tasks/pending` queue + recent terminal) and a `worc completion bash\|zsh` script that completes task-ids by shelling out to `worc list --format ids`. | Read-only; reuses existing read helpers + a shared `recent_tasks` query (the same one `worc top` needs). No `argcomplete`, no new dependency, no schema bump. Additive down-payment on the console item. **Implemented (2026-06-27).** |
| [Configurable tasks directory](configurable-tasks-dir.md) | `paths.tasks_dir` in `config.yaml` (default `tasks`) makes the task inbox directory name configurable per project; `worc install` prompts for the value. Adding the directory to `.gitignore` degrades gracefully with no extra config key. | `tasks/` is hardcoded in 5+ modules today; collides with projects that already use that directory name. **Proposed.** |
| [Log management: `worc logs clean` and `logging.*` config](log-management.md) | `worc logs clean [--keep N \| --all]` to reclaim disk space from task artifact directories; `logging.level` and `logging.artifacts` (minimal/standard/full) in `config.yaml` to persist operator-trace verbosity and control which per-node files are written. **Implemented (2026-06-27, `schema_version` 23).** | `completed.jsonl` never deleted by default; redaction invariant holds at all levels. |
| [Operator confirmation gates in autonomous mode](operator-confirmation-gates.md) | Two durable Telegram approval gates for full-auto `watch`: confirm before claiming each next task (`auto_mode.confirm_next_task`), and "continue or stop?" when an agent hits `max_turns` (`max_turns_gate`, continue = resume +N), letting the default cap drop from 400 to ~50–100. **Proposed.** | Provider stays dumb (gate is flow/orchestrator-level); reuses existing `ask_human` HITL + durable-resume; fail-closed (preflight error if gate-on without Telegram, timeout→STOP). Feasibility crux: does `--resume` grant a fresh turn budget? |
| [Telegram step-trace (live run progress)](telegram-step-trace.md) | One-way best-effort live feed to Telegram of each node finish (`node id → outcome`), toggled by a single global flag (`telegram.trace`, default off). **Proposed.** | No secrets in messages (node id + outcome only); best-effort, never blocks; hooks the existing post-node observability seam; distinct from `logging.*` (files vs Telegram). |
| [Skills selection rework](skills-selection-rework.md) | Move repo-skill selection off the `planning` node to two layers: operator-pinned `skills:` per flow node (deterministic) + an optional once-per-task supervisor proposal of a `node → skills` map (proposer proposes, Core decides). Whole-repo inventory auto-discovered via `git ls-files **/SKILL.md`. Stays in Model A (read-only reference paths, never executed). **Proposed.** | Keeps provider parity + never-executed safety; hooks/executable skills + diff-scoped sets out of scope. Bare-name identity (path on collision), warn-by-default on unresolved pins, `skills: {dynamic, strict}` config. |
| [Remote operator agent over Telegram (`worc listen`)](worc-listen-remote-control.md) | Long-running Telegram interface to drive and interrogate the orchestrator by tagging the bot: a conversational "navigator" agent (NL → proposed actions, answers from redacted state) layered over a deterministic, configured-chat-only command grammar (list/status/logs/run-existing/rerun/stop). Remote twin of the local [console](cli-upgrade.md); further horizon = full task dictation (compose + create + run new tasks from chat, still via a validated file). **Proposed (exploratory — future vision).** | One bot token = one long-poll (409 Conflict), so a single daemon-owned poll broker is the shared prerequisite (also unblocks [confirmation gates](operator-confirmation-gates.md) + [step-trace](telegram-step-trace.md)); bounded to already-vetted artifacts (never authors task content); agent tool surface = the bounded verbs only; configured-chat trust boundary; proposer-proposes/Core-decides; fail-closed. |
| [Task queue tags for multiple worc instances](multi-instance-task-queues.md) | Optional `queue` tag per task + a per-instance selector (`orchestrator.queue`, `--queue` override) so several worc instances sharing one git-distributed pool don't grab the same task. Static partition by string equality; default `default` both sides preserves the single-instance case. **Proposed.** | Mutual exclusion **between** instances (distinct from one instance running many tasks via [worktrees](archive/concurrent-task-worktrees.md)). No shared `state.db`; selector is local config, tag travels in the task file through git. Partitions but does not arbitrate — "one worc per queue" is operator-enforced. Fail-closed parsing; no new status/schema. |
| [Branch name: epoch prefix + total length cap](branch-name-epoch-and-slug-limit.md) | Auto-generated branch name `{prefix}/{epoch}-{task_id}-{slug}` capped at 50 chars total (slug truncated to fit); operator `branch_name` > 50 chars falls back to auto-gen + warning. No schema bump. **Proposed.** | Re-run collisions + 100+ char slugs from long titles. Slug budget is dynamic (50 minus fixed segments). Four targeted changes in parser, git_manager, orchestrator, validation_gate. |
| [Per-node model/reasoning/provider in task front matter](task-node-model-override.md) | Extend `NodeOverride` with `model`, `reasoning`, `provider` fields; applied as a best-effort overlay in `engine_driver` before node execution. Invalid overrides fall back to the flow's declared value + warning (watch-mode compat). Eliminates per-model flow YAML variants. **Proposed.** | Medium complexity: schema extension + one merge step in `engine_driver`. Four open questions before "accepted". |

## Open detail files (in this folder)

| Item | Detail |
| --- | --- |
| Runtime provider capacity gate | [runtime_provider_capacity_gate.md](archive/runtime_provider_capacity_gate.md) |
| Auto-retry on network errors (transient provider-failure recovery) | [transient-provider-failure-recovery.md](transient-provider-failure-recovery.md) |
| Token optimization | [archive/token_optimization.md](archive/token_optimization.md) |
| Interactive operator console | [cli-upgrade.md](cli-upgrade.md) |
| Task discovery: `worc list` + shell completion | [cli-task-list-and-completion.md](cli-task-list-and-completion.md) |
| Configurable tasks directory | [configurable-tasks-dir.md](configurable-tasks-dir.md) |
| Log management (`worc logs clean` + `logging.*`) | [log-management.md](log-management.md) |
| Operator confirmation gates in autonomous mode | [operator-confirmation-gates.md](operator-confirmation-gates.md) |
| Telegram step-trace (live run progress) | [telegram-step-trace.md](telegram-step-trace.md) |
| Skills selection rework | [skills-selection-rework.md](skills-selection-rework.md) |
| Remote operator agent over Telegram (`worc listen`) | [worc-listen-remote-control.md](archive/worc-listen-remote-control.md) |
| Orchestrator memory — task hub (authoritative) | [memory/index.md](memory/index.md) |
| Orchestrator memory — V1 decision (ADR-0001, accepted) | [memory/adr-0001-memory-subsystem-v1.md](memory/adr-0001-memory-subsystem-v1.md) |
| Orchestrator memory — consolidated blueprint (research) | [memory/research/memory-architecture-blueprint.md](memory/research/memory-architecture-blueprint.md) |
| Orchestrator memory — exploratory predecessor (superseded) | [orchestrator-memory.md](orchestrator-memory.md) |
| Task queue tags for multiple worc instances | [multi-instance-task-queues.md](multi-instance-task-queues.md) |
| Task priority field | [task-priority.md](task-priority.md) |
| Branch name: epoch prefix + slug limit | [branch-name-epoch-and-slug-limit.md](branch-name-epoch-and-slug-limit.md) |
| Per-node model/reasoning/provider in task front matter | [task-node-model-override.md](task-node-model-override.md) |

## Rules

- Keep detailed analysis in a dedicated backlog file when the topic needs design context.
- Keep the short product inventory in this file.
- When a deferred feature is mentioned in another document, link back here instead of creating a new isolated backlog list.
- Mark status explicitly (`backlog / not scheduled`, `candidate`, `accepted`, or `done`) before implementation starts.
