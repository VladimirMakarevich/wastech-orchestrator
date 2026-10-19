# Phase 03 — Git Manager writes the overview

- **Status:** ☐
- **Depends on:** 01
- **Delivers:** `.worc/logs/<task-id>/change-overview.txt` written in the same call as `current.diff`, from the same snapshot, while `current.diff` stays byte-identical.

## Goal

Implement D-1 and D-5 in the one place that owns the base, the gate reference and the diff. Moves toward AC-1, AC-2, AC-7, AC-E3, AC-E5, AC-E7, AC-N2.

## Steps

1. **First, before any change:** a regression test that writes `current.diff` for a fixture tree (modified, deleted, renamed, untracked, a `.worc/` path) and pins its bytes.
2. Add a frozen `CurrentDiff` result (`diff_path`, `overview_path`, `base_sha`) in `src/wastech_orchestrator/git_manager.py`, and change `write_current_diff(task_id) -> CurrentDiff`.
3. Inside it, after writing the diff unchanged: resolve `_diff_base()` with `rev-parse` to a full SHA; render the whole-task section from `parse_diff` over the redacted diff text just written; when `gate_reference(task_id)` resolves to a different SHA, build the since-last-commit section from `changed_code_entries` plus `git diff --numstat -z <gate-ref>` and line counts for untracked files; mark entries matching `security.denied_read_paths` (the project's glob matcher); redact the rendered text with `_diff_secrets()`; write it as UTF-8 bytes with `\n`.
4. Update the git protocol in `src/wastech_orchestrator/core/flow/nodes/base.py` and its callers in `core/flow/nodes/agent.py` (`_apply_post_edit_guard`, `_reconsider`) to read `.diff_path` from the result — behavior otherwise unchanged in this phase.
5. Register the private overview as artifact kind `change_overview` where the diff is registered.
6. Update every fake git manager in the suite to return a `CurrentDiff`.

## Files touched

- `src/wastech_orchestrator/git_manager.py`
- `src/wastech_orchestrator/core/flow/nodes/base.py`
- `src/wastech_orchestrator/core/flow/nodes/agent.py`
- `tests/git/test_git_manager.py`
- `tests/core/` fakes that implement the git protocol

## Invariants in play

- `current.diff`'s content, base, pathspec and write moment are unchanged (out-of-scope).
- No secret in the overview: paths and counts only, redacted like the diff.
- A git failure here is a Git error, routed exactly as a failing `write_current_diff` is today — never a fallback.
- Cross-platform: POSIX paths from git, bytes written with `\n`.

## Tests

- The byte-identity test from step 1 still passes.
- Whole-task section paths equal `summarize_diff(current.diff).paths`, untracked included, `.worc/` excluded; counts per file match the hunks.
- Base line is 40-hex: `new` mode equals `rev-parse` of the base branch; `existing` mode equals the recorded start commit.
- No second section before an orchestrator commit; after `commit_subtask`, the second section lists only paths changed since, and they are a subset of the first section's.
- A denied path carries `[denied]`; a secret embedded in a path is redacted.
- An empty change writes `0 files, +0 −0`; a failing git call raises the existing error type and leaves no half-written overview.

## Docs to sync in this phase

None — the file exists but nothing hands it to an agent yet.

## Acceptance for this phase

- [ ] AC-1, AC-2, AC-7, AC-E3, AC-E5, AC-E7 hold at the Git Manager level.
- [ ] `current.diff` is byte-identical to the pre-change output on the fixture.
- [ ] `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports` and `pytest` are green.
