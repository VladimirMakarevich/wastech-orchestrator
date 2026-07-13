# AGENTS.md — instructions for Codex CLI in this repository

You are working on **wastech-orchestrator** — an orchestrator that launches coding agents (Codex / Claude Code) to carry out development tasks and publish the result to Git.

This file is for Codex. The full set of rules matches [CLAUDE.md](CLAUDE.md) and [.agents/rules/](.agents/rules/); below is the gist.

## Before writing code

1. Read the **[Functional Map](docs/functional/index.md)** (`docs/functional/`) — the code-derived reference (source of truth is the code; [docs/worc_architecture.md](docs/worc_architecture.md) gives the design rationale).
2. Follow the rules in **[.agents/rules/](.agents/rules/)**: `architecture.md`, `coding-style.md`, `security.md`, `git-workflow.md`, `testing.md`.

## Hard invariants (must not be violated)

- **The core does not know the CLI syntax.** Provider-specific logic lives only in `src/wastech_orchestrator/providers/`. The core only ever calls `AgentProvider`.
- **Only the orchestrator does commit / push / PR**, not the provider.
- **Fallback is only for infrastructure errors** of the provider. Test/review errors → the `fixing` stage.
- **The security policy cannot be weakened** through a task or `extra_args`; no flags that bypass sandbox/approvals.
- **Secrets** do not end up in logs, SQLite, or artifacts. Processes get only allowlisted env.
- **Call the CLI with an argument list**, without shell interpolation of user strings.

## Canonical names

- Providers: `codex`, `claude`.
- Stages: `refinement`, `planning`, `implementation`, `testing`, `review`, `fixing`, `summary`, `publishing`.
- Default task branch: `repo.branch_prefix/<task-id>-<slug>` (`worc/...` by default); task `branch_name` may override the full branch name after validation.

## Check commands

```bash
pip install -e ".[dev]"
pre-commit install      # local gate; + `pre-commit install --hook-type pre-push`
ruff check .
ruff format --check .   # CI runs this; use `ruff format .` to fix
mypy src
lint-imports            # architectural import contracts (.importlinter)
pytest
# further CI gates: interrogate src · vulture · deptry src
```

## Definition of Done for a change

- the code passes `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, and `pytest` (plus the `interrogate`/`vulture`/`deptry` CI gates);
- tests are added/updated when behavior changes;
- docs are updated in the same change when behavior/CLI/config/architecture change (use `/sync-docs`), and deferred work is recorded in [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md) — the Stop docs-sync gate enforces this;
- the invariants above are not violated.

The MVP build is complete; the phased build docs have been removed — the [Functional Map](docs/functional/index.md) is the current code-derived reference. Track ongoing work in [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md) and the product backlog.

@RTK.md
