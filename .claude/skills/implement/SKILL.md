---
name: implement
description: Implement a task in wastech-orchestrator end to end — a backlog ADR (docs/backlog/), a follow-up, or a free-form request — driving it through the project's own workflow (understand → shape → plan → build → check → sync docs → review → commit) while enforcing the hard invariants. Use when the user says "implement X", "build the ADR for Y", "do the backlog item Z", or hands you a task to carry out in this repo. Orchestrates the sibling skills (/clarify-task, /assess-refactor, /simplify-task, /run-checks, /sync-docs, /simplify-review); it does not replace them.
---

# implement

This skill carries one task in **wastech-orchestrator** from a starting point to a committed, checked, doc-synced change. It is the spine that connects the other skills in the right order and enforces the repo's hard invariants at each step. It writes code — but only after the task is understood, shaped, and planned.

Take `args` as the task pointer: a backlog file (`docs/backlog/<slug>.md`), a `follow_ups.md` entry, a spec section, or a free-form description. If nothing is given, ask what to implement before starting.

Speak in the **user's language** (default to the language they wrote in). Keep the running commentary tight; this is execution, not a report.

## Hard invariants — never violate these (from [CLAUDE.md](../../../CLAUDE.md) and [.agents/rules/](../../../.agents/rules/))

Re-read them if the task touches any of these areas:

- **Core knows no CLI syntax.** Provider-specific logic lives only in `src/wastech_orchestrator/providers/`; the core only calls the `AgentProvider` interface. See [architecture.md](../../../.agents/rules/architecture.md).
- **Only the orchestrator does commit / push / PR** — never the provider. Providers do no fallback and do not touch the state machine.
- **No secrets** in logs, SQLite, or artifacts. Pass only allowlisted env vars to processes. See [security.md](../../../.agents/rules/security.md).
- **Launch CLIs without shell interpolation** — an argv list, not a string.
- **Cross-platform (Windows / Linux / macOS) is mandatory** for every feature: `pathlib` + `Path.as_posix()` for stored/compared/displayed paths; `newline=""` (or bytes) for committed/templated files; no `os.kill`/`signal` assumptions for cross-process control on Windows. Branch platform differences explicitly and test all three.
- **Canonical names only** — don't invent providers, node ids, statuses, or branch schemes. There is no `Stage` enum.

Bias: greenfield MVP, no deployed installs — **challenge migration/back-compat machinery**. KISS > YAGNI > DRY.

## The flow

Run these phases in order. Skip a phase only when it plainly doesn't apply, and say so in one line rather than skipping silently. Don't re-do work the user already did in this conversation.

### 1. Understand the task (WHAT)

- Read the source of truth: the backlog/ADR file, the linked spec sections, the relevant part of the [Functional Map](../../../docs/functional/index.md), and the code the task names.
- If the goal, scope, or boundaries are fuzzy or contradictory, run **`/clarify-task`** and resolve it with the user before touching code. Don't guess your way past ambiguity.
- Restate in 2–3 lines: the goal, what's explicitly out of scope, and the acceptance signal (how we'll know it's done).

### 2. Judge the ground and shape the change (design)

- **`/assess-refactor`** — is the code the feature lands in healthy enough to build on, or does a bounded, behavior-preserving refactor come first? Keep any refactor as its own commit ahead of the feature.
- **`/simplify-task`** — find the simplest sound approach for the agreed task; strip speculative generality, premature config/abstraction, and back-compat scaffolding before writing anything.

### 3. Analyze in depth and draft the implementation plan (pre-build gate)

Turn the understanding and the design judgments into an explicit, written deliverable **before touching code** — this is the pre-step that gates the build:

- **Detailed analysis** — the current behavior and the exact code paths/seams the change touches; the data, state, and config surfaces affected (and the `config`/`state.db` version bumps their shape forces); the cross-platform (Windows / Linux / macOS) implications; the hard invariants the change interacts with; and the risks or open questions. Read the code deeply enough that the plan below rests on what the code actually does, not on assumptions.
- **Implementation plan** — an ordered, concrete step list: the modules/functions to add or change, the tests to add or update (and the `/fake-cli` fixtures if provider/router/pipeline coverage is needed), the docs to sync, and the commit boundaries. Track the steps with `TodoWrite`.
- **Confirm before building** — for a non-trivial or multi-part task, present the analysis and plan to the user and get a go-ahead before writing code; fold their corrections back into the plan. For a trivial task, a one-line plan is enough — say so and proceed. Don't start building until the plan is drafted.

### 4. Branch (see [git-workflow.md](../../../.agents/rules/git-workflow.md))

- Develop the orchestrator itself on a branch off `main`: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, or `chore/<slug>`. **Never commit to `main` directly** unless the user explicitly asks and it's already the working branch.
- If the task is a phase of an existing epic branch (check memory / the user), land the phase there instead — one squashed commit per phase.

### 5. Build

- Make minimal, focused changes that match the surrounding code's style, naming, and comment density (see [coding-style.md](../../../.agents/rules/coding-style.md)).
- Add or update tests as you go (see [testing.md](../../../.agents/rules/testing.md)). For provider/router/pipeline integration coverage, use **`/fake-cli`** to scaffold deterministic fake binaries and fixtures — never call the real Codex/Claude CLIs in tests.
- Keep every feature cross-platform by construction, not as an afterthought.

### 6. Sync docs (same change as the code)

- When behavior, the CLI, config, or architecture changes, run **`/sync-docs`** to bring the affected docs (README, operations, configuration, cookbook, architecture, functional map) in line, and record any deferred work in [follow_ups.md](../../../docs/backlog/follow_ups.md). The Stop docs-sync gate enforces this.
- Markdown is not hard-wrapped (one paragraph per line); run `npx prettier@3 --write "**/*.md"` after editing docs.
- If you implemented a backlog ADR, update its status and move/link it per [docs/backlog/README.md](../../../docs/backlog/README.md) (open item → archived when done).

### 7. Check

- Run **`/run-checks`** (ruff + mypy + pytest) — the full pre-commit gate. During the build loop, **`/test`** is the faster targeted/last-failed runner. The suite must be green before commit.

### 8. Review the diff

- Run **`/simplify-review`** to catch complexity nobody asked for in the diff (speculative generality, premature abstraction, back-compat scaffolding, over-broad error handling, dead code). Fix what it flags.

### 9. Commit (only when asked, or when the user's request clearly implies it)

- Atomic commit, imperative subject (`Add provider health preflight`). One squashed commit per phase for epic work.
- **Scoped staging only** — stage the intended paths explicitly; **never `git add .` / `-A`**. Never commit `config.yaml`, `*.db`, `logs/`, `workspace/`, `tasks/…`, `.venv/`, or secrets.
- Push / open a PR only when the user asks. PR into `main`, never push to `main` directly.

## Output

As you finish, report concisely in the user's language: what was built, the files/tests touched, docs synced, check results (state them honestly — if something failed or was skipped, say so with the output), and any deferred work recorded in `follow_ups.md`. If a decision genuinely belongs to the user (a scope cut, a risk trade-off, an open sub-decision), surface it with options rather than deciding silently.

## What not to do

- Don't start coding before the task is understood — route fuzziness to `/clarify-task`.
- Don't start building before the implementation plan is drafted (and, for a non-trivial task, confirmed with the user) — that pre-build analysis-and-plan gate is not optional.
- Don't re-implement what the sibling skills do; invoke them.
- Don't violate a hard invariant to make the task easier — raise the conflict instead.
- Don't add back-compat/migration machinery, config knobs, or abstraction layers the task didn't ask for (greenfield MVP; KISS).
- Don't skip docs or tests to "finish faster" — the gates will (and should) stop you.
- Don't commit, push, or open a PR unless the user asked or clearly implied it; never touch `main` directly.
- Don't invent canonical names, statuses, or a `Stage` enum.
