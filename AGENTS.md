# AGENTS.md — instructions for Codex CLI in this repository

You are working on **wastech-orchestrator** — an orchestrator that launches coding agents (Codex / Claude Code) to carry out development tasks and publish the result to Git.

This file is for Codex. The full set of rules matches [CLAUDE.md](CLAUDE.md) and [docs/rules/](docs/rules/); below is the gist.

## Before writing code

1. Read **[docs/orchestrator_final_plan.md](docs/orchestrator_final_plan.md)** — the canonical spec (source of truth).
2. Follow the rules in **[docs/rules/](docs/rules/)**: `architecture.md`, `coding-style.md`, `security.md`, `git-workflow.md`, `testing.md`.

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
- Branch prefix: `agent/<task-id>-<slug>`.

## Check commands

```bash
pip install -e ".[dev]"
ruff check .
mypy src
pytest
```

## Definition of Done for a change

- the code passes `ruff`, `mypy`, `pytest`;
- tests are added/updated when behavior changes;
- the invariants above are not violated;
- there is no move to the next implementation stage without the DoD of the previous one (see spec §15–16).
