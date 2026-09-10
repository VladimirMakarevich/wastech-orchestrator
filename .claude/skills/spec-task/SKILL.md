---
name: spec-task
description: Turn a rough idea or task into a structured, reviewed spec folder under docs/backlog/<slug>/ — worked through WITH the user one document at a time, from the raw problem down to a phased implementation plan. Scaffolds problem / requirements / out-of-scope / happy-path / design / acceptance-criteria / definition-of-done / open-questions docs plus a plan/ folder split into numbered phases, all linked from the folder README. Interactive and sequential: drafts each file, refines it with the user, gets sign-off, then moves to the next — foundational docs first, the implementation plan last. Use when the user says "spec out X", "shape this task", "let's plan feature Y properly", "разложи задачу", or hands over an idea that needs discovery before coding. Produces documentation only — never code, and never the implementation itself.
---

# spec-task

Takes a task or a raw idea and turns it into a **spec folder** under `docs/backlog/<slug>/` — a set of documents built **collaboratively with the user, one file at a time**, from the original problem down to a phased implementation plan.

This skill has exactly one job: **granular documentation for one task**. It writes documents, never code, and it never starts the implementation — the finished plan is the handoff.

The spec lands in the repository's own task queue: `docs/backlog/` is the only `docs/` tree that exists on `dev`, and a spec folder is a queue item carrying its design detail — the same shape as the campaign folder already there. It is an inventory entry, **not an implementation contract**: the code stays the source of truth, the documents must never override a hard invariant, and the folder **leaves the queue once the work lands** (deleted, or archived in one dated batch — see [docs/backlog/README.md](../../../docs/backlog/README.md)).

## When to use / skip

- **Use** for anything non-trivial, fuzzy, or multi-part that deserves discovery before code: a new subsystem, a reworked flow or provider surface, a config/schema change, a vague idea to explore.
- **Skip** for a small, obvious change (a flag rename, a one-field fix, a doc typo) — don't scaffold a nine-document folder for a one-liner. Skip it too when a backlog item already carries its own design detail: it has been specced, don't re-spec it.

## How to converse

- **Speak in the user's language** (default to the language they wrote in). Write the spec documents in the project's doc language (English), and keep identifiers, node ids, config keys, file paths, and branch names in English regardless.
- **Interactive and sequential — never dump all files at once.** Draft one document, show it, refine it with the user, get an explicit sign-off, then move to the next. The user is a co-author, not the reviewer of a finished pile.
- Ask the sharp questions as they arise and log them in `questions.md`. Resolve every **blocking** question before the plan.
- Keep the running commentary tight. Between documents, say what is done and what is next in a line.

## Kickoff

1. **Capture the input.** Take `args` as the idea/task. If nothing was given, ask what the task is before scaffolding.
2. **Agree the slug.** Propose a kebab-case slug in the style the queue already uses (`configurable-report-dir`, `upgrade-flows`) and confirm it. The folder is `docs/backlog/<slug>/`.
3. **Scaffold from templates.** Copy this skill's [templates/](templates/README.md) into `docs/backlog/<slug>/`, filling `{{TASK_TITLE}}`, `{{SLUG}}`, `{{DATE}}` (today), `{{STATUS}}` (start `draft`), `{{OWNER}}`. Keep `plan/01-phase-template.md` until you create real phases, then rename it per phase (`01-<name>.md`, `02-<name>.md`, …). Confirm the folder exists, then start filling documents in order.
4. **Register the folder.** Add one row to [docs/backlog/README.md](../../../docs/backlog/README.md) linking `<slug>/README.md`, the way the existing campaign folder is listed. An unlinked document fails the Markdown gate, and an item nothing links to is an item nobody picks up.
5. **Branching, if this gets committed.** Spec docs belong on a branch off `dev` (`chore/spec-<slug>`, or the feature's own `feat/<slug>`) — never commit to `dev` or `main` directly, and commit/push/PR only when the user asks. See [git-workflow.md](../../../.agents/rules/git-workflow.md).

## The flow — one document at a time

Fill **foundational docs first, the plan last**. For each: draft → review with the user → sign off (tick it ☑ in the folder `README.md`) → next. `README.md` and `questions.md` are **living** — update them throughout, not once.

1. **`problem.md`** — capture the original problem faithfully, in the user's framing. No solutioning yet. Name whose problem it is: the operator running `worc`, an agent executing a node, or a maintainer of this repository.
2. **`requirements.md`** — derive verifiable FR-/NFR- from the problem. Behavior and outcomes, not implementation. Flag every versioned surface the task touches (the `config.yaml` schema, `state.db`, the flow schema, task-file fields, the packaged operator guide).
3. **`out-of-scope.md`** — fence the scope: what is excluded, deferred, or an explicit non-goal. Fight creep here; this is a greenfield MVP and YAGNI wins.
4. **`happy-path.md`** — make it concrete for a non-technical reader: what the operator types, what the run does, what lands. Before/After, plain language, small Mermaid diagrams. If you cannot tell the story simply, the requirements are not ready — loop back.
5. **`design.md`** — the technical HOW across the layers (`cli/` → `core/` → `providers/` → `git/` → `storage/`), honoring the invariants below, with the heavy decisions recorded inline (context, options, why).
6. **`acceptance-criteria.md`** — testable Given/When/Then per requirement, plus edge/error cases and NFR checks, each with a verification method (unit test, fake-CLI integration test, a real run, a gate).
7. **`definition-of-done.md`** — the project's gates plus the task-specific ones.
8. **`questions.md`** — keep current the whole time; before the plan is signed off, every **blocking** question must be resolved.
9. **`plan/`** — **last.** Break the work into ordered phases (`plan/README.md` plus one `NN-<name>.md` each), sequenced by dependency: schema and state before the code that reads them, a provider adapter before the flow that routes to it, the engine before the packaged flow that uses it. Each phase is small, leaves the suite green and the docs synced, and is one commit. This is the handoff to whoever implements it.

Keep the folder `README.md` in sync as you go: flip each document's status, write the Summary once the docs settle, append to the change log.

## Respect the hard invariants

`design.md` and every plan phase must conform to [AGENTS.md](../../../AGENTS.md) and the rules in [.agents/rules/](../../../.agents/rules/architecture.md). The spec **plans within** these — it does not get to wish them away:

- **The core knows no CLI syntax.** Provider-specific logic lives only in `src/wastech_orchestrator/providers/`; the core calls the `AgentProvider` interface. See [architecture.md](../../../.agents/rules/architecture.md).
- **The orchestrator never delegates publication** — no node is given a mandate to commit, push, or open a PR, and no mechanism expects the agent to.
- **Fallback is only for a provider's infrastructure error classes.** Failed checks, review findings, incomplete work, Git errors, an invalid task or config, or a security violation route to `fixing` / `failed` / `manual_action_required` — never to fallback. Providers perform no fallback and do not touch the state machine.
- **The security envelope cannot be weakened** by a task, by `extra_args`, or by a flow node — at any value of `security.strict_isolation`. See [security.md](../../../.agents/rules/security.md).
- **No secrets** in logs, in SQLite, or in artifacts; only allowlisted env vars reach a process.
- **Launch CLIs without shell interpolation** — an argv list, never a string; task content reaches a provider as a file path.
- **Cross-platform (Windows / Linux / macOS) is mandatory** for every feature, by construction and in the tests. See [coding-style.md](../../../.agents/rules/coding-style.md).
- **Canonical names only** — do not invent providers, node ids, statuses, config keys, or branch schemes; check them against the code.
- New or changed behavior ships with tests (see [testing.md](../../../.agents/rules/testing.md)), and provider/router/pipeline coverage uses fake CLIs, never the real ones.

Bias the design and the plan toward **minimal, reversible** slices. Greenfield MVP, no deployed installs: challenge every migration or back-compat step the task did not ask for. KISS > YAGNI > DRY.

## Keep the Markdown gate green

The spec folder is inside the linted corpus (`python tools/mdlint.py`), so shape it correctly as you write:

- **Links point one way only.** The folder `README.md` links every document; documents link _backwards_ along the chain (`design.md` → `requirements.md` → `problem.md`) and never back to the index. A "back to index" breadcrumb closes a reference cycle, and cycles are an error.
- **Every document needs an incoming link** — the folder `README.md` for the spec docs, `plan/README.md` for the phases, and [docs/backlog/README.md](../../../docs/backlog/README.md) for the folder itself.
- **Name the root docs and the rules in plain backticks** from inside the spec (for example `.agents/rules/security.md`) unless you verify a relative path — from `docs/backlog/<slug>/` the repository root is three levels up, and from `plan/` it is one level deeper.
- **Prose is not hard-wrapped** — one paragraph per line; run `npx prettier@3 --write "docs/backlog/<slug>/**/*.md"` after editing.
- Run `python tools/mdlint.py` after scaffolding and again when the plan is done.

## Definition of done (for the spec itself)

- `docs/backlog/<slug>/` exists with every document filled (no leftover placeholder text) and each signed off ☑ in the folder `README.md`.
- No **blocking** open question remains.
- `plan/README.md` lists ordered phases with dependencies, each honoring the invariants above and ready to be implemented.
- The folder `README.md` Summary and document table are accurate, and `Status` reflects reality (`ready-to-implement` once the plan is signed off).
- The folder is linked from [docs/backlog/README.md](../../../docs/backlog/README.md), and `python tools/mdlint.py` is green.

## What not to do

- Don't generate all documents in one shot — co-author them one at a time, with sign-off.
- Don't write the plan before the problem, the requirements, and the design are settled.
- Don't smuggle a solution into `problem.md`.
- Don't plan anything that violates a hard invariant to make the task easier — raise the conflict instead; the rules win.
- Don't write code, run the suite, or start the implementation — this skill produces the spec and stops there.
- Don't leave placeholder tokens (`{{…}}`) or empty template sections in a document you have marked done.
- Don't create the derived documentation pages (`worc_architecture.md`, `configuration.md`, …) — they live on the documentation branch; record the doc impact in the spec instead.
