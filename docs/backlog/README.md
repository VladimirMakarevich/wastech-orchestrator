# Backlog

Status: **inventory** Date: 2026-06-21 Owner: Vladimir Makarevich

This folder is the single place for backlog and deferred-product ideas for **wastech-orchestrator**. It is an inventory, not an implementation contract. The source of truth remains the code; the [Functional Map](../functional/index.md) is the code-derived reference.

The canonical reference is the [Functional Map](../functional/index.md). Backlog documents must not override the hard invariants from [../../CLAUDE.md](../../CLAUDE.md), [../../AGENTS.md](../../AGENTS.md), or [../../.agents/rules/](../../.agents/rules/).

Where to find the design detail:

- **Open items** keep their detailed design in a file in this folder (linked in [§ Open detail files](#open-detail-files-in-this-folder)).
- **Completed backlog items** are archived in [archive/done/README.md](archive/done/README.md).
- **Shipped follow-up history** lives in [archive/follow_ups_history.md](archive/follow_ups_history.md) for traceability.
- **Build-time tech-debt and implementation follow-ups** live in [follow_ups.md](follow_ups.md), not here.

## Other files in this folder

| Document | Purpose |
| --- | --- |
| [implementation-roadmap.md](implementation-roadmap.md) | Historical cross-ADR **build order** for the 14 backlog items; steps 1–13 are now archived in `archive/done/`, and step 14 remains open. Ordering only, not a new design. |
| [improvements.md](improvements.md) | Aggregated intake of improvement ideas from real `worc` usage, processed one item at a time into bounded tasks. |
| [follow_ups.md](follow_ups.md) | **Open** implementation follow-ups / tech-debt discovered while building — distinct from product features. Recorded via `/sync-docs`. Completed/superseded entries are archived in [archive/follow_ups_history.md](archive/follow_ups_history.md). |
| [archive/done/README.md](archive/done/README.md) | Completed backlog items archived out of the active inventory. |
| [archive/follow_ups_history.md](archive/follow_ups_history.md) | Frozen historical log of completed and superseded `follow_ups.md` entries (as of 2026-06-22). Traceability only — not a source of truth. |
| [runtime_provider_capacity_gate.md](archive/runtime_provider_capacity_gate.md) | Detailed backlog task for checking Codex and Claude capacity before autonomous `watch` admits a pending task. |
| [archive/concurrent-task-worktrees.md](archive/concurrent-task-worktrees.md) | Decision record for processing tasks in parallel via `git worktree`: chosen as the v2 concurrency primitive (not for isolation), gated on a mandatory provider capacity gate. |
| [archive/token_optimization.md](archive/token_optimization.md) | Detailed backlog task for measuring and reducing token usage. |

## Completed backlog

Completed product backlog items are archived in [archive/done/README.md](archive/done/README.md).

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
| PR template support | Generate PR bodies from a configurable template in addition to `summary.md`. | Should integrate with the existing summary stage and Git Manager. |
| GitHub Issues integration | Link tasks to issues, update status, optionally close on PR creation/merge. | Requires GitHub auth through the existing external credential model. |
| Web UI for tasks and logs | Browser UI for pending/running/done tasks, artifacts, logs, stuck states. | Read-only first; mutations need auth/audit. |
| Per-task budget limit | Task-level budget/time/token limits. | Must not let a task raise global security or cost ceilings. |
| Auto-close stale tasks | Detect and close/archive/escalate stale pending/running/manual tasks. | Needs explicit policy and audit trail. |
| Dry-run without push | Run planning/check/review/publish prep without pushing or opening a PR. | Must clearly mark that no publish operation occurred. |
| Lightweight project memory | Persist small project-level lessons/conventions between tasks. | Optional; must not store secrets or unbounded agent context. The minimal first slice of [Orchestrator memory](archive/done/orchestrator-memory.md). |
| [Orchestrator memory (cross-task reuse, defrag, autodream)](memory/index.md) | Persistent, repo-scoped memory across tasks. Entry point = the [task hub](memory/index.md) (problem / requirements / design / acceptance / plan); design distilled from the research-backed [architecture blueprint](memory/research/memory-architecture-blueprint.md) (files-first `.worc/memory/`; short-term / long-term / entity tiers; **narrow supervisor + deterministic MemoryService/PacketBuilder/CleanupJob/DerivedIndex**; precision-first per-stage packets; trust/quarantine/audit/rollback; SQLite→embeddings→graph roadmap gated by an eval plan). Consolidates two deep-research reports; supersedes the exploratory [archive/done/orchestrator-memory.md](archive/done/orchestrator-memory.md). **V1 implemented** ([ADR-0001](memory/adr-0001-memory-subsystem-v1.md); phases 01–05 on `feat/memory-subsystem`) — disabled by default; V2/V3/V4 gated by a measured lift (AC-O4). | No secrets in memory; not an unbounded context dump; advisory only (Core decides, no enforcement channel); memory ≠ derived index; bounded + audited autonomy that never touches an active task. |
| Dynamic agent selection by model | Let a model choose provider/stage routing dynamically. | Must not bypass the configured allowlist, route audit, or deterministic fallback rules. |
| Provider SDK/API backends | OpenAI API / Claude Agent SDK backends in addition to CLIs. | Must remain behind `AgentProvider`; Core must not learn provider syntax. |
| Automatic CLI installation/authorization | Install or authorize Codex/Claude/GitHub CLIs automatically. | Current policy keeps credentials and auth outside the orchestrator. |
| Vendor session transfer | Transfer an active vendor session between Codex and Claude Code. | Not supported; artifacts, not sessions, are the source of truth. (Per-provider durable resume shipped in the durable-sessions item.) |
| Multi-repo/project binding | Select a configured repository per task (`repo` field / project map). | Current config targets one repository. Requires validation and workspace isolation. |
| Agent instruction stubs in target repo | Seed or update target-repo `AGENTS.md`, `CLAUDE.md`, and skills before a run. | **Partial:** the skill-_reference_ half shipped (whole-repo skill discovery, operator pins + the supervisor proposal, `{skills_path}`; see [skills-selection-rework.md](archive/done/skills-selection-rework.md)); _authoring/managing_ the stubs themselves is still deferred (see [follow_ups.md](follow_ups.md)). |
| [Generic node-output prompt variables](node-output-prompt-variables.md) | Let a custom flow chain arbitrary nodes by referencing each node's output by id — `{<node_id>_path}` resolves to the persisted `<node_id>.out.md`. Relaxes the closed `ALLOWED_PROMPT_VARS` / output-slot model to a flow-derived name set, still paths-only (never inlined content). Keeps the three special slots (`plan`/`summary`/`enriched_spec`) for their dedicated consumers. | Renderer invariant unchanged (path, not content); no-secrets redaction on the output artifact; the paired prompt-var lint must be flow-aware. **Proposed.** |
| [Sub-task context handoff (intra-task decompose)](subtask-context-handoff.md) | Two-layer handoff brief between decompose subtasks: a deterministic factual floor (changed files + commit + acceptance criteria, zero LLM, always) plus an interpretive 3-section supervisor brief (new surface / locked decisions / open edges) on the warm durable session. Selected by subtask `depends_on`, injected into the region's `implementation` node via `{?predecessor_context}`. Prevents re-exploration, duplication, and contract breakage between back-to-back subtasks. | V1: intra-task subtasks only (linear + diamonds). Cross-task `depends_on`, intra-task node handoff, and cross-instance persistence are out of scope. Transient — never written to the memory tiers. |
| Custom `tool` nodes (P5) | Operator Python/executable as a typed `tool` node, run out-of-process under the ceiling. | Reserved seam exists in the flow engine. See [p5-custom-tool-nodes.md](p5-custom-tool-nodes.md). |
| [Interactive operator console](cli-upgrade.md) | Attended console as a client over the `watch` daemon: `worc top` (read-only live monitor) + `worc shell` (prompt_toolkit REPL), plus a shared stop ladder for `worc stop`/`restart`. | `prompt_toolkit` ships as an optional `[shell]` extra; the daemon never imports it. No new task status / schema bump. **Proposed.** |
| [Remote operator agent over Telegram (`worc listen`)](worc-listen-remote-control.md) | Long-running Telegram interface to drive and interrogate the orchestrator by tagging the bot: a conversational "navigator" agent (NL → proposed actions, answers from redacted state) layered over a deterministic, configured-chat-only command grammar (list/status/logs/run-existing/rerun/stop). Remote twin of the local [console](cli-upgrade.md); further horizon = full task dictation (compose + create + run new tasks from chat, still via a validated file). **Proposed (exploratory — future vision).** | One bot token = one long-poll (409 Conflict), so a single daemon-owned poll broker is the shared prerequisite (also unblocks [confirmation gates](archive/done/operator-confirmation-gates.md) + [step-trace](archive/done/telegram-step-trace.md)); bounded to already-vetted artifacts (never authors task content); agent tool surface = the bounded verbs only; configured-chat trust boundary; proposer-proposes/Core-decides; fail-closed. |

## Open detail files (in this folder)

| Item | Detail |
| --- | --- |
| Runtime provider capacity gate | [runtime_provider_capacity_gate.md](archive/runtime_provider_capacity_gate.md) |
| Token optimization | [archive/token_optimization.md](archive/token_optimization.md) |
| Interactive operator console | [cli-upgrade.md](cli-upgrade.md) |
| Remote operator agent over Telegram (`worc listen`) | [worc-listen-remote-control.md](archive/worc-listen-remote-control.md) |
| Custom `tool` nodes (P5) | [p5-custom-tool-nodes.md](p5-custom-tool-nodes.md) |
| Prompt & supervisor authoring contract (refines improvements 1 / 3 / 5 + new prompt-var task) | [prompt-and-supervisor-authoring-contract.md](prompt-and-supervisor-authoring-contract.md) |
| Generic node-output prompt variables (`{<node_id>_path}`) | [node-output-prompt-variables.md](node-output-prompt-variables.md) |
| Orchestrator memory — task hub (authoritative) | [memory/index.md](memory/index.md) |
| Orchestrator memory — V1 decision (ADR-0001, accepted) | [memory/adr-0001-memory-subsystem-v1.md](memory/adr-0001-memory-subsystem-v1.md) |
| Orchestrator memory — consolidated blueprint (research) | [memory/research/memory-architecture-blueprint.md](memory/research/memory-architecture-blueprint.md) |

## Rules

- Keep detailed analysis in a dedicated backlog file when the topic needs design context.
- Keep the short product inventory in this file.
- When a deferred feature is mentioned in another document, link back here instead of creating a new isolated backlog list.
- Mark status explicitly (`backlog / not scheduled`, `candidate`, `accepted`, or `done`) before implementation starts.
