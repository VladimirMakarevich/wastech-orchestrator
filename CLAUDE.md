# CLAUDE.md — instructions for Claude Code in this repository

You are working on **wastech-orchestrator** — an orchestrator that launches coding agents (Codex / Claude Code) to carry out development (or any other) tasks and publish (optional) the result to Git.

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
- **Cross-platform (Windows / Linux / macOS) is mandatory** for every feature — design and test for all three as you build (see [.agents/rules/coding-style.md](.agents/rules/coding-style.md)). In short: `pathlib` + `Path.as_posix()` for any stored/compared/displayed path string; `newline=""` (or bytes) for committed/templated files; no `os.kill`/`signal` assumptions for cross-process control on Windows (use a sentinel file / the self-managed PID file); branch platform differences explicitly and test both.

## Canonical names (do not invent your own)

- Providers: `codex`, `claude`.
- Default-flow node ids: `refinement`, `planning`, `implementation`, `testing`, `review`, `fixing`, `publish` (the packaged `implementation` flow; the whole-task `summary` is the supervisor layer, not a node). These are flow node ids, not a `Stage` type — there is no `Stage` enum.
- Default task branch: `repo.branch_prefix/<task-id>-<slug>` (`worc/...` by default); task `branch_name` may override the full branch name after validation.
- State machine statuses: see the [system flows](docs/functional/system-flows.md) and `src/wastech_orchestrator/core/state_machine.py`.

## Commands

```bash
pip install -e ".[dev]"   # install
pre-commit install        # local gate; + `pre-commit install --hook-type pre-push`
ruff check .              # lint (+ Phase-2 complexity/size ratchets)
ruff format --check .     # formatting (CI runs this — `ruff format .` to fix)
mypy src                  # types
lint-imports              # architectural import-boundary contracts (.importlinter)
pytest                    # tests
```

CI also runs `interrogate src` (docstring coverage), `vulture` (dead code), and `deptry src` (dependency hygiene). There is a skill for running all checks: `/run-checks`.

## Working style

- Make minimal, focused changes; follow the style of the surrounding code.
- When adding/changing behavior — add or update tests (see .agents/rules/testing.md).
- **Ignore any `.md` file that lives under a gitignored path** (e.g. `.archive/`) when researching, citing, or treating something as current project documentation — verify with `git ls-files`/`git check-ignore -v` before citing a doc as authoritative. Such files may still exist on disk (readable by file-search tools regardless of git status) but are not part of the tracked, current source of truth; a doc getting gitignored/removed from tracking is itself a signal it was deliberately retired. See [git-workflow.md](.agents/rules/git-workflow.md). Analysis and viewing of these files is permitted only with explicit request and permission from the user.
- When you change behavior/CLI/config/architecture — update the affected docs **in the same change** (use `/sync-docs`). **Doc-sync includes the shipped, operator-facing docs under `src/wastech_orchestrator/packaged/`** — the `guide/` quickstarts, `config.example.yaml`, and the built-in flows / role prompts — not just `docs/`; these live under `src/` and are the copy the operator reads after `install`, so they are the most-often-forgotten half of a doc change.
- **Markdown docs are not hard-wrapped.** Write prose as one paragraph per line (rely on editor soft-wrap); never insert manual mid-paragraph line breaks. Formatting is enforced by Prettier (`proseWrap: never`, `.prettierrc.json`) — run `npx prettier@3 --write "**/*.md"` after editing docs. `logs/`, `tasks/`, `src/`, and `packaged/guide/` are excluded (`.prettierignore`); don't reformat them.
- Before committing, run `ruff check .`, `ruff format --check .`, `mypy src`, `pytest` (CI enforces `ruff format --check`).
- Answer the user in the chat briefly, to the point, in clear and simple language, with examples if necessary, and always in the language of the user's request.

@RTK.md
