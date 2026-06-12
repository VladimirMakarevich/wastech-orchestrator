# CLAUDE.md — instructions for Claude Code in this repository

You are working on **wastech-orchestrator** — an orchestrator that launches coding agents (Codex / Claude Code) to carry out development tasks and publish the result to Git.

## Before writing code

1. Read **[docs/orchestrator_final_plan.md](docs/orchestrator_final_plan.md)** — this is the canonical spec. In case of any discrepancy, it takes precedence over architecture.md.
2. Check against the rules in **[docs/rules/](docs/rules/)** — they are mandatory:
   - [architecture.md](docs/rules/architecture.md) — invariants that must not be violated
   - [coding-style.md](docs/rules/coding-style.md) — Python style
   - [security.md](docs/rules/security.md) — security policy
   - [git-workflow.md](docs/rules/git-workflow.md) — branches, commits, PRs
   - [testing.md](docs/rules/testing.md) — what to test and how

## Hard invariants (must not be violated)

- **The core does not know the CLI syntax.** All provider-specific logic lives only in `src/wastech_orchestrator/providers/`. The core only ever calls the `AgentProvider` interface.
- **Only the orchestrator does commit / push / PR**, not the agent provider. Providers do not perform fallback and do not change the state machine.
- **Fallback is only for infrastructure errors** (`binary_not_found`, `timeout`, `rate_limited`, …). Test/review errors go to the `fixing` stage, not to another provider.
- **The security policy cannot be weakened** through a task or `extra_args`. No flags that bypass sandbox/approvals.
- **No secrets** in logs, in SQLite, or in artifacts. Pass only allowlisted env variables to processes.
- **Launch the CLI without shell interpolation** of user strings (an argument list, not a string).

## Canonical names (do not invent your own)

- Providers: `codex`, `claude`.
- Stages: `refinement`, `planning`, `implementation`, `testing`, `review`, `fixing`, `summary`, `publishing`.
- Branch prefix: `agent/<task-id>-<slug>`.
- State machine statuses: see [docs/orchestrator_final_plan.md §8](docs/orchestrator_final_plan.md).

## Commands

```bash
pip install -e ".[dev]"   # install
ruff check .              # lint
mypy src                  # types
pytest                    # tests
```

There is a skill for running all checks: `/run-checks`.

## Working style

- Make minimal, focused changes; follow the style of the surrounding code.
- For new components, check against the contracts in [docs/orchestrator_final_plan.md](docs/orchestrator_final_plan.md) (§4, §7, §8).
- When adding/changing behavior — add or update tests (see docs/rules/testing.md).
- When you change behavior/CLI/config/architecture — update the affected docs and a `CHANGELOG.md` `[Unreleased]` entry **in the same change** (use `/sync-docs`), and record deferred work in [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md). The Stop docs-sync gate enforces this.
- Before committing, run `ruff`, `mypy`, `pytest`.
- The MVP build (the six phases under [docs/implementation_stages/](docs/implementation_stages/)) is **complete**; those phase docs are now a historical record. Track ongoing work in [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md) and the product backlog.
