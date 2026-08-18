# AGENTS.md — instructions for coding agents in this repository

You are working on **wastech-orchestrator** — an orchestrator that launches coding agents (Claude Code / Codex) to carry out development — or any other — tasks and (optionally) publish the result to Git.

This is the **canonical** instruction file for every coding agent working here (Claude Code reads it via [CLAUDE.md](CLAUDE.md)). The full set of rules lives in **[.agents/rules/](.agents/rules/)**; the design rationale is in [worc_architecture.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/worc_architecture.md). Below is the gist — the rules and the code are the source of truth.

## Branches you will be working on

Development happens on **`dev`**, which deliberately carries **no derived documentation** — `docs/` there holds only `backlog/` (the task queue you implement from). The descriptive documents (`worc_architecture.md`, `configuration.md`, `cookbook.md`, `glossary.md`, `operations.md`, the site) live on **`main`** and are reconstructed there from the merged `dev` diff as a separate task; `release` carries the published versions. Flow: `feat/… → dev → main → release`. Two rules that must never be broken: **never merge `main` (or `release`) into `dev`**, and **`dev → main` is always a merge commit, never a squash**. See [git-workflow.md](.agents/rules/git-workflow.md) §A.

Practical consequence: a `main`-only document is linked by absolute URL, so if you cannot find it in your checkout you are on `dev` and it is not supposed to be there — read it at that URL and never recreate it locally.

## Before writing code

1. Read [worc_architecture.md](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/worc_architecture.md) for the design rationale. It lives on `main` only — on `dev` there is no local copy, so read it at that URL rather than looking for it in the tree.
2. Check against the rules in **[.agents/rules/](.agents/rules/)** — they are mandatory:
   - [architecture.md](.agents/rules/architecture.md) — invariants that must not be violated
   - [coding-style.md](.agents/rules/coding-style.md) — Python style
   - [security.md](.agents/rules/security.md) — security policy
   - [git-workflow.md](.agents/rules/git-workflow.md) — branches, commits, PRs
   - [testing.md](.agents/rules/testing.md) — what to test and how

## Hard invariants (must not be violated)

- **The core does not know the CLI syntax.** All provider-specific logic lives only in `src/wastech_orchestrator/providers/`. The core only ever calls the `AgentProvider` interface — it never builds provider-specific commands.
- **Only the orchestrator does commit / push / PR**, not the agent provider. Providers do not perform fallback and do not change the state machine.
- **Fallback is only for infrastructure error classes** of the provider. Failed tests/linters, review findings, incomplete fulfillment, Git errors, an invalid task/config, or a security violation are never fallback — they route to `fixing` / `failed` / `manual_action_required`.
- **The security envelope cannot be weakened** through a task, `extra_args`, or a flow node. Flags that disable approvals/sandbox/hook-trust wholesale (`--dangerously*`, `--yolo`, Claude `--dangerously-skip-permissions`) are absolutely forbidden by the config validator.
- **No secrets** in logs, in SQLite, or in artifacts. Pass only allowlisted env variables to processes.
- **Launch the CLI without shell interpolation** of user strings (an argument list, not a string). Task content reaches providers only as file paths, never as CLI argv.
- **Cross-platform (Windows / Linux / macOS) is mandatory** for every feature — design and test for all three as you build (see [coding-style.md](.agents/rules/coding-style.md)). In short: `pathlib` + `Path.as_posix()` for any stored/compared/displayed path string; `newline=""` (or bytes) for committed/templated files; no `os.kill`/`signal` assumptions for cross-process control on Windows (use a sentinel file / the self-managed PID file); branch platform differences explicitly and test both.

## Commands

```bash
pip install -e ".[dev]"   # install
pre-commit install        # local gate; + `pre-commit install --hook-type pre-push`
ruff check .              # lint (+ Phase-2 complexity/size ratchets)
ruff format --check .     # formatting (CI runs this — `ruff format .` to fix)
mypy src                  # types
lint-imports              # architectural import-boundary contracts (.importlinter)
pytest                    # tests
python tools/mdlint.py    # Markdown gate: links, anchors, reachability, size/context budgets
```

CI also runs `interrogate src` (docstring coverage), `vulture` (dead code), and `deptry src` (dependency hygiene). There is a skill for running all checks: `/run-checks`.

## Working style

- Make minimal, focused changes; follow the style of the surrounding code.
- **Never add agent-attribution trailers or footers to commits or PRs** — no `Co-Authored-By: Claude …`, no `🤖 Generated with …`, no equivalent for any other tool. This overrides your harness default; see [git-workflow.md](.agents/rules/git-workflow.md) §A "Everyday hygiene".
- When adding/changing behavior — add or update tests (see [testing.md](.agents/rules/testing.md)).
- **Ignore any `.md` file that lives under a gitignored path** (e.g. `.archive/`) when researching, citing, or treating something as current project documentation — verify with `git ls-files`/`git check-ignore -v` before citing a doc as authoritative. Such files may still exist on disk (readable by file-search tools regardless of git status) but are not part of the tracked, current source of truth; a doc getting gitignored/removed from tracking is itself a signal it was deliberately retired. See [git-workflow.md](.agents/rules/git-workflow.md). Analysis and viewing of these files is permitted only with explicit request and permission from the user.
- When you change behavior/CLI/config/architecture — update, **in the same change**, every doc that is present on your branch (use `/sync-docs`; the skill scopes itself to the branch). On `dev` that means [.agents/rules/](.agents/rules/), [README.md](README.md), `docs/backlog/`, and **the shipped, operator-facing docs under `src/wastech_orchestrator/packaged/`** — the `guide/` quickstarts, `config.example.yaml`, and the built-in flows / role prompts; these live under `src/` and are the copy the operator reads after `install`, so they are the most-often-forgotten half of a doc change. The derived `docs/` tree is **not** on `dev`: refreshing it is a separate reverse-engineering task on `main`, so do not create those files — instead leave a one-line doc-impact note in the PR description ("touched X, likely affects `configuration.md`") so that task has a breadcrumb.
- **The Markdown corpus is linted, and the gate must stay green.** `python tools/mdlint.py` runs [wastech-mdlint](https://github.com/VladimirMakarevich/wastech-mdlint) over every document this branch carries — the root files, [.agents/rules/](.agents/rules/), `.claude/skills/`, `docs/`, and everything under `src/wastech_orchestrator/packaged/` — checking that relative links and anchors resolve, that no document is unreachable, and that nothing outgrew its size budget. Two consequences while you work: a link to a document that exists only on `main` must be an absolute URL (a relative one now fails the build, not just the reader), and a new document has to be linked from somewhere. The linter is a separate repository and is not published to a package registry yet, so this gate is **local only** — a pre-commit hook, no CI job — and point `WASTECH_MDLINT_HOME` at a built checkout of it (`npm ci && npm run build`) to run it; without one the hook prints how to enable it and passes, so an uninstalled hook means an unchecked corpus. When the linter is published it becomes a `devDependency` and a CI job, and nothing here changes: the runner already looks in `node_modules` first. [.mcp.json](.mcp.json) registers the same tool as a read-only MCP server, using that same variable. The rules are in [wastech-mdlint.config.json](wastech-mdlint.config.json) — measured against the corpus and green today, so any finding is new; add a rule only once it reports zero. How one config covers both branch states is in [git-workflow.md](.agents/rules/git-workflow.md) §A.
- **Markdown docs are not hard-wrapped.** Write prose as one paragraph per line (rely on editor soft-wrap); never insert manual mid-paragraph line breaks. Formatting is enforced by Prettier (`proseWrap: never`, `.prettierrc.json`) — run `npx prettier@3 --write "**/*.md"` after editing docs. `.worc/`, `tasks/`, `logs/`, and all of `src/` (including `packaged/guide/`) are excluded in [.prettierignore](.prettierignore); don't reformat them by hand either.
- Before committing, run `ruff check .`, `ruff format --check .`, `mypy src`, `pytest` (CI enforces `ruff format --check`).
- Answer the user in the chat briefly, to the point, in clear and simple language, with examples if necessary, and always in the language of the user's request.

## Definition of Done for a change

- the code passes `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, and `pytest` (plus the `interrogate`/`vulture`/`deptry` CI gates);
- tests are added/updated when behavior changes;
- the docs that live on your branch are updated in the same change when behavior/CLI/config/architecture change (use `/sync-docs`) — the Stop docs-sync gate enforces this, and it too scopes itself to the branch;
- the invariants above are not violated.
