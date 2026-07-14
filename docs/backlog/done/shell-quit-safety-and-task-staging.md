# Shell quit safety + staged task-file creation (`preparing` → `pending`)

Status: **implemented** (2026-07-14) Date: 2026-07-13 Owner: Vladimir Makarevich

This is a design record for two independent, composable operator-safety fixes found while investigating an incident: the operator ran `worc shell` → `up` → a task ran to completion → `quit`, then started composing a new task file directly in `tasks/pending/` — unaware that `quit` deliberately leaves the watch daemon running (the `M3` decision, shipped 2026-07-06 — see the _worc shell reliable control surface_ ADR referenced in [follow_ups.md](../follow_ups.md)). The still-running daemon's next poll tick picked up the half-written file and the validation gate quarantined it, moving the operator's in-progress draft out of `tasks/pending/` before it was finished. `quit` detaching is correct, intentional, and tested behavior (`tests/test_cli_shell.py::test_run_shell_detaches_attached_daemon_on_quit`) and must not change; the gap is that nothing makes that state loud, and the pending-folder scanner has no write-atomicity at all. Both fixes below close that gap without touching the `M3` decision or the "broken task is quarantined immediately, never branched" invariant ([.agents/rules/architecture.md](../../../.agents/rules/architecture.md)).

## The problem

1. `up` spawns a detached daemon process that polls `tasks/pending/` (default `poll_interval_seconds: 300`, but the first tick fires immediately on daemon start).
2. `select_pending` ([cli.py:1105](../../../src/wastech_orchestrator/cli.py#L1105)) does a bare `iterdir()` scan — no mtime/age check, no debounce, no lock file. Any `.md`/`.json` file present is a candidate, including one that is mid-write.
3. A syntactically-incomplete file almost always fails `ValidationGate`, and `Orchestrator._quarantine()` ([core/orchestrator.py:2985](../../../src/wastech_orchestrator/core/orchestrator.py#L2985)) reacts by immediately renaming it out of `tasks/pending/` into `.worc/tasks/rejected/` — destroying/derailing a draft the operator hasn't finished writing.
4. `quit` (`cli_shell.py:310`) intentionally leaves the daemon running, and today only prints a quiet, easy-to-miss informational line in `_shutdown_daemon` ([cli_shell.py:546](../../../src/wastech_orchestrator/cli_shell.py#L546)) — nothing stops the operator from assuming the orchestrator is fully off.

## Decision

Ship both parts; they are independent and can land separately.

### Part A — loud exit warning + confirmation gate on `quit`

- When `quit`/`exit` is dispatched (`cli_shell.py:310`) and a watch daemon is currently running (same `process_control.running_daemon_pid` check `_do_up` already uses), print a `WARNING:`-level message — not the current quiet print in `_shutdown_daemon` ([cli_shell.py:546-561](../../../src/wastech_orchestrator/cli_shell.py#L546-L561)) — and require the operator to confirm before the REPL actually exits.
- Interactive mode (`_run_interactive`, [cli_shell.py:516-543](../../../src/wastech_orchestrator/cli_shell.py#L516-L543)): confirm via the _same_ `PromptSession`/`prompt_async()` the REPL already uses, not a bare `input()`. `_shutdown_daemon`'s own docstring already documents why: a blocking prompt there would violate H1, the single-stdin-reader rule enforced for `down`/`restart`/`rerun` ([cli_shell.py:332-344](../../../src/wastech_orchestrator/cli_shell.py#L332-L344)). Concretely, the confirmation has to happen inside the `quit`/`exit` handling while the interactive session is still live — not in `run_shell()`'s shared `finally`, where `_shutdown_daemon` runs today, since by then the session is already torn down.
- Scripted/headless mode (`_run_scripted`, [cli_shell.py:499-504](../../../src/wastech_orchestrator/cli_shell.py#L499-L504), used by tests and non-interactive driving) has no user to confirm with — follow the existing `--non-interactive`/`--yes` convention already used by `down`/`restart`/`rerun` rather than blocking.
- Distinguish "queue is being served, nothing active right now" from "a task is actively running" using the existing `has_active_task` probe ([cli.py:1078](../../../src/wastech_orchestrator/cli.py#L1078)) — the risk (something will be silently picked up next tick) exists even when idle.

### Part B — staged task-file creation: `tasks/preparing/` → `tasks/pending/`

- Add a new sibling folder next to the existing `tasks/pending`/`tasks/done`/`tasks/failed` convention (`paths.tasks_dir`, default `"tasks"`, [config.example.yaml:27](../../../src/wastech_orchestrator/packaged/config.example.yaml#L27); `pending_dir()` at [cli.py:1073](../../../src/wastech_orchestrator/cli.py#L1073)) — exact name TBD, see Open questions. The watch scanner never looks inside it, so a file mid-composition there is invisible to the daemon by construction — the race is closed completely, regardless of how long composition takes, with no mtime-heuristic needed for files that follow this path.
- Add a "promote" verb to move a file (or all files) from the staging folder into `tasks/pending/` once actually ready: a new `worc shell` command (sibling to `enqueue`/`cancel` in `dispatch()`, [cli_shell.py:305-348](../../../src/wastech_orchestrator/cli_shell.py#L305-L348)) plus a top-level non-interactive `worc` subcommand (mirroring `run`/`watch`/`stop`) so the `worc-task`/`worc-deco-task` skills and scripts can call it without opening the interactive shell. Support one id/file and `--all`.
- **Must move atomically** (`Path.rename`/`os.replace`, same filesystem — a single syscall, no partial-write window), not copy. The existing `enqueue` ([cli_shell.py:351-364](../../../src/wastech_orchestrator/cli_shell.py#L351-L364)) uses `shutil.copy` straight into `tasks/pending/` today — fix this in the same change so it isn't left as a second, still-unsafe way to land a file directly in the scanned folder (see Open questions on whether `enqueue` should instead target the staging folder).
- `worc-deco-task`'s subtask batch convention (`tasks/pending/subtasks/NN-<slug>.md` alongside the root task) needs the equivalent staged shape and the "promote all" mode must preserve that subfolder structure when moving a whole batch together.
- Update every place that currently tells a human or an agent to write straight into `tasks/pending/` (doc-sync sweep, per [CLAUDE.md](../../../CLAUDE.md)'s doc-sync rule — this reaches the shipped, operator-facing docs under `packaged/`, not just `docs/`):
  - `packaged/guide/tasks/skills/worc-task/SKILL.md` step 4 ("Write the file to `tasks/pending/<id>.md`")
  - `packaged/guide/tasks/skills/worc-deco-task/SKILL.md` (root task + subtasks paths)
  - any other guide/quickstart under `packaged/guide/` that names `tasks/pending/` as a direct write target

## Constraints

- Must not touch the `M3` decision itself — Part A adds a confirmation gate; `quit` still detaches by default, it does not start stopping the daemon.
- Must not weaken the "broken task is quarantined immediately, never branched" invariant ([.agents/rules/architecture.md](../../../.agents/rules/architecture.md)) — Part B makes the race moot for anyone who stages first; it does not change `ValidationGate`/`_quarantine` behavior for a file that does land in `tasks/pending/` broken.
- Cross-platform: confirm `Path.rename`/`os.replace` existing-destination semantics match what's already used elsewhere in this codebase (e.g. `process_control.write_pid_file`'s atomic temp+replace) and reuse the same pattern on both POSIX and Windows.
- No shell interpolation — the new CLI verb takes a real argv like every other subcommand.
- Greenfield MVP, no deployment — no migration path needed for the new folder or config; a fresh install just ships with it.

## Resolved decisions (as implemented 2026-07-14)

- **Folder name:** `preparing` (sibling of `pending`/`done`/`failed`, tracked at the repo root, created by `install`). Added to `REPO_TASK_DIRS`; `preparing_dir()` mirrors `pending_dir()`. The subfolder name is fixed (not a new config key) like the other lifecycle folders.
- **Promote verb:** `worc promote <id|file>` (top-level CLI subcommand) and a `promote <id|file> | --all` verb inside `worc shell`, both delegating to `cli.promote_tasks()` (single source of truth). Single promote is decomposition-aware — a root pulls the subtask specs it references (`preparing/subtasks/…` → `pending/subtasks/…`, specs moved before the root); `--all` moves every staged top-level file plus the whole `subtasks/` subfolder. The move is a single atomic `Path.replace`; it refuses to overwrite an existing queued file.
- **`enqueue`:** kept as the "already-complete external file" fast path, but its write is now atomic — `cli._atomic_copy` writes a `.tmp` sibling (never a `.md`/`.json` scan candidate) in `pending/`, then `os.replace`s it into place.
- **Part A gate:** interactive `quit`/`exit` prints a loud `WARNING` and confirms via the REPL's own `PromptSession` (H1 — one stdin reader) when a daemon is still serving, distinguishing an actively-running task from an idle-but-serving daemon; scripted/headless prints the warning but never blocks. No `quit --yes` flag was needed. M3 (detach-by-default) is unchanged.
- **Defense-in-depth mtime-settle check on `tasks/pending/`:** deferred (not implemented) — staging closes the race for compliant producers; revisit only if real-world drift from a producer that bypasses staging is observed.
