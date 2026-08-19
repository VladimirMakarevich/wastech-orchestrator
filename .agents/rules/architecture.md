# Architecture rules (invariants)

The source of truth is the code (`src/wastech_orchestrator/`). These invariants must not be violated. The design rationale lives in [worc_architecture.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/worc_architecture.md) — on `main` only, so the link is absolute (see [git-workflow.md](git-workflow.md) §A).

## Domain-agnostic and data-driven (no hardcoding)

- The orchestrator hosts an unbounded number of operator-authored flows across arbitrary domains (software, content, research, audits, and topics not yet imagined). The core is **domain-agnostic**: it knows flow _shape_ and node _kind_, never the topic. Adding a new flow on a new topic must require **zero** core/engine code changes.
- Nothing may be hardcoded to a specific flow, topic, task type, node id, or count — no magic strings, no `if node.id == "…"`, no per-topic branches, no fixed "flows we support" list. Every such decision is driven by flow/config **data**.

## Design values

- **Flexibility and ease of use come first**, in both code and docs: prefer the simplest, most flexible design that keeps these invariants, expose configuration and flow authoring as data (not code changes), and make the common path obvious. This never overrides the security envelope — flexibility means arbitrary _safe_ flows, not an escape hatch.

## Layers and dependencies

- **Core** is a thin wrapper around the FlowEngine: it owns the validation gate, the single processing slot, the isolation/check preamble, node wiring, state-machine transitions, and terminal handling. It calls **only** the Router, the Check Runner, and the Git Manager, and **never builds provider-specific commands**.
- **FlowEngine** traverses the validated flow graph, owns the transitions between nodes, charges the fix loops, and drives the node runners.
- **Provider adapters** are the only place a specific CLI's syntax lives. They **perform no fallback** and **do not change the state machine**.
- **Router** resolves a node's `(primary, fallback)` from config and checks availability against the allowlist. Routing is node-based. It also answers the per-attempt capability questions core must not answer itself — "can this provider isolate on this host" and "does this attempt get a shell" — from tables of provider-owned functions the composition root injects, so core learns the answer without importing an adapter.
- **Supervisor** is a per-task oversight layer **above** any flow (not a node), constant by default and removable in one config key (`supervisor.enabled`): it observes completed steps read-only at the configured cadence and writes the whole-task summary at close; with the layer off that summary is rendered deterministically from the run's recorded facts instead.
- **Git Manager / Check Runner / State Store / Artifact Store** are separate components with narrow responsibilities.
- **Memory** (optional, off by default) has its own invariants: redacted atomic writes, an append-only audit, and never passing unredacted content into a prompt.
- **Dependency direction is `core → router → provider(interface)`** — providers never depend on core, and core imports only the provider _interface_, never a concrete adapter. This is machine-enforced by `import-linter` (`lint-imports`). The factories that bind the concrete adapters live in the composition root, not in core.

## Contracts

- `AgentProvider`: `id`, `preflight()`, `run(AgentRunRequest) -> AgentRunResult`.
- `Notifier`: two-phase `start_ask` / `wait_for_answer` with a durable, secret-free handle.
- Each node receives its context through files/artifacts and the prompt, not through hidden channels.
- `AgentRunRequest` / `AgentRunResult` / `ProviderHealth` are the only data channels between core and a provider. Do not add hidden state channels beyond them.

## Flow nodes and routing

- The pipeline is **data, not a fixed stage loop**: each `task_type` resolves to a validated flow (a YAML graph of typed nodes) driven by the FlowEngine. There is **no `Stage` enum**.
- **Flows are fully operator-authorable — no node is privileged.** The engine dispatches a runner by `node.kind` (`agent`, `checks`, `evaluator`, `publish`) and **never** branches on a specific node **id**. No packaged id is mandatory — an operator may rename, drop, reorder, or duplicate nodes freely. Behavior attaches to a node **kind** or a declared fact, never to an id.
- Flow **shape** is unrestricted; flow **safety is not**. Every flow — packaged or custom — is fatally validated before use: graph well-formedness plus the non-weakening security envelope (permission ceiling, output/network policy, provider ∈ allowed, budgets ≤ caps). Maximum flexibility means arbitrary graphs, not an escape hatch from those gates.
- **Check commands are operator-authored** in config (no discovery, no agent proposal, no flow-supplied command); a deterministic diff-based selector picks which sets run, and core passes those argv lists through — it never builds CLI argv itself.
- Routing is **node-based**: a node runs on its declared provider or the single global primary; the fallback target is the other allowed provider (none when only one is allowed).
- `refinement` runs first to enrich an incomplete task (no code edits) and is deterministically **skipped** when no enrichment is needed. A refinement/planning human round-trip returns its answer only through a redacted artifact path.
- A task may override a node's `{model, reasoning, provider}` per run. Overrides are **best-effort**: they never change security, commands, or credentials, and an invalid override is warned and skipped — the flow's declared value stands and the task is never aborted.
- **Decomposition** is flag-gated and **off by default**. The split is proposed by the agent but accepted deterministically by the core; subtasks then run strictly sequentially on the single task branch (one commit each) into a single PR.
- **Validation gate**: every task passes a structural gate before any branch or provider run. A broken task is terminal `failed`, quarantined, and never branched.
- **`documentation`** is a workspace-write node after `review` accepts the code: it updates the target project's docs to match the shipped change, and its edits join the same diff the orchestrator commits.
- **Supervisor layer** (not a node; constant by default, and removable with one key): it observes completed steps read-only at the operator's configured cadence (advisory — it can flag but cannot rework) and writes the plain-language handoff at close, which becomes the PR body. It is best-effort, not a quality gate: a reviewed, passing change is never blocked by it. Switched off, the PR body is rendered deterministically from the run's own recorded facts, so no run ships without one.

- **Detection brackets and the diff gate**: the Git-control fingerprint around an attempt covers the **remote** side too (what `origin` holds for the task branch, where a push would go, whether the branch has an open PR) and the configuration the publishing processes read (the clone's agent-CLI config and the user git config, by digest) — cutting the agent off from `.git` stops only one publishing operation out of four. The base branch is deliberately outside the comparison: it moves legitimately. The bracket keys on whether that attempt actually gets a **shell** at that provider on that host — not on the permission profile, not on a declared grant — because command execution is what makes a working-tree write or a `.git` mutation reachable. A workspace-write agent attempt that drifts parks the task; every other node class (a shell-bearing read-only attempt, an `evaluator`, a `tool`) warns and continues. The dangerous-diff gate runs on **every** path that can edit the tree, including a writing node's `hitl` round-trip, and measures from the last commit the orchestrator itself made for the task (the task's base until it makes one) — never from `HEAD`, which a commit made inside the task would empty.

## Fallback and transient-failure recovery

- Allowed **only** for infrastructure error classes.
- **Forbidden** for: failed tests/linters, review findings, incomplete fulfillment, Git errors, an invalid task/config, exhausted fix cycles, or a security violation — these route to `fixing` / `failed` / `manual_action_required`.
- Partial changes after an infrastructure error are not rolled back: a snapshot+diff is preserved, the fallback receives the current diff, and it goes through the full set of checks.
- **Symmetric cross-provider fallback**: a node on the global primary falls back to the other allowed provider (Claude↔Codex); with a single allowed provider there is no fallback. The fallback target lives in the Router; core never changes the CLI it speaks.
- **Bounded same-provider transient retry**: a raised transient infra error (provider/network unavailable — never timeout/rate-limit, never a quality failure) is retried on the same provider with bounded backoff before falling back. A quality verdict is never retried.
- **Soft, resumable pause**: when both retries and cross-provider fallback are exhausted and **any** attempt in the exhausted stage reported a park-eligible class (provider/network unavailable or rate-limited) while **no** attempt reported a containment/capability failure, the task is parked as resumable (not terminal) and resumed on the next watch tick / restart, bounded by a max-blocked timeout after which it goes terminal `failed`. The decision reads the class of **every** attempt, never only the last one: a fallback provider that fails worse than the primary must never be able to turn a park-eligible primary failure into a terminal one.
- **Frozen control plane**: at task start the orchestrator freezes the exact control inputs the flow references (flow YAML, role/supervisor prompts, and each tool's complete supported launch set — the executable plus any same-name Windows launcher/payload siblings) into a private, immutable per-task bundle and binds every runner to it — no later node reopens live `.worc`. An **agent** mutating a control file mid-run is a non-fallback, non-park `manual_action_required` (detected on the next attempt), and automatic crash-recovery over a live edit stays fail-closed the same way. The one exception is a deliberate **operator `rerun --continue`**: it **adopts** the current on-disk control plane (re-freeze + new digest) so a between-run flow/role/tool fix takes effect from the resume point onward — the operator invoked the CLI, so the edit is trusted, while agent-side in-run tamper detection is unchanged.
- **Tool verdict integrity**: a tool process that exits non-zero with empty stdout and non-empty stderr produced no quality verdict and parks at `manual_action_required` without charging a fix iteration. A repeated identical `fail` without findings from the same tool node parks on its second result before another fix charge; changed output or findings resets the guard.
- **Process-tree quiescence barrier**: every provider attempt runs inside an orchestrator-owned process-containment object; on **every** exit path the tree is proven empty before any result is trusted and before any downstream work (checks, Git, next task) runs. A tree that cannot be proven empty routes the task to `manual_action_required`. The platform-specific containment syntax lives only in the process runner.

## State machine and idempotency

- Transitions are transactional; a re-run **does not** create a second commit/push/PR.
- **The orchestrator never delegates publication to the agent:** no node is given a mandate to commit, push, or open a PR, and no product mechanism expects the agent to. Mechanical impossibility is guaranteed only where a sandbox exists, and only for the local half (`.git` and `.worc` are immutable); the remote half is held by detection on our `origin` and is not held outside it.
- **Publishing recovers, it never adopts.** What `origin` holds for the task branch decides the push: matching us (nothing sent), behind us (an ordinary push), diverged from the commit we recorded pushing (a lease-guarded force-push over our own stale push, and only that), or diverged from something we never pushed (merge it in, re-run the checks over the combination, declare the adopted commits in the PR; a conflict parks with the tree restored). A pull request is only ever written into when this orchestrator opened it. An existing remote branch or an open PR is never taken as proof of our own earlier publication.
- After a restart, the unfinished step is resumed or its result is safely reconciled.
- Human waiting does not add a task status; the registered HITL artifact is the recovery source of truth, and a timeout, transport error, or ambiguous approval fails closed to `manual_action_required`.
- Publishing happens only when checks succeed and there are no blocking findings.
- After `implementation`/`fixing`, tracked-file deletions and dependency-manifest/lock changes require approval before tests; ordinary diffs and routine commit/push/PR do not ask.
- At most one task is active at a time (a single processing slot); more than one active task on restart → `manual_action_required`.
- After a terminal status, cleanup must safely return the target repo to `base_branch` before any next task starts; ambiguous branch state forbids automatic continuation. Exception: a resumable `manual_action_required` park carrying the task's **own** uncommitted WIP intentionally leaves `HEAD` on the task branch (that WIP is its resume input, and a cleanup error must never mask the node's real stop reason); the next `new`-mode task still checks out base at branch prep, so the slot is freed either way.
- Auto mode is off by default and controls only whether the next pending task is picked after successful cleanup — it never changes the single-active-task invariant.
- Every terminal transition appends exactly one record to the append-only completed-tasks ledger; the ledger is never rewritten.
- The Git Manager uses **scoped staging** — an explicit pathspec that excludes `tasks/`/`logs/`/`workspace/`; **never `git add .`/`-A`** for a code commit. Orchestration/task artifacts never enter a code commit.
- Every automatic loop (stage attempts, fix cycles) has a configurable limit, and total fix iterations per task are bounded by a single global cap. When a loop or the cap is exhausted, the task stops in `manual_action_required` with a failure report — never an unbounded loop. `failed` is reserved for unrecoverable errors, not an exhausted fix budget.

## What must not be done

- Couple core to a specific CLI, or let it build provider-specific commands.
- Grant a provider a mandate to commit/push/PR, or build a mechanism that expects the agent to publish (see the publication invariant above — it is de jure, and the mechanical half of it holds only where a sandbox does).
- Perform fallback on a quality error.
- Change the provider route for a node that has already begun.
- Hardcode or special-case a specific flow node **id**, or make any packaged id mandatory — behavior attaches to a node kind or a declared fact.
- Hardcode anything to a specific flow, topic/domain, or task type, or assume how many flows exist.
- Run the `fixing` loop without a global per-task bound.
- Process more than one task at a time.
- Start a next task before cleanup has returned the repo to `base_branch`.
- Overwrite the original task file or rewrite the completed-tasks ledger.
- Let a task that has not passed the validation gate reach branch creation or a provider run.
- Accept a decomposition split without the deterministic rule, or run subtasks in parallel / on separate branches.
- Stage with `git add .` / `git add -A` in the target repo, or let orchestration/task artifacts enter a code commit.
- Accept a Telegram reply from a different chat/message/callback, pass an answer through CLI argv, or treat `contacts` as access control.
