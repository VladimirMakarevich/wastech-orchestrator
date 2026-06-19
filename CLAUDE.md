# CLAUDE.md — instructions for Claude Code in this repository

You are working on **wastech-orchestrator** — an orchestrator that launches coding agents (Codex / Claude Code) to carry out development tasks and publish the result to Git.

## Before writing code

1. Read the **[Functional Map](docs/functional/index.md)** (`docs/functional/`) — the canonical, code-derived reference for the system's purpose, blocks, invariants, and flows. The code in `src/wastech_orchestrator/` is the source of truth; [docs/worc_architecture.md](docs/worc_architecture.md) gives the design rationale.
2. Check against the rules in **[.agents/rules/](.agents/rules/)** — they are mandatory:
   - [architecture.md](.agents/rules/architecture.md) — invariants that must not be violated
   - [coding-style.md](.agents/rules/coding-style.md) — Python style
   - [security.md](.agents/rules/security.md) — security policy
   - [git-workflow.md](.agents/rules/git-workflow.md) — branches, commits, PRs
   - [testing.md](.agents/rules/testing.md) — what to test and how

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
- State machine statuses: see the [system flows](docs/functional/system-flows.md) and `src/wastech_orchestrator/core/state_machine.py`.

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
- For new components, check against the contracts in the [Functional Map](docs/functional/index.md) and [.agents/rules/architecture.md](.agents/rules/architecture.md).
- When adding/changing behavior — add or update tests (see .agents/rules/testing.md).
- When you change behavior/CLI/config/architecture — update the affected docs **in the same change** (use `/sync-docs`), and record deferred work in [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md). The Stop docs-sync gate enforces this.
- **Markdown docs are not hard-wrapped.** Write prose as one paragraph per line (rely on editor soft-wrap); never insert manual mid-paragraph line breaks. Formatting is enforced by Prettier (`proseWrap: never`, `.prettierrc.json`) — run `npx prettier@3 --write "**/*.md"` after editing docs. `logs/`, `tasks/`, `src/`, and `docs/worc/` are excluded (`.prettierignore`); don't reformat them.
- Before committing, run `ruff`, `mypy`, `pytest`.
- The MVP build is **complete**; the phased build docs have been removed — the [Functional Map](docs/functional/index.md) is the current code-derived reference. Track ongoing work in [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md) and the product backlog.

@RTK.md