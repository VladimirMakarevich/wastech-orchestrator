# Accumulating follow-ups file for the target repository

Status: **implemented 2026-08-06** Date: 2026-08-06 Owner: Vladimir Makarevich

As built, with two deviations from the plan below, both noted in place: the file gets a one-time explanatory header when it is created (there is no CLI surface, so its own first lines are the only place it can explain itself), and `_write_deterministic_summary` **merges** into the carried tuple instead of replacing it — see "Where it plugs in".

## Problem

A task's follow-ups — the supervisor's technical-debt notes plus the evaluator findings a gate let past (everything below `gate_severity`, typically `medium` and `low`) — reach the operator in two places and neither answers "what has this orchestrator not fixed in this repository?":

- the `## Technical debt / follow-ups` section of the pull-request body — once per change, scattered across as many pull requests as there were tasks;
- `.worc/logs/<task-id>/summary.json` — once per task, under a directory [`worc logs clean`](../../../src/wastech_orchestrator/cli.py) deletes.

Ten tasks that each wave three `low` findings past the gate leave thirty items the operator has to go and collect by hand from thirty places. Wanted: one file in the repository's orchestrator home that accumulates them.

## Requirements (fixed by the owner — do not renegotiate these)

1. **On task completion, append that task's follow-ups to `.worc/follow-ups.md`.**
2. **Append only.** The file is never rewritten, never regenerated, never reconciled. Ten tasks with three findings each ⇒ **thirty entries** in the file.
3. **Dedup within a task only** — which the existing code already does via `merge_follow_ups`. **No cross-task dedup**: the same item found by two tasks is written twice.
4. **No CLI commands.** The operator reads the file and edits it by hand; deleting an entry is how an item is closed.
5. **Take the follow-ups where they are already computed in memory.** Do not re-derive them from `state.db`.

## Where it plugs in (verified against the code)

| What | Where |
| --- | --- |
| `FollowUp` record + the existing section renderer | [`core/follow_ups.py`](../../../src/wastech_orchestrator/core/follow_ups.py) — `render_follow_ups_section` can be reused or a file-specific renderer written beside it |
| Supervisor path — the merged tuple is already in hand | `_engine_finalize` → `finalized.follow_ups` ([`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py)) |
| Deterministic path (layer off / degraded / non-`done` terminal) — likewise | `_write_deterministic_summary` → `evaluator_finding_follow_ups(evaluations)` (same file). **As built it merges** rather than replaces: on a degraded `done` both producers run, this one second, and a bare assignment would have dropped the supervisor's own debt notes from the accumulating file _and_ from the pull-request body. The merge is a no-op on every path where nothing set them. |
| **The one append site** | `_finalize_task_artifacts` — it runs on both terminal paths (the `done` finalize and the infra-terminal publish), and it is where the summary is already written. Needs the tuple carried on `_Pipeline`, set by each of the two producers above. |
| File location | `RuntimeLayout.control_home` (`<repo>/.worc/`) — gitignored, so the file never enters a commit or a pull-request diff |

Two details worth not rediscovering:

- **`logs clean` needs no change.** Its sweep-by-shape helper `_daemon_log_files` only walks `.worc/logs/`; a file at the `.worc/` root is out of its reach. (It _would_ have swept a file placed under `logs/`.)
- **Write with `newline=""`.** The daemon may run on any host and the file must not change line endings with it.

- **The append goes after `_summary_md_body`**, which is the call that runs the deterministic producer, and before the `dest is None` return, so a synthetic `run` with no on-disk task file still accumulates its debt.

Terminal paths that bypass `_finalize_task_artifacts` need nothing: `_resume_cleanup` finishes a task whose finalize already ran in the previous process, and the manual `finalize` command closes a task that never produced a summary or a follow-up.

One re-entry is accepted rather than guarded: a git failure inside `publish` stops the task `manual_action_required` **after** finalize already appended, so a `rerun --continue` that re-enters the publish node appends a second section under the same task id. That is two finalizations and the file says so (each section carries its own timestamp); suppressing it would need the file read back, which requirement 2 forbids.

## Non-goals — considered and rejected

- **Rebuilding the file from `state.db`.** Correct on content (the rows are never pruned) but it overwrites the operator's edits, and with no `resolve` verb that leaves no way to ever close an item.
- **Cross-task dedup**, in any form — neither a `state.db` oracle nor reading the file back.
- **`worc follow-ups` / `worc follow-ups resolve`.** No CLI surface at all.
- **A JSONL registry** beside the ledger. The markdown file is the artifact; there is no second store.
- **Committing the file to the target repository.** Every task runs on its own branch, so a committed accumulating file conflicts on every task and adds an unrelated file to every diff.
- **Backfilling tasks that finished before this ships.** The file starts empty and grows from the next task.

## Definition of done

- The file accumulates across tasks with no entry lost or overwritten; a task with no follow-ups writes nothing (not an empty section).
- Appending is best-effort: a write failure is logged and never changes the task's terminal status.
- Tests cover the accumulation across several tasks, the empty-follow-ups case, and LF line endings.
- Docs synced on `dev`: [`README.md`](../../../README.md) and the shipped operator guide under `src/wastech_orchestrator/packaged/guide/` (`footprint.md` describes the `.worc/` layout). Leave a doc-impact line in [main-docs-reconstruction-notes.md](main-docs-reconstruction-notes.md) for the derived `main` pages.
