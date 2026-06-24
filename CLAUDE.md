# CLAUDE.md — instructions for Claude Code in this repository

You are working on **wastech-orchestrator** — an orchestrator that launches coding agents (Codex / Claude Code) to carry out development tasks and publish the result to Git.

## Before writing code

1. Check against the rules in **[.agents/rules/](.agents/rules/)** — they are mandatory:
   - [architecture.md](.agents/rules/architecture.md) — invariants that must not be violated
   - [coding-style.md](.agents/rules/coding-style.md) — Python style
   - [security.md](.agents/rules/security.md) — security policy
   - [git-workflow.md](.agents/rules/git-workflow.md) — branches, commits, PRs
   - [testing.md](.agents/rules/testing.md) — what to test and how

## Hard invariants (must not be violated)

- **The core does not know the CLI syntax.** All provider-specific logic lives only in `src/wastech_orchestrator/providers/`. The core only ever calls the `AgentProvider` interface.
- **Only the orchestrator does commit / push / PR**, not the agent provider. Providers do not perform fallback and do not change the state machine.
- **No secrets** in logs, in SQLite, or in artifacts. Pass only allowlisted env variables to processes.
- **Launch the CLI without shell interpolation** of user strings (an argument list, not a string).

## Canonical names (do not invent your own)

- Providers: `codex`, `claude`.
- Default-flow node ids: `refinement`, `planning`, `implementation`, `testing`, `review`, `fixing`, `publish` (the packaged `implementation` flow; the whole-task `summary` is the supervisor layer, not a node). These are flow node ids, not a `Stage` type — there is no `Stage` enum.
- Default task branch: `repo.branch_prefix/<task-id>-<slug>` (`worc/...` by default); task `branch_name` may override the full branch name after validation.
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
- When adding/changing behavior — add or update tests (see .agents/rules/testing.md).
- When you change behavior/CLI/config/architecture — update the affected docs **in the same change** (use `/sync-docs`), and record deferred work in [docs/backlog/follow_ups.md](docs/backlog/follow_ups.md). The Stop docs-sync gate enforces this.
- **Markdown docs are not hard-wrapped.** Write prose as one paragraph per line (rely on editor soft-wrap); never insert manual mid-paragraph line breaks. Formatting is enforced by Prettier (`proseWrap: never`, `.prettierrc.json`) — run `npx prettier@3 --write "**/*.md"` after editing docs. `logs/`, `tasks/`, `src/`, and `docs/worc/` are excluded (`.prettierignore`); don't reformat them.
- Before committing, run `ruff`, `mypy`, `pytest`.

@RTK.md
