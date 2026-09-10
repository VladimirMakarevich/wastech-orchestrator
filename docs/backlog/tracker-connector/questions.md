# Open questions — Tracker connector

Living document. The draft was written in one pass, so every question carried the default the design assumed; on 2026-09-10 the user answered Q-1 and Q-2 explicitly and accepted the recorded default for every other question then open. Two questions remain, neither blocking.

## Open

| # | Question | Default assumed in the design | Blocking? | Raised in | Owner |
| --- | --- | --- | --- | --- | --- |
| Q-6 | Triage report channel (phase 07): `private_control_workspace_report` lands under `.worc/`, which the connector should not read as a contract; `repository_document` lands in `docs/research/<task_id>/` and is committed and pushed. Neither is a clean fit; the "configurable report directory" backlog item would let the flow name a directory the connector owns. | undecided — decided in phase 07 | no (triage is off in v1) | design D12 | spec |
| Q-11 | When an item is re-triggered while the previous task's PR is **still open** (the owner asked for more on the same issue), should the follow-up task continue on the same branch and PR (`branch_mode: existing` + `branch_ref`, which worc's publishing reuses and whose body it appends to) instead of opening a second PR from a fresh branch? | yes — reuse the open PR; a fresh branch only once the PR is merged or closed | no | design D14 | user |

## Resolved

| # | Question | Answer | Decided on |
| --- | --- | --- | --- |
| Q-1 | Package and repository name (`worc-connect` vs the operator's first name `gith-worc`). | **`worc-connect`.** Fixes the CLI verb, the extras (`worc-connect[github]`), the entry-point group `worc_connect.trackers`, and the home directory name. | 2026-09-10 (user) |
| Q-2 | Connector home: own gitignored `.worc-connect/` or a subfolder of worc's private control home `.worc/`. | **Own `.worc-connect/`**, appended to the tracked `.gitignore` by `worc-connect init`, the way `worc install` adds `.worc/` and `.worc-io/`. | 2026-09-10 (user) |
| Q-3 | Trigger label and state-label vocabulary. | Default accepted: trigger `worc`, states `worc:queued` … `worc:done`; prefix configurable. | 2026-09-10 (user, default) |
| Q-4 | Include issue comments in the task body in v1? | Default accepted: body only; comments deferred, policy to be decided when triage lands. | 2026-09-10 (user, default) |
| Q-5 | Own process or a worc plugin? | Default accepted: own process, `worc-connect watch` beside `worc watch`. | 2026-09-10 (user, default) |
| Q-7 | Same host and clone as `worc watch` in v1? | Default accepted: yes (assumption A-1); a remote connector is deferred. | 2026-09-10 (user, default) |
| Q-8 | Default `commit_type` mapping from labels. | Default accepted: `bug → fix`, `documentation → docs`, else `feat`. | 2026-09-10 (user, default) |
| Q-9 | May the connector create the trigger and state labels on `init`? | Default accepted: create on `init`, skip those already present. | 2026-09-10 (user, default) |
| Q-10 | Where does the connector half of this spec live once its repository exists? | Default accepted: copied there as its backlog; this folder keeps the worc-side items (phases 01–02). | 2026-09-10 (user, default) |
| R-1 | Inside worc or a separate repository? | Separate repository; worc gets only contract items. | 2026-09-10 (conversation) |
| R-2 | One connector per tracker, or one core with adapters? | One core that knows no tracker API; adapters behind optional dependencies, discovered through an entry-point group; one repository for v1, split later if the interface stabilises. | 2026-09-10 |
| R-3 | Is triage (analyse, reproduce) part of v1? | Optional mechanic behind `triage.enabled`, **off** in v1, so the connector mechanics can be proven fast; both paths converge on one task builder. | 2026-09-10 |
| R-4 | Does v1 need any change to worc? | No. The connector names the branch itself and finds the PR by branch through `gh`; `references:` and `pr_url` are v1.1 conveniences. | 2026-09-10 |
| R-5 | Who runs the agent for triage — the connector or worc? | worc, always. The connector never launches an agent; it queues a task. | 2026-09-10 |
| R-6 | Who closes the issue in v1? | The connector, via `gh issue close` on merge (configurable). With `references:` available the operator may switch to GitHub's own close-on-merge. | 2026-09-10 |
| R-7 | Is the tracker axis the same as the code-host axis? | No. worc publishes through `gh` only; GitLab MR / Azure Repos publishing is a separate orchestrator item ("code host adapters"), not this one. | 2026-09-10 |
| R-8 | May the owner push commits to a connector-created PR by hand, retitle or reopen it, merge it any way, and delete the branch — and still have the item closed and the task followed? | Yes, by design (D14): the connector tracks the PR by number once found, recomputes PR-derived phases from the PR's live state every tick, never pushes, and closes on `mergedAt` whoever merged and however. worc's side already treats foreign commits and external merges as ordinary state (`adopt_foreign_commits`, `worc prs --sync`). | 2026-09-10 (user raised; conversation) |
