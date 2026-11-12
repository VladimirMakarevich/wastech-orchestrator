# Findings from the first real worc-connect runs

Status: **open** Date: 2026-11-13 Owner: Vladimir Makarevich

## Context

On 2026-11-12/13 the tracker connector (`worc-connect`, its own repository) made its first two real end-to-end runs against worc `0.16.0a1` in `wastech-mdlint`: one issue taken straight to an implementation task (`gh-24` → PR #34), and one through the connector's shipped `issue_triage` flow (`gh-31` triage → `gh-31.2` implementation → PR #35). Both reached a merged pull request and a closed issue. They also surfaced three worc-side defects. Each one is confirmed against the code on `dev` below, and all three are small enough to fix in one pass.

## 1. A task whose flow publishes no pull request is never finalized

**Observed.** The `issue_triage` task `gh-31` ran to `done`, and its file stayed in `tasks/pending/gh-31.md`. `worc list` kept showing it under `pending:` (as `high gh-31`) and under `recent:` as `done` at the same time. No `gh-31.summary.md` was written. worc did not run the task again on the next scans, but the file stayed where it was.

**Cause.** `PublishNodeRunner._publish` (`src/wastech_orchestrator/core/flow/nodes/publish.py`) calls `self._s.finalize()` (the hook that is `Orchestrator._engine_finalize` → `_finalize_task_artifacts` → `_move_task_file`) only on the pull-request path. `private_control_workspace_report` returns from `_store_private_report`, and `none` / `local_artifact` return `None`, before that call. The module docstring even says "Task finalize (moving the task file before the audit commit) … stay orchestrator-level", but no orchestrator-level path does it for these policies on success. The terminal-failure paths call `_finalize_task_artifacts` themselves, so only the successful no-PR path is affected.

**Who is affected.** Every successful task whose flow does not open a pull request: the packaged `security_audit` (private report), `merge` (`publishing: none`), any operator flow with `none` / `local_artifact`, and the connector's `issue_triage`. The pending folder fills with finished tasks, and every read-only view that lists `pending/` shows them as queued.

**Expected.** A successful task ends with its file in `tasks/done/` and its `<id>.summary.md` beside it, whatever its publishing policy. The no-git policies have to stay no-git: no audit commit, nothing staged. The ordering constraint the PR path needs (finalize before the audit commit) does not apply to them, so they can finalize after the deliverable is stored. A regression test should cover each non-PR policy: the file ends in `done/` and a summary is written.

**Check.** Before changing it, confirm why the leftover file was not run again (a terminal row for the id, or the duplicate-id gate). Whichever it is, it must keep holding for files that were left behind by versions before the fix.

## 2. A single-pass `watch` is invisible to the liveness probe, and claims nothing

**Observed.** During `worc watch --poll-seconds 0`, `worc list` showed the task being executed as `parked (no daemon)` for the whole run. That is the label that sends an operator to `rerun --continue`.

**Cause.** `cmd_watch` (`src/wastech_orchestrator/cli.py`) takes the single-pass branch (`if poll <= 0:`) before it writes the daemon PID file. The comment there says so on purpose: "Single pass: no PID file, no signal handler, no stop wiring". `_executor_alive` checks the daemon PID file and the `run` marker, so it sees neither. `_display_status` then reports `parked (no daemon)`. This is the same defect `_executor_alive`'s own docstring describes as already fixed for `worc run`.

**Also check.** The single-pass branch also returns before the executor-owner check (`_executor_owner`), which the loop path performs. As far as the code reads, a single pass can start while a daemon or a `run` holds the clone, and a daemon can start while a single pass is executing. Confirm that, and close it with the same exclusive claim `run` uses (the runner marker): a single pass is an executor for exactly as long as it runs.

**Expected.** A task executing under a single-pass `watch` reads as `running`, and a single pass refuses to start in a busy clone with the same message `run` gives.

## 3. A build from `dev` reports a version older than the release it contains

**Observed.** Installed from `git+…@dev`, worc reports `wastech-orchestrator 0.10.3a2.dev229+g3e898df0f`, while that build already carries everything `v0.14.0a1` shipped. Any consumer that gates on worc's version breaks on it. The connector's handshake (`supports_contract`, minimum `0.14.0a1`) refused the contract it was being tested against until its test dependency was pinned to the tag `v0.16.0a1`.

**Cause.** The version comes from hatch-vcs, and the release tags (`v0.11.0a1` … `v0.16.0a1`) sit on `release`, a branch that does not merge back: `git merge-base --is-ancestor v0.16.0a1 origin/main` is false, and the newest tag reachable from `dev` is `v0.10.3a1`.

**Expected.** A build from `dev` or `main` reports a version at least as new as the newest release it contains. Ways to get there:

- merge `release` back into `main` after each tag, so the tags become reachable;
- tag the merge on `main`;
- give hatch-vcs a fallback that reads the newest release tag.

Choose one under the four-branch model in `.agents/rules/git-workflow.md`, without breaking the seal it describes, and record the choice there.

## Out of scope

- The connector's own findings from the same runs are tracked in its repository.
- The CI failure in `wastech-mdlint`'s Markdown-context job (a published linter that does not know a rule the repository's config already uses) is that repository's problem, not worc's.

## Definition of done

- Item 1: each non-PR publishing policy moves the task file to `tasks/done/` and writes the summary on success, with regression tests; `worc list` no longer shows a finished task as pending.
- Item 2: a single-pass `watch` is seen by `_executor_alive` and respects `_executor_owner`, with tests.
- Item 3: the chosen versioning fix is in place and documented; a build from `dev` reports at least the newest release version.
- The usual gate is green, the docs listed in `AGENTS.md` are updated in the same change, and this file leaves `docs/backlog/` when it lands.
