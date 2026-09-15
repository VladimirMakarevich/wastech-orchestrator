<!-- Root of the task spec: the folder index, and the only document that links every other one.
     Created first as a scaffold, kept in sync as each document is signed off. Nothing links back to
     it — the spec's link graph points one way, so the Markdown gate's cycle rule stays quiet. -->

# {{TASK_TITLE}}

Status: **{{STATUS}}** Date: {{DATE}} Owner: {{OWNER}} Slug: `{{SLUG}}`

<!-- Status: draft → shaping → ready-to-implement → in-progress → done. Same one-line header the
     other queue documents carry. -->

## Summary

<!-- 2–4 sentences: what this task is and why it matters. Written last, once the documents below have settled. -->

These documents are design detail, not an implementation contract, and must not override the hard invariants in `CLAUDE.md`, `AGENTS.md`, or `.agents/rules/`. The code remains the source of truth; this folder leaves the queue once the work lands.

## Documents

| # | Document | Purpose | Signed off |
| --- | --- | --- | --- |
| 1 | [problem.md](problem.md) | The original problem, in the user's own words | ☐ |
| 2 | [requirements.md](requirements.md) | What the solution must do | ☐ |
| 3 | [out-of-scope.md](out-of-scope.md) | What this task explicitly does **not** cover | ☐ |
| 4 | [happy-path.md](happy-path.md) | Operator-level Before/After and flow diagrams | ☐ |
| 5 | [design.md](design.md) | Technical design and the decisions behind it | ☐ |
| 6 | [acceptance-criteria.md](acceptance-criteria.md) | Testable pass/fail criteria | ☐ |
| 7 | [definition-of-done.md](definition-of-done.md) | When the task is truly finished | ☐ |
| 8 | [questions.md](questions.md) | Open questions (living) | ☐ |
| — | [plan/README.md](plan/README.md) | Phased implementation plan | ☐ |

<!-- Mark a document ☑ only after the user has reviewed and signed off on it. -->

## How this task is being worked

Built one document at a time: the foundational documents first, the implementation plan last, each reviewed with the user before moving on. Blocking questions in [questions.md](questions.md) are resolved before the plan is signed off.

## Change log

<!-- Append one line per meaningful revision: date — what changed. -->

- {{DATE}} — spec folder scaffolded.
