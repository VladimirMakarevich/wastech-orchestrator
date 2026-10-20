# Open questions — Agent diff hand-off

## Open

No open questions.

## Resolved

| # | Question | Answer | Decided on |
| --- | --- | --- | --- |
| Q-1 | In a decomposed run, which scope does each packaged node get — the whole task since its base, the current subtask since the last orchestrator commit, or both side by side? | Both, side by side (`design.md` D-5): the whole-task section always, a since-last-commit section whenever the orchestrator has committed inside the task; the subtask `review` prompt judges the second, `documentation` uses the first. | 2026-10-19 (user) |
| Q-2 | Should a node that resumes its own editing session (`fixing`, `documentation`) be guided to the overview at all? | The footer stays uniform for every node (`design.md` D-6); the packaged `fixing` prompts start from the review findings and the failed check log and use the overview only to locate files, `documentation` works from the overview and per-file diffs. | 2026-10-19 (user) |
| Q-3 | Does anything between two nodes change the working tree — a `checks` command, a `tool` node? | Not settled by code: a check command is operator-defined and may run a formatter. Refreshing the overview per node would also mean rewriting `current.diff`, which this task does not touch, so FR-6 stays a Could and is deferred (see `out-of-scope.md`). | 2026-10-19 |
| Q-4 | The bounds for NFR-4. | At most 200 paths per section, then a `… and N more paths (+A −R) not listed` line that keeps the totals exact; a constant in code, not config (`design.md` D-8). | 2026-10-19 |
| Q-5 | When a route can fall back to a provider with a different shell capability, is the guidance rebuilt per attempt or chosen once? | Chosen once per request, for the most restrictive provider on the route: the shell sentence only when every provider on the route grants a shell, anything unknown counts as no shell (`design.md` D-3). A partial-diff fallback drops the overview for that attempt (D-7). | 2026-10-19 |
