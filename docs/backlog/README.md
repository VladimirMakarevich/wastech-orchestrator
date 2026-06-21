# Backlog

Status: **inventory** Date: 2026-06-21 Owner: Vladimir Makarevich

This folder is the single place for backlog and deferred-product ideas for **wastech-orchestrator**. It is an inventory, not an implementation contract. The source of truth remains the code; the [Functional Map](../functional/index.md) is the code-derived reference.

The canonical reference is the [Functional Map](../functional/index.md). Backlog documents must not override the hard invariants from [../../CLAUDE.md](../../CLAUDE.md), [../../AGENTS.md](../../AGENTS.md), or [../../.agents/rules/](../../.agents/rules/).

Where to find the design detail:

- **Open items** keep their detailed design in a file in this folder (linked in [§ Open detail files](#open-detail-files-in-this-folder)).
- **Shipped items** are listed in [§ Shipped](#shipped-implemented) for traceability (their numbered design docs are in [outdated/](outdated/)).
- **Build-time tech-debt and implementation follow-ups** live in [follow_ups.md](follow_ups.md), not here.

## Other files in this folder

| Document | Purpose |
| --- | --- |
| [follow_ups.md](follow_ups.md) | Implementation follow-ups / tech-debt discovered while building — distinct from product features. Recorded via `/sync-docs`. |
| [2026-06-21-audit.md](2026-06-21-audit.md) | Code-verified audit (bugs, dead/decorative config, DRY, fragility, stale comments) found while reconstructing `docs/functional/` purely from the code on 2026-06-21. |
| [runtime_provider_capacity_gate.md](runtime_provider_capacity_gate.md) | Detailed backlog task for checking Codex and Claude capacity before autonomous `watch` admits a pending task. |
| [token_optimization.md](token_optimization.md) | Detailed backlog task for measuring and reducing token usage. |
| [outdated/](outdated/) | Historical design documents for shipped items and the pre-implementation flow-engine engineering specs. DO NOT USE as a source of truth — the [Functional Map](../functional/index.md) is. |

## Open backlog

### Agent quality and continuity program

The program shipped in four phases (P1–P4, 2026-06-18 to 2026-06-21); all phase engineering specs are in [outdated/flows/](outdated/flows/) for historical reference. One item from the original program remains open:

| Item | Summary | Status |
| --- | --- | --- |
| Documentation update stage | Optional, default-on finalizing `implementation` stage: a dedicated doc agent (own prompt/model/reasoning) updates the target repo's docs from the accepted-outcome context, doc-path-guarded, before summary/publishing. Historical design: [outdated/documentation_update_stage.md](outdated/documentation_update_stage.md). | accepted (not scheduled) |

### Other deferred features

These are deferred by the v1 spec or described in architecture notes; not scheduled.

| Item | Summary | Source / constraint |
| --- | --- | --- |
| [Runtime provider capacity gate](runtime_provider_capacity_gate.md) | Before autonomous `watch` claims a pending task, query the capacity of the Codex/Claude accounts its resolved routes need and defer the task when configured headroom is unavailable. | Runtime admission control, not install preflight. Deferred tasks stay pending, consume no attempts, retried after provider reset. |
| [Token optimization](token_optimization.md) | Measure and reduce token consumption across stages. | Analysis + candidate levers; not part of v1 scope. |
| Config-level per-stage `stage_defaults` | The complement to the shipped per-task `stages:` overrides: per-stage `{model,reasoning}` defaults under each provider in `config.yaml`. | Tracked in [follow_ups.md](follow_ups.md). Touches schema (+version bump), loader, validation. |
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
| Custom `tool` nodes (P5) | Operator Python/executable as a typed `tool` node, run out-of-process under the ceiling. | Reserved seam exists in the flow engine. See [outdated/flows/p5-custom-tool-nodes.md](outdated/flows/p5-custom-tool-nodes.md). |
| `Stage` enum removal + per-node observability paths | Remove the load-bearing `Stage` enum from routing; give each node its own interaction/audit path. | Deferred from P3/P4. Research and audit nodes currently share a kind-based Stage identity. |

## Shipped (implemented)

These were backlog items and have shipped. Design docs for the program items are in [outdated/](outdated/).

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
| Workflow execution foundation (flow engine P1) | 2026-06-18 | `FlowEngine` as the sole driver; immutable resolved-profile snapshots; execution roles/session scopes (`run_kind`, `role`); reusable evaluator-loop primitive; common output/audit/path/delta contracts. Design: [outdated/workflow_execution_foundation.md](outdated/workflow_execution_foundation.md) |
| Supervisor quality-gate (flow engine P2) | 2026-06-19 | Constant per-step oversight layer: `observe` after each node + `finalize` summary at close; `evaluations` table (state.db v8); mandatory with no disable mode. Design: [outdated/supervisor_quality_gate.md](outdated/supervisor_quality_gate.md) |
| Durable sessions and implementation/fixing affinity (flow engine P2) | 2026-06-19 | `editing_lineage` table (state.db v9); Claude `--resume` / Codex `exec resume`; provider-aware affinity; raw-ID redaction; `session_unavailable` retry. Design: [outdated/durable_sessions_and_fixing_affinity.md](outdated/durable_sessions_and_fixing_affinity.md) |
| Hybrid agent testing infrastructure (flow engine P2) | 2026-06-19 | `test_quality` evaluator role + evaluator primitive; checks mutation guard; optional `testing_quality` node available for operator-custom flows (not in the packaged default flow by design — node presence is graph shape, not a config flag). Design: [outdated/hybrid_agent_testing.md](outdated/hybrid_agent_testing.md) |
| Task workflow profiles — deep_research, security_audit (flow engine P3) | 2026-06-21 | `task_type` dispatch; `deep_research.yaml` and `security_audit.yaml` packaged flows; `citation` + `dependency_scan` checkers; output/network policies; private report storage under `.worc/security-reports/`. Design: [outdated/task_workflow_profiles.md](outdated/task_workflow_profiles.md) |
| Operator flow surface (flow engine P4) | 2026-06-21 | `FlowRegistry` with `.worc/flows/` operator directory; `validate_flow_against_config` (provider/reasoning/ceiling); fatal preflight validation of all flows. |

## Not Backlog

The following are already part of the v1 spec / current implementation and should not be tracked here as future work unless their scope changes:

- provider abstraction through `AgentProvider`;
- infrastructure-only fallback;
- deterministic flow graph (YAML-declared nodes and edges, driven by `FlowEngine`);
- validation gate before branch/provider runs;
- bounded fix loops and global fix-iteration budget;
- optional v1 decomposition as a sequential planning sub-phase;
- constant supervisor oversight layer and whole-task summary;
- the canonical `.worc/` layout, scoped staging, and the task-scoped audit commit;
- terminal cleanup and auto mode;
- state-store recovery and idempotent publishing;
- Telegram terminal notifications, typed HITL for `refinement`/`planning`, durable interaction recovery, and deletion/dependency diff approvals (see [telegram.md](../telegram.md));
- automatic check discovery and environment resolution (see [configuration.md](../configuration.md));
- operator-extensible flow registry with config-aware validation (see [configuration.md](../configuration.md)).

## Open detail files (in this folder)

| Item | Detail |
| --- | --- |
| Runtime provider capacity gate | [runtime_provider_capacity_gate.md](runtime_provider_capacity_gate.md) |
| Token optimization | [token_optimization.md](token_optimization.md) |

Design documents for shipped items are in [outdated/](outdated/) — historical reference only. The documentation-update-stage design is there too: [outdated/documentation_update_stage.md](outdated/documentation_update_stage.md).

## Rules

- Keep detailed analysis in a dedicated backlog file when the topic needs design context.
- Keep the short product inventory in this file.
- When a deferred feature is mentioned in another document, link back here instead of creating a new isolated backlog list.
- Mark status explicitly (`backlog / not scheduled`, `candidate`, `accepted`, or `done`) before implementation starts.
