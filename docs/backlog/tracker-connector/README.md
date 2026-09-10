# Tracker connector — work items in, worc tasks out

Status: **ready-to-implement** Date: 2026-09-10 Owner: Vladimir Makarevich Slug: `tracker-connector`

## Summary

A separate, optionally installed package, `worc-connect`, watches an issue tracker for one repository, turns each work item a maintainer has explicitly gated into a worc task through the ingress worc already has (`tasks/preparing/` + `worc promote`), and writes the outcome back to the tracker — a state label, a comment naming the task, the pull-request link, and the close on merge. GitHub is the first tracker, driven through the operator's own `gh` login; the connector's core knows no tracker API, so Azure DevOps, GitLab or Jira are one adapter each behind an optional dependency. Triage by an agent (analyse, reproduce, write a failing test) is an **optional** mechanic switched off in v1 — v1 exists to prove the connector mechanics fast, and it needs **no change to worc at all**. Four worc-side contract items are specified here too, none on v1's critical path: a `references:` task field that worc appends verbatim to the PR body (so a connector can say `Fixes #142` without worc learning any tracker's syntax); `pr_url` and a `rejected` section in `worc list --format json` (so the connector learns a task's PR and a gate reject's reason from the one scripting surface, never from `.worc/`); and a configurable report directory that the private report policy may point outside `.worc/`, so the optional triage flow can leave its report in the connector's own home.

The connector lives in its own repository, `VladimirMakarevich/worc-connect` (created empty and private on 2026-09-11, bootstrapped by phase 03). Until phase 03 has copied the connector half there, this folder carries the whole design; the connector phases move with it, and the worc-side phases (01, 02, 08) stay.

These documents are design detail, not an implementation contract, and must not override the hard invariants in `CLAUDE.md`, `AGENTS.md`, or `.agents/rules/`. The code remains the source of truth; this folder leaves the queue once the work lands.

## Documents

| # | Document | Purpose | Signed off |
| --- | --- | --- | --- |
| 1 | [problem.md](problem.md) | The original problem, in the user's own words | ☑ |
| 2 | [requirements.md](requirements.md) | What the connector and the worc-side contract must do | ☑ |
| 3 | [out-of-scope.md](out-of-scope.md) | What this task explicitly does **not** cover | ☑ |
| 4 | [happy-path.md](happy-path.md) | Operator-level Before/After and flow diagrams | ☑ |
| 5 | [design.md](design.md) | Technical design and the decisions behind it | ☑ |
| 6 | [acceptance-criteria.md](acceptance-criteria.md) | Testable pass/fail criteria | ☑ |
| 7 | [definition-of-done.md](definition-of-done.md) | When the task is truly finished | ☑ |
| 8 | [questions.md](questions.md) | Open questions (living) | ☑ |
| — | [plan/README.md](plan/README.md) | Phased implementation plan (worc-side phases first, then the connector) | ☑ |

## How this task is being worked

At the user's request every document was drafted in one pass, from the design conversation that preceded it, instead of one document at a time. On 2026-09-10 the documents were verified against the code (`dev` at `ea8467d`) and corrected; on 2026-09-11 the user answered the last open question, signed off every row above and set the folder to `ready-to-implement`. [questions.md](questions.md) keeps the record of every decision. Implementation order: phases 01, 02 and 08 here (independent of each other, branches off `dev`), phases 03 → 04 → 05 in the connector repository, then 06 and 07 once their dependencies have merged — see [plan/README.md](plan/README.md).

## Change log

- 2026-09-10 — spec folder scaffolded and every document drafted in one pass; branch `chore/spec-tracker-connector` off `dev`.
- 2026-09-10 — user fixed the name (`worc-connect`) and the home (`.worc-connect/`) and accepted the defaults for Q-3…Q-10; D14 added for the owner editing, retitling, reopening and merging a published PR by hand (FR-C16/FR-C17, AC-16/AC-17/AC-E9, happy-path Example 4, phase 05 steps); Q-11 opened for the follow-up-on-open-PR default.
- 2026-09-10 — pre-implementation review against the code (`dev` at `ea8467d`): D11 / phase 02 resolved (the PR URL is the completed `pr` publish-op's `result_ref`, read by the existing `_recorded_pr_url`; no new column); phase 01 pinned to `NodeInputs` / `build_node_inputs`, the gate's check order (`INVALID_REFERENCES` before the injection scan) and the reused-PR path; doc targets corrected to `packaged/guide/README.md` + `tasks/task-rich.md` (no operations page exists); `worc list --all` semantics and status labels made explicit in FR-C12 / AC-W2 / AC-12; `promote`'s stdout signal recorded; the default-gitignored lifecycle tree (`worc install`, same day) reflected in problem / D4 / happy-path; gate rejects verified to leave no `tasks` row → Q-12 opened, AC-E3 and control-flow step 4 rewritten around it. Sign-off still pending on every row.
- 2026-09-10 — user decided Q-11 (**yes**, follow-up continues the open PR) and Q-12 (**option b**): a third worc-side item, FR-W3 / D15 / AC-W3 — `worc list --format json --all` gains a `rejected` section read from the ledger, built in phase 02 and adopted by the connector in phase 06. Only Q-6 stays open (phase 07). Nothing committed; the folder awaits the user's sign-off.
- 2026-09-11 — user decided Q-6: the triage report goes to the connector's own home through worc's configurable report directory, allowed for the private policy outside `.worc/` (D16, FR-W4, AC-W4, new worc phase 08; phase 07 now depends on it; the `configurable-report-dir.md` backlog row is absorbed). User signed off every document and set the status to `ready-to-implement`. Connector repository `VladimirMakarevich/worc-connect` created (private, empty; briefly named `worc-connect-github` and renamed the same day once D1's one-repository layout — core plus adapters as extras — was reconfirmed). Nothing committed.
