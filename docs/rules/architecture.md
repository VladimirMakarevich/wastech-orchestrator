# Architecture rules (invariants)

The source of truth is [orchestrator_final_plan.md](../../orchestrator_final_plan.md). These invariants must not be violated.

## Layers and dependencies

- **Orchestrator Core** manages the sequence of stages, attempt limits, state machine transitions, and publishing conditions. Core calls **only** the `AgentProvider` interface and **does not build** provider-specific commands.
- **Provider adapters** (`CodexProvider`, `ClaudeCodeProvider`) are the only place where the syntax of a specific CLI lives. They **do not perform fallback** and **do not change the state machine**.
- **Agent Router** decides the primary/fallback for a stage, the route source (global config / task override), and availability against the allowlist.
- **Git Manager / Check Runner / State Store / Artifact Store** are separate components with narrow responsibilities.

Dependency direction: `core → router → provider(interface)`. Providers do not depend on core.

## Contracts (see spec §4.3)

- `AgentProvider`: `id`, `preflight() -> ProviderHealth`, `run(AgentRunRequest) -> AgentRunResult`.
- Each stage run is **independent** and receives all context through files/artifacts and the prompt — the vendor session is **not** a source of truth.
- The `AgentRunRequest` / `AgentRunResult` / `ProviderHealth` structures are as defined in §4.3. Do not add hidden state channels beyond them.

## Stages and routing

- Stages: `planning`, `implementation`, `testing`, `review`, `fixing`, `publishing`.
- `testing` is executed by the Check Runner; `publishing` by the Git Manager. The rest are agent-driven.
- Default route: planning/implementation/fixing → primary `claude`, fallback `codex`; review → primary `codex`, fallback `claude`.
- A task override is allowed **only**: for known stages, with a provider from `agents.allowed`, without changing security/command/credentials, and after full validation of the task before the branch is created.

## Fallback

- Allowed **only** for infrastructure error classes (see spec §7.2).
- **Forbidden** for: failed tests/linters, review findings, incomplete fulfillment of requirements despite a successful CLI run, Git errors, an invalid task/config, exhaustion of fix cycles, or a security violation. These cases → `fixing` / `failed` / `manual_action_required`.
- Partial changes made after an infrastructure error are not rolled back automatically: a snapshot+diff is preserved, the fallback receives the current diff, and it goes through the full set of checks.

## State machine and idempotency

- Transitions are transactional; a re-run **does not** create a second commit/push/PR.
- After a restart, the unfinished step is resumed or its result is safely reconciled.
- Publishing happens only when checks succeed and there are no blocking findings.
- Every automatic loop (attempts, fix cycles) has a configurable limit.

## What must not be done

- Coupling core to a specific CLI.
- Granting a provider the right to commit/push/PR.
- Performing fallback on a quality error.
- Changing the provider route retroactively for a stage that has already begun.
- Continuing work when an inconsistent branch state is detected (→ `manual_action_required`).
