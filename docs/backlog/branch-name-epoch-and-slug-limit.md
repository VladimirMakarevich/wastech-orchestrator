# Branch name: epoch prefix + total length cap

Status: **proposed** (2026-06-26) Date: 2026-06-26 Owner: Vladimir Makarevich

When the orchestrator creates a branch for a task it derives the name from a static formula — `{prefix}/{task_id}-{slug}` — where the slug is the full task title lowercased and reduced to alphanumerics plus dashes. Two concrete problems emerge in practice: re-running the same task collides with the already-existing branch, and a long task title produces a slug that overflows GitHub's UI, CI log columns, and readable `git log` output. This document records the decision to add a unix-timestamp epoch before the task-id, cap the total auto-generated branch name at 50 characters (slug is truncated dynamically to fit), and degrade gracefully when an operator-supplied `branch_name` is over the limit.

## The problem

Re-running the same task produces the identical branch name. If the previous branch still exists locally or on the remote (e.g. the PR was closed without deletion, or the run was aborted before publish), `git checkout -b` fails and the pipeline errors out before any agent work begins.

Separately, task titles are often full English sentences. A title like "Implement user authentication with OAuth2 and Google provider integration" yields a 75-character slug, making the full branch name roughly 90 characters. This degrades readability in `git log --oneline`, GitHub's PR list, and CI pipeline names without providing any indexing value beyond the task-id.

Additionally, if an operator supplies a `branch_name` override that is too long, the current validation gate raises an error and aborts the run — an unnecessary hard failure when a safe fallback exists.

## Constraints

The hard Git ceiling is 255 UTF-8 bytes (`model.py:BRANCH_NAME_MAX_BYTES`). The core must not know the CLI syntax (`architecture.md`); branch construction lives in `git_manager.py`, not in providers.

## Alternatives considered

| Option | Why rejected |
| --- | --- |
| Do nothing | Re-run collisions are a runtime error; long slugs degrade UX. |
| Epoch embedded in `task_id` | task_id is stored in `state.db`, ledger, per-node logs, and prompt audit; changing its format breaks all references and reporting. |
| Date prefix (YYYYMMDD) | Human-readable but not unique across multiple runs on the same day. |
| Fixed slug limit (e.g. 40 chars) | Slug length is not an independent budget — it depends on prefix + epoch + task_id length. A total cap is the only guarantee that the full name stays short. |
| Unique suffix (short hash) instead of epoch | Epoch gives chronological ordering in `git branch --sort=creatordate` for free; a random hash does not. |
| Error on oversized operator `branch_name` | Aborts the run for a fixable problem; a warning + fallback preserves progress. |

## Decision

**Auto-generated names.** Change the formula to:

```
{prefix}/{epoch}-{task_id}-{slug}
```

where `epoch` is the unix timestamp in seconds at the moment `_prepare_branch` is called. The total name is capped at **50 characters**: the slug is computed as whatever fits after `{prefix}/{epoch}-{task_id}-`, truncated and stripped of trailing dashes. If nothing fits (prefix + epoch + task_id already ≥ 50 chars), the slug segment is omitted entirely.

**Operator `branch_name` override.** If the front-matter `branch_name` is set but its length exceeds 50 characters, log a warning and fall back to the auto-generated name. Values within the limit continue to be used as-is (the operator is responsible for uniqueness in that path). The 255-byte hard Git ceiling remains a hard error regardless of source.

The epoch makes every run unique — even a re-run of the same task with the same task-id gets a distinct branch — and ascending epoch order coincides with `git branch --sort=creatordate`. Capping the total at 50 characters keeps every auto-generated name well inside the GitHub UI column width with room to spare.

## Open questions

None — all sub-decisions resolved during the design conversation.

## Implementation notes

Four targeted changes; no schema bump, no config key, no migration:

- `src/wastech_orchestrator/task/parser.py` — add `slugify_bounded(value: str, max_len: int) -> str` that calls `slugify()` then truncates to `max_len` and strips trailing dashes. Returns `""` (not `"task"`) when `max_len ≤ 0` so the caller can omit the slug segment cleanly.
- `src/wastech_orchestrator/git_manager.py:branch_name()` — accept `epoch: int`; build the fixed prefix `f"{branch_prefix}/{epoch}-{task_id}"`, compute remaining budget `= 50 - len(fixed)`, append `-{slug}` only when the budget allows, using `slugify_bounded(slug, budget - 1)`.
- `src/wastech_orchestrator/core/orchestrator.py:_prepare_branch()` — capture `int(time.time())` once and pass it through to `git.prepare_branch(...)` → `branch_name(epoch=…)`.
- `src/wastech_orchestrator/task/validation_gate.py:_branch_name()` — on length > 50 emit `logger.warning(...)` and set `p.task.branch_name = None` to trigger auto-generation instead of raising a validation error. Keep the existing 255-byte check as a hard error.
