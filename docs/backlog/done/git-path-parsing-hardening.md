# Git path parsing hardening for Unicode and quoted names

Status: **implemented** Date: 2026-07-15 Owner: Vladimir Makarevich

This backlog item is an implementation plan for fixing Git path handling across the orchestrator. It is not an ADR. The immediate trigger was a publish failure on a successfully completed task whose edited file was named `blog/20260206-моя-история-(EN).md`: the task finished, but publish failed when the orchestrator attempted `git add -- blog/20260206-\320...`.

## The problem

The orchestrator currently parses several Git path-producing commands from line-based text output and assumes the path can be recovered with simple string trimming. That assumption fails for non-ASCII file names because Git's default text output C-quotes such paths (`\320\274...`) instead of emitting the literal UTF-8 path.

The known hot spots are in `src/wastech_orchestrator/git_manager.py`:

- `changed_code_paths()` reads `git status --porcelain` for the publish staging set.
- `_fully_staged_deletions()` reads `git status --porcelain` for the F18 deletion edge case.
- `changed_code_entries()` reads `git diff --name-status`.
- `_changed_code_paths_from()` reads `git diff --name-only`.
- `files_in_commit()` reads `git diff-tree --name-only`.
- `_unaccounted_dirty_paths()` reads `git status --porcelain` during terminal cleanup.

This is broader than a publish bug. The same parsing shape can mis-handle:

- check-set selection,
- dangerous-diff / protected-path detection,
- task-scoped changed-path calculation for memory and evaluator inputs,
- handoff file lists for decomposed tasks,
- cleanup diagnostics after task completion.

## Goal

Make Git path handling correct for Unicode, spaces, quotes, tabs and other unusual-but-valid file names across the whole orchestrator, not only on the publish path.

## Non-goals

- Changing the GitManager ownership model or the scoped-staging invariant.
- Adding a global repo mutation such as persisting `core.quotepath=false` in user config.
- Narrow tactical fixes that only unbreak `publish`.

## Recommended approach

Use NUL-delimited Git output (`-z`) as the default for every machine-parsed path list and centralize the parsing in small helpers inside `git_manager.py`.

Why this approach:

- It preserves the current CLI/security invariants: still argv lists, no shell interpolation.
- It fixes Unicode correctly instead of relying on display-oriented text formatting.
- It also fixes edge cases beyond Cyrillic: embedded spaces, quotes, tabs and rename records.
- It avoids silent drift between call sites because one helper defines the contract.

`git -c core.quotepath=false ...` is acceptable as a temporary diagnostic aid, but not the target implementation: it still leaves the code dependent on line-based text parsing for outputs that Git already exposes in a machine-safe form.

## Plan

### 1. Introduce machine-safe Git path parsers

Add internal helpers in `git_manager.py` for the few output shapes the orchestrator actually needs:

- `name-only -z` parser for plain path lists.
- `name-status -z` parser for status + path tuples, including rename/copy entries.
- `status --porcelain -z` parser for working-tree state, including rename records and untracked files.

The helpers should return normalized repo-relative POSIX paths and hide Git's record layout from the rest of the file.

### 2. Move every machine-parsed path call site to the helpers

Replace ad hoc string parsing in:

- `changed_code_paths()`
- `_fully_staged_deletions()`
- `changed_code_entries()`
- `_changed_code_paths_from()`
- `files_in_commit()`
- `_unaccounted_dirty_paths()`

Goal: no path-bearing Git output should be parsed with `splitlines()` plus string trimming unless the output is intentionally human-facing.

### 3. Preserve human-facing text outputs as text

Do not churn commands whose output is meant for humans or artifacts rather than path extraction:

- `git diff --text`
- `git diff --stat`
- `git diff --cached --check`
- ordinary command stderr surfaced in failure messages

This keeps the patch focused on machine parsing and avoids unnecessary regression risk.

### 4. Add regression tests for Unicode file names

Extend `tests/git/test_git_manager.py` with real-repo tests covering at least:

- untracked non-ASCII file committed through `commit_code()`,
- modified tracked non-ASCII file committed through `commit_code()`,
- `changed_code_entries()` returning literal Unicode paths,
- `changed_code_paths_since_base()` returning literal Unicode paths,
- `files_in_commit()` returning literal Unicode paths,
- `_unaccounted_dirty_paths()` / cleanup reporting literal Unicode paths.

The tests should use actual non-ASCII names rather than escaped literals so the failure mode is visible and the intended behavior is explicit.

### 5. Add at least one rename case

Because `-z` rename records differ from line-based `"old -> new"` parsing, add a focused test for a rename where either the source or destination path contains non-ASCII characters. This validates that the new helper handles the record shape correctly and does not regress F18/F15-era staging logic.

### 6. Verify the broader consumers indirectly

A full orchestrator E2E is not required for the first patch if unit/integration coverage proves the GitManager contract, but the change should at least exercise the GitManager-backed behaviors that depend on these path sets:

- publish staging,
- check selection,
- dangerous-diff path matching,
- task cleanup.

If one focused orchestrator-level regression test is cheap to add, prefer the publish path because that is the incident that exposed the bug.

## Acceptance criteria

- [x] `commit_code()` succeeds when the changed file path contains Cyrillic characters.
- [x] The path passed to `git add` is the real UTF-8 repo path, not a quoted `\320...` string.
- [x] `changed_code_entries()` preserves literal non-ASCII paths for both tracked and untracked files.
- [x] `changed_code_paths_since_base()` and `changed_code_paths_since_task_base()` preserve literal non-ASCII paths.
- [x] `files_in_commit()` preserves literal non-ASCII paths.
- [x] Cleanup and dirty-tree diagnostics preserve literal non-ASCII paths.
- [x] Existing F15/F18 behavior remains intact for root files, tracked `tasks/` exclusion and fully-staged deletions.
- [x] The implementation does not rely on mutating repo-local Git config.

## Shipped

Implemented as planned: three module-level `-z` parsers in `git_manager.py` (`_parse_name_only_z`, `_parse_name_status_z`, `_parse_porcelain_status_z`), with all six hot-spot call sites migrated to them. `tests/conftest.py`'s shared `run_git` test helper now decodes subprocess output as UTF-8 explicitly (it previously relied on the platform locale encoding, which is not reliably UTF-8 on Windows) so the new regression tests can assert on literal non-ASCII paths. New tests in `tests/git/test_git_manager.py` cover untracked/tracked Unicode commits, a Unicode rename (both `--name-status -z` and `status --porcelain -z` record shapes), and Unicode paths through `changed_code_entries()`, `changed_code_paths_since_base()`/`_since_task_base()`, `files_in_commit()`, and cleanup dirty-path diagnostics.

## Risks and watchpoints

- `status --porcelain -z` rename records are NUL-separated and not formatted like `"old -> new"`; the parser must model Git's real record structure instead of adapting the old string logic.
- The fix should stay local to GitManager. Re-implementing path parsing in multiple modules would recreate the bug class later.
- Tests must stay cross-platform: assert POSIX-style repo-relative paths, not platform-native separators.

## Likely implementation areas

- `src/wastech_orchestrator/git_manager.py`
- `tests/git/test_git_manager.py`
- optionally one orchestrator/publish regression test if the first patch adds end-to-end coverage
