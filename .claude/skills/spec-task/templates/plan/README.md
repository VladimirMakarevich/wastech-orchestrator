<!-- The implementation plan: the whole task broken into ordered phases. This is the FINAL artifact —
     written only after the problem, requirements, design and acceptance criteria are settled. Each
     phase is its own file (01-…, 02-…) and gets its row below; a phase file nothing links to fails
     the Markdown gate. -->

# Implementation plan — {{TASK_TITLE}}

Builds [design.md](../design.md) toward [acceptance-criteria.md](../acceptance-criteria.md); the finish line is [definition-of-done.md](../definition-of-done.md).

## Strategy

<!-- One paragraph: the sequence and why it is ordered this way. Name the critical path and the
     biggest risk. Dependency, not taste, decides the order — schema and state before the code that
     reads them, a provider adapter before the flow that routes to it, the engine before the packaged
     flow that uses it. -->

## Phases

| #   | Phase                            | Delivers    | Depends on | Status |
| --- | -------------------------------- | ----------- | ---------- | ------ |
| 01  | [`<name>`](01-phase-template.md) | `<outcome>` | —          | ☐      |

<!-- Copy `01-phase-template.md` per phase, rename it `NN-<kebab-name>.md`, and update this table so
     every phase file is linked exactly once. Keep phases small and independently reviewable: each
     one leaves the suite green, the docs synced, and is a single commit. -->

## Cross-cutting

- **Branching** — a branch off `dev` (`feat/<slug>`, `fix/<slug>`, `chore/<slug>`); never commit to `dev` or `main` directly, and the PR targets `dev`. See `.agents/rules/git-workflow.md`.
- **Commits** — atomic, imperative subject, scoped staging only (never `git add .`), and **no agent-attribution trailer or footer**.
- **Gates per phase** — `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest`, plus `python tools/mdlint.py` when Markdown changed.
- **Tests** — new or changed behavior ships with tests; provider, router and pipeline coverage uses deterministic fake CLIs.
- **Docs** — the documents on this branch are updated in the same phase as the code, including the shipped operator-facing copies under `src/wastech_orchestrator/packaged/`.
- **Invariants** — no phase weakens the security envelope, teaches the core a provider's CLI syntax, or hands a node a publication mandate.

## Risks & mitigations

| Risk | Likelihood | Mitigation |
| ---- | ---------- | ---------- |
|      |            |            |
