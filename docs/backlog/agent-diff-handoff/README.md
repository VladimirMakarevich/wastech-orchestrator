<!-- Root of the task spec: the folder index, and the only document that links every other one.
     Created first as a scaffold, kept in sync as each document is signed off. Nothing links back to
     it — the spec's link graph points one way, so the Markdown gate's cycle rule stays quiet. -->

# Agent diff hand-off

Status: **in-progress** Date: 2026-10-19 Owner: Vladimir Makarevich Slug: `agent-diff-handoff`

<!-- Status: draft → shaping → ready-to-implement → in-progress → done. Same one-line header the
     other queue documents carry. -->

## Summary

Every node after the first editing pass is pointed at one file — the task's whole cumulative diff, often tens or hundreds of KB — and told nothing else about the change, not even where it starts; a node with a shell then reads it **and** inspects the tree with git on top (32 of 32 measured Claude reviewers). This task keeps `current.diff` exactly as the core needs it and adds, from the same snapshot, a bounded **change overview** and the exact **base SHA**, plus a one-line where-to-start sentence chosen by whether every provider on the node's route grants a shell: start from the overview and inspect files against the base, or read the full diff with the overview as its map. Six phases, bottom-up, with one behavior-changing phase (05).

These documents are design detail, not an implementation contract, and must not override the hard invariants in `CLAUDE.md`, `AGENTS.md`, or `.agents/rules/`. The code remains the source of truth; this folder leaves the queue once the work lands.

## Documents

| # | Document | Purpose | Signed off |
| --- | --- | --- | --- |
| 1 | [problem.md](problem.md) | The original problem, in the user's own words | ☑ |
| 2 | [requirements.md](requirements.md) | What the solution must do | ☑ |
| 3 | [out-of-scope.md](out-of-scope.md) | What this task explicitly does **not** cover | ☑ |
| 4 | [happy-path.md](happy-path.md) | Operator-level Before/After and flow diagrams | ☑ |
| 5 | [design.md](design.md) | Technical design and the decisions behind it | ☑ |
| 6 | [acceptance-criteria.md](acceptance-criteria.md) | Testable pass/fail criteria | ☑ |
| 7 | [definition-of-done.md](definition-of-done.md) | When the task is truly finished | ☑ |
| 8 | [questions.md](questions.md) | Open questions (living) | ☑ |
| — | [plan/README.md](plan/README.md) | Phased implementation plan | ☑ |

<!-- Mark a document ☑ only after the user has reviewed and signed off on it. -->

## How this task is being worked

Built one document at a time: the foundational documents first, the implementation plan last, each reviewed with the user before moving on. Blocking questions in [questions.md](questions.md) are resolved before the plan is signed off.

## Change log

<!-- Append one line per meaningful revision: date — what changed. -->

- 2026-10-19 — spec folder scaffolded.
- 2026-10-19 — problem, requirements, out-of-scope, happy path, design, acceptance criteria, definition of done signed off; Q-1/Q-2 resolved by D-5/D-6; plan drafted (six phases).
- 2026-10-19 — plan signed off; status ready-to-implement.
