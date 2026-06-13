# Architecture rules (invariants)

The source of truth is
[00_orchestrator_final_plan.md](../implementation_stages/00_orchestrator_final_plan.md). These
invariants must not be violated.

## Layers and dependencies

- **Orchestrator Core** manages the sequence of stages, attempt limits, state machine transitions, and publishing conditions. Core calls **only** the `AgentProvider` interface and **does not build** provider-specific commands.
- **Provider adapters** (`CodexProvider`, `ClaudeCodeProvider`) are the only place where the syntax of a specific CLI lives. They **do not perform fallback** and **do not change the state machine**.
- **Agent Router** decides the primary/fallback for a stage, the route source (global config / task override), and availability against the allowlist.
- **Git Manager / Check Runner / State Store / Artifact Store** are separate components with narrow responsibilities.

Dependency direction: `core → router → provider(interface)`. Providers do not depend on core.

## Contracts (see spec §4.3)

- `AgentProvider`: `id`, `preflight() -> ProviderHealth`, `run(AgentRunRequest) -> AgentRunResult`.
- `Notifier`: two-phase `start_ask` / `wait_for_answer` with a durable secret-free handle, plus the
  `ask_human` facade and best-effort terminal notifications.
- Each stage run is **independent** and receives all context through files/artifacts and the prompt — the vendor session is **not** a source of truth.
- The Core persists the `stage_runs` row before invoking a provider and passes its ID through
  `AgentRunRequest`; providers use it only to namespace artifacts. Provider fallback attempts share
  that stage-run ID and have distinct attempt numbers.
- The `AgentRunRequest` / `AgentRunResult` / `ProviderHealth` structures are as defined in §4.3. Do not add hidden state channels beyond them.

## Stages and routing

- Stages: `refinement`, `planning`, `implementation`, `testing`, `review`, `fixing`, `summary`, `publishing`.
- `testing` is executed by the Check Runner; `publishing` by the Git Manager. The rest are agent-driven.
- Default route: refinement/planning/implementation/fixing/summary → primary `claude`, fallback `codex`; review → primary `codex`, fallback `claude`.
- `refinement` runs first to enrich an incomplete task (no code edits) and is **skipped** by the
  Core for tasks that are already complete or flagged `refined: true`. The skip decision is
  deterministic and audited. Refinement/planning may request one typed human round-trip; the answer
  returns only through a redacted artifact path.
- A task override is allowed **only**: for known stages, with a provider from `agents.allowed`, without changing security/command/credentials, and after full validation of the task before the branch is created.
- **Decomposition** (spec §5.1) is a flag-gated sub-phase of `planning`, **off by default**. The split is proposed by the agent but accepted deterministically by the Core (gate on; `2 <= n <= max_subtasks`; linear `depends_on` only); otherwise the task runs as a single unit. Subtasks run **strictly sequentially on the single task branch** (one local commit each) into a **single PR**; the global `fix_iterations` budget spans all subtasks.
- **Validation gate** (spec §19): every task passes a structural gate on `new -> validated` before any branch or provider run. A broken task is terminal `failed`, quarantined to `tasks/rejected/`, and never branched.
- **Final `summary`** (spec §5.2) is an agent-driven, no-edit stage after `review` that writes a plain-language handoff (what / how / integration / why) which becomes the PR body. It is **best-effort, not a quality gate**: a provider failure falls back, and ultimately the Core writes a deterministic minimal summary — a reviewed, passing change is never blocked by it.

## Fallback

- Allowed **only** for infrastructure error classes (see spec §7.2).
- **Forbidden** for: failed tests/linters, review findings, incomplete fulfillment of requirements despite a successful CLI run, Git errors, an invalid task/config, exhaustion of fix cycles, or a security violation. These cases → `fixing` / `failed` / `manual_action_required`.
- Partial changes made after an infrastructure error are not rolled back automatically: a snapshot+diff is preserved, the fallback receives the current diff, and it goes through the full set of checks.

## State machine and idempotency

- Transitions are transactional; a re-run **does not** create a second commit/push/PR.
- After a restart, the unfinished step is resumed or its result is safely reconciled.
- Human waiting does not add a task status. The registered `logs/<task-id>/hitl/*.json` artifact is
  the recovery source of truth; timeout, transport error, ambiguous approval, or a repeated signal
  fails closed to `manual_action_required`.
- Publishing happens only when checks succeed and there are no blocking findings.
- After `implementation`/`fixing`, tracked-file deletion and dependency manifest/lock changes require
  approval before tests. Exact approved planning scope may be reused; expanded scope requires a new
  approval. Ordinary diffs and routine commit/push/PR do not ask.
- At most one task is active at a time (a single processing slot); the rest wait in `pending`. More than one active task on restart → `manual_action_required`.
- After a task reaches a terminal status, terminal cleanup must safely return the target repo to `repo.base_branch` before any next task can start. If cleanup or branch state is ambiguous, automatic continuation is forbidden and the task/state requires `manual_action_required`.
- Auto mode (`orchestrator.auto_mode.enabled`) controls only whether the next pending task is picked after successful terminal cleanup. It is off by default and does not change the single-active-task invariant.
- Every terminal transition appends one record to the append-only completed-tasks ledger (`logs/completed.jsonl`) after the terminal cleanup outcome is known; the ledger is never rewritten.
- The Git Manager uses **scoped staging** in the target repo — an explicit pathspec that excludes `tasks/`/`logs/`/`workspace/`; **never `git add .`/`-A`**. The git footprint mode (spec §21) is configurable: `in_repo` + `commit` (**default** — the task + its `summary.md` in the repo, stored via a separate orchestrator-made `tasks/` commit; `logs/` stays local), `in_repo` + `exclude_local` (`.git/info/exclude`, never committed), or `external` + `none` (zero footprint). Orchestration/task artifacts never enter a *code* commit.
- Every per-task automatic loop (stage attempts, fix cycles) has a configurable limit, and the total number of fix iterations per task is bounded by a single global cap (`agents.max_total_fix_iterations`). When a fix loop or the global cap is exhausted, the task stops in `manual_action_required` with a recorded failure report — it must never loop unbounded. `failed` is reserved for unrecoverable errors, not an exhausted fix budget.

## What must not be done

- Coupling core to a specific CLI.
- Granting a provider the right to commit/push/PR.
- Performing fallback on a quality error.
- Changing the provider route retroactively for a stage that has already begun.
- Continuing work when an inconsistent branch state is detected (→ `manual_action_required`).
- Running the `fixing` loop without a global per-task bound (→ must stop in `manual_action_required` with a failure report).
- Processing more than one task at a time in v1 (concurrency and worktrees are v2).
- Starting a next task before successful terminal cleanup has returned the repo to `repo.base_branch`.
- Overwriting the original task file or rewriting the completed-tasks ledger.
- Letting a task that has not passed the §19 validation gate reach branch creation or any provider run.
- Accepting a decomposition split without the deterministic rule, or running subtasks in parallel / on separate branches in v1.
- Staging with `git add .` / `git add -A` in the target repo, or letting orchestration/task artifacts enter a code commit.
- Accepting a Telegram reply from a different chat/message/callback, passing an answer through CLI
  argv, or treating `contacts` as access control.
