# Tracker connector — work items in, worc tasks out

Status: **shaping** Date: 2026-09-10 Owner: Vladimir Makarevich Slug: `tracker-connector`

## Summary

A separate, optionally installed package, `worc-connect`, watches an issue tracker for one repository, turns each work item a maintainer has explicitly gated into a worc task through the ingress worc already has (`tasks/preparing/` + `worc promote`), and writes the outcome back to the tracker — a state label, a comment naming the task, the pull-request link, and the close on merge. GitHub is the first tracker, driven through the operator's own `gh` login; the connector's core knows no tracker API, so Azure DevOps, GitLab or Jira are one adapter each behind an optional dependency. Triage by an agent (analyse, reproduce, write a failing test) is an **optional** mechanic switched off in v1 — v1 exists to prove the connector mechanics fast, and it needs **no change to worc at all**. Two small worc-side contract items are specified here too, as v1.1 conveniences: a `references:` task field that worc appends verbatim to the PR body (so a connector can say `Fixes #142` without worc learning any tracker's syntax), and `pr_url` in `worc list --format json`.

The connector itself will live in its own repository. Until that repository exists this folder carries the whole design; the connector phases move with it, and the worc-side phases stay.

These documents are design detail, not an implementation contract, and must not override the hard invariants in `CLAUDE.md`, `AGENTS.md`, or `.agents/rules/`. The code remains the source of truth; this folder leaves the queue once the work lands.

## Documents

| # | Document | Purpose | Signed off |
| --- | --- | --- | --- |
| 1 | [problem.md](problem.md) | The original problem, in the user's own words | ☐ |
| 2 | [requirements.md](requirements.md) | What the connector and the worc-side contract must do | ☐ |
| 3 | [out-of-scope.md](out-of-scope.md) | What this task explicitly does **not** cover | ☐ |
| 4 | [happy-path.md](happy-path.md) | Operator-level Before/After and flow diagrams | ☐ |
| 5 | [design.md](design.md) | Technical design and the decisions behind it | ☐ |
| 6 | [acceptance-criteria.md](acceptance-criteria.md) | Testable pass/fail criteria | ☐ |
| 7 | [definition-of-done.md](definition-of-done.md) | When the task is truly finished | ☐ |
| 8 | [questions.md](questions.md) | Open questions (living) | ☐ |
| — | [plan/README.md](plan/README.md) | Phased implementation plan (worc-side phases first, then the connector) | ☐ |

## How this task is being worked

At the user's request every document was drafted in one pass, from the design conversation that preceded it, instead of one document at a time. Sign-off is therefore still pending on every row above, and the questions the draft could not settle are collected in [questions.md](questions.md) — none of them is blocking, but each one is a decision the user owns. The folder's status becomes `ready-to-implement` once the user has reviewed the documents and the open questions are answered.

## Change log

- 2026-09-10 — spec folder scaffolded and every document drafted in one pass; branch `chore/spec-tracker-connector` off `dev`.
- 2026-09-10 — user fixed the name (`worc-connect`) and the home (`.worc-connect/`) and accepted the defaults for Q-3…Q-10; D14 added for the owner editing, retitling, reopening and merging a published PR by hand (FR-C16/FR-C17, AC-16/AC-17/AC-E9, happy-path Example 4, phase 05 steps); Q-11 opened for the follow-up-on-open-PR default.
- 2026-09-10 — pre-implementation review against the code (`dev` at `ea8467d`): D11 / phase 02 resolved (the PR URL is the completed `pr` publish-op's `result_ref`, read by the existing `_recorded_pr_url`; no new column); phase 01 pinned to `NodeInputs` / `build_node_inputs`, the gate's check order (`INVALID_REFERENCES` before the injection scan) and the reused-PR path; doc targets corrected to `packaged/guide/README.md` + `tasks/task-rich.md` (no operations page exists); `worc list --all` semantics and status labels made explicit in FR-C12 / AC-W2 / AC-12; `promote`'s stdout signal recorded; the default-gitignored lifecycle tree (`worc install`, same day) reflected in problem / D4 / happy-path; gate rejects verified to leave no `tasks` row → Q-12 opened, AC-E3 and control-flow step 4 rewritten around it. Sign-off still pending on every row.
- 2026-09-10 — user decided Q-11 (**yes**, follow-up continues the open PR) and Q-12 (**option b**): a third worc-side item, FR-W3 / D15 / AC-W3 — `worc list --format json --all` gains a `rejected` section read from the ledger, built in phase 02 and adopted by the connector in phase 06. Only Q-6 stays open (phase 07). Nothing committed; the folder awaits the user's sign-off.
