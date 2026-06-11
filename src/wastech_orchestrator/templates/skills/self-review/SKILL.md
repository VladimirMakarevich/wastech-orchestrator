---
name: self-review
description: Review your own change before finishing a stage in a repository driven by the wastech-orchestrator, to catch the issues the review stage would otherwise flag. Use at the end of implementation or fixing, before handing the result back.
---

# self-review

The orchestrator runs a dedicated **review** stage; blocking findings send you back into a bounded
**fixing** loop. Catch those findings yourself first — a clean self-review is the cheapest way to
reach a single passing pull request.

## Checklist (run before you hand off)

- **Task alignment.** Does the change satisfy the task and every acceptance criterion — no more,
  no less? No scope creep, no half-finished path.
- **Correctness & edge cases.** Inputs at the boundaries, empty/None, error paths, concurrency or
  ordering assumptions — are they handled?
- **Diff hygiene.** Only the intended files changed; no stray/accidental files, no debug prints,
  no commented-out code, no unrelated reformatting.
- **Tests.** New/changed behavior is covered; the project's checks pass locally
  (see the `test-discipline` skill).
- **Style.** The change matches the surrounding code's conventions and naming.
- **Dependencies.** No new dependency was added without need; if one was, it's called out.
- **Guardrails.** You did not commit/push/PR, did not touch secrets, and stayed within the sandbox
  (see the `safe-change` skill).

## Steps

1. Re-read the task and acceptance criteria, then read your own diff end to end as if reviewing
   someone else's work.
2. Walk the checklist above; fix anything that fails **now**, while you still hold the context.
3. Re-run the project's checks after any fix.
4. Write the handoff: what changed and why, any assumption made, residual risks, and what the
   reviewer should focus on. This becomes part of the trail the orchestrator uses for the summary.

## Rule

If you find a blocking problem you cannot resolve within the task's scope, say so explicitly in your
result (with the reason) rather than shipping a change you know is incomplete.
