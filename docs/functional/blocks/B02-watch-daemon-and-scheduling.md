# B02 — Watch Daemon and Task Scheduling

> Reconstructed from code (`cli.py` watch surface, `process_control.py`) and tests (`tests/test_cli_watch.py`, `tests/test_process_control.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/cli.py`, `src/wastech_orchestrator/process_control.py`

## Responsibility

Turn the one-shot `run` pipeline into a stoppable polling service: discover task files dropped (or git-pushed) into `tasks/pending`, resume any single in-flight task first, then feed pending tasks to the orchestrator one at a time under the auto-mode rule. The daemon owns its own lifecycle — a PID file, a refuse-second-instance guard, and a cross-platform graceful-stop mechanism (a `SIGTERM`→event bridge on POSIX plus an `orchestrator.stop` sentinel file polled everywhere) that lets the current tick finish before exit. All scheduling logic lives in `cli.py`; all PID/signal plumbing lives in `process_control.py`, which is print-free and fully seam-injectable so it is unit-testable without real processes ([process_control.py:1](../../../src/wastech_orchestrator/process_control.py#L1)).

The block schedules; it does not execute. Task execution, resumption, slot ownership, and base-branch refresh are all `Orchestrator` methods owned by [B06](B06-orchestrator-pipeline.md); this block only sequences calls to them.

## Public surface

- `select_pending` ([cli.py:535](../../../src/wastech_orchestrator/cli.py#L535)) — pending `.md`/`.json` task files in a deterministic (sorted) order.
- `watch_once` ([cli.py:542](../../../src/wastech_orchestrator/cli.py#L542)) — one scheduling pass: resume, then drain pending under the auto-mode rule.
- `watch_loop` ([cli.py:571](../../../src/wastech_orchestrator/cli.py#L571)) — `refresh_repo` → `watch_once` → sleep/event-wait, bounded by `poll_interval`/`max_iterations`/`stop_event`, and stopped between ticks by `stop_event` (POSIX `SIGTERM`) or the `stop_file` sentinel (cross-platform).
- `cmd_watch` / `cmd_stop` / `cmd_restart` ([cli.py:935](../../../src/wastech_orchestrator/cli.py#L935), [cli.py:1002](../../../src/wastech_orchestrator/cli.py#L1002), [cli.py:1021](../../../src/wastech_orchestrator/cli.py#L1021)) — the operator subcommands, dispatched by [B01](B01-cli-and-operator-commands.md).
- `pending_dir` ([cli.py:522](../../../src/wastech_orchestrator/cli.py#L522)) — `<repo>/tasks/pending`.
- `pid_file_path`, `stop_file_path`, `stop_file_requested`, `write_pid_file`, `read_pid`, `is_running`, `running_daemon_pid`, `stop_process`, `StopController`, `_can_signal` ([process_control.py:36](../../../src/wastech_orchestrator/process_control.py#L36) onward) — the PID/stop-file/signal plumbing.

## Behavior

### Single scheduling pass — `watch_once`

A pass resumes the in-flight task **first**, before touching the pending queue ([cli.py:552](../../../src/wastech_orchestrator/cli.py#L552)). `orchestrator.resume()` returns the resumed task's terminal result, or `None` when the slot is free ([orchestrator.py:643](../../../src/wastech_orchestrator/core/orchestrator.py#L643)). If the resume lands in `MANUAL_ACTION_REQUIRED`, the pass returns immediately and **no pending task is picked** ([cli.py:555](../../../src/wastech_orchestrator/cli.py#L555)) — a stuck human-in-the-loop task blocks the queue rather than piling new work behind it.

Otherwise the pass reads each pending file's `id`/`depends_on`/`priority`/`queue` with the cheap `_scan_pending_meta` front-matter scan, **drops any task whose `queue` does not equal this instance's selector** (`--queue` if given, else `config.orchestrator.queue`; a missing/malformed queue folds to `"default"` in the scan), then sorts the surviving list by `(priority_rank, filename)` so eligible tasks run `high → mid → low` with filename as the tie-break (an unrecognised/absent `priority` sorts as `mid`), then iterates. The queue filter runs before the sort and the `depends_on` map is built, so out-of-queue tasks are invisible to this instance — a cross-queue dependency is just an unmerged dependency that keeps the dependent WAITING. `depends_on` stays stronger than priority: a higher-priority task still waiting on an unmerged dependency is skipped, so a lower-priority eligible task runs ahead of it. For each file the pass re-checks the slot via `acquire_slot("")` ([cli.py:560](../../../src/wastech_orchestrator/cli.py#L560)). `acquire_slot` is true iff no task with a non-empty id is active ([orchestrator.py:377](../../../src/wastech_orchestrator/core/orchestrator.py#L377)); the empty-string argument means "is the slot free for _anyone_". A busy slot breaks the loop. On a free slot it runs `orchestrator.run_task(str(task_file))` and appends the result.

**Next-task confirmation gate (idea 27, opt-in).** When `orchestrator.auto_mode.confirm_next_task` is on, the pass calls `_confirm_next_task` immediately after the dependency check and **before** `run_task` — an `orchestrator.notifier.ask_human` approve/deny carrying the task id + front-matter title only (no diff/prompt). Approve → claim and run it. Deny / timeout / transport error → `break` (leave the task pending, stop chaining for this cycle — fail-closed STOP; a silent operator never advances an autonomous claim). It gates **new claims only**: the resume-first step above is never gated. It is non-durable by design — a daemon restart mid-prompt simply re-asks next tick (the task is still pending). Preflight rejects the gate when `telegram.enabled` is false (B05), so an enabled gate always has a transport. `_PendingScan` now also carries the task `title` for the prompt.

Auto-mode gating after each `run_task` ([cli.py:558](../../../src/wastech_orchestrator/cli.py#L558), [cli.py:564](../../../src/wastech_orchestrator/cli.py#L564)):

- `MANUAL_ACTION_REQUIRED` → break (always blocks continuation, regardless of auto-mode).
- auto-mode **off** → break after exactly one task.
- auto-mode **on** → continue to the next pending task (the next iteration re-checks the slot).

So `auto_mode=false` processes at most one task per pass; `auto_mode=true` drains consecutive pending tasks until the slot is busy, the queue empties, or a manual gate fires.

### The loop — `watch_loop`

Each iteration runs `orchestrator.refresh_repo()` then extends results with one `watch_once` ([cli.py:598](../../../src/wastech_orchestrator/cli.py#L598)). `refresh_repo` is a best-effort fetch + ff-only pull of `base_branch` that the Git Manager no-ops unless the working copy is on `base_branch` — i.e. only when the slot is free after terminal cleanup — so it never disturbs an active or interrupted task branch ([orchestrator.py:634](../../../src/wastech_orchestrator/core/orchestrator.py#L634)). This is how a task pushed to git **after** the daemon started becomes visible.

Termination is checked at the **top of the loop**, before the tick, and again after the between-tick wait. A stop is requested by either of two channels (`_stop_requested`): the `stop_event` (set by the POSIX `SIGTERM` handler) or the presence of the `stop_file` sentinel (the cross-platform channel `stop` writes). `poll_interval <= 0` runs exactly one tick with no sleep; `max_iterations` bounds the loop for tests. The between-tick wait is `stop_event.wait(poll_interval)` when an event is supplied — it returns the instant `SIGTERM` fires, cutting the sleep short — otherwise plain `sleep_fn(poll_interval)`; either way the loop then re-checks `_stop_requested` so a stop-file dropped during the wait is honored at the next tick boundary. Because both channels are consulted only at the loop top and around the wait, **a stop is respected only between ticks**: an in-flight `watch_once` (and the task stage inside it) always finishes.

### Daemon vs single pass — `cmd_watch`

`poll` is `--poll-seconds` when given, else `config.orchestrator.poll_interval_seconds` ([cli.py:953](../../../src/wastech_orchestrator/cli.py#L953)); the queue selector is `--queue` when given, else `config.orchestrator.queue`, threaded through `watch_loop` to `watch_once`. `restart` accepts the same two flags for the fresh loop it starts. The PID file lives at `<repo>/.worc/orchestrator.pid` — under `worc_home_for` (the gitignored runtime home), not the tracked `tasks/` root ([cli.py:959](../../../src/wastech_orchestrator/cli.py#L959), [cli.py:503](../../../src/wastech_orchestrator/cli.py#L503)).

When `config.git.create_pull_request` is set, `detect.require_gh()` fails fast (exit 2) before the loop starts, in both modes ([cli.py:951](../../../src/wastech_orchestrator/cli.py#L951)).

**Single pass (`poll <= 0`):** no PID file, no signal handler — just `_summarize_watch(watch_loop(..., poll_interval=poll))` ([cli.py:979](../../../src/wastech_orchestrator/cli.py#L979)).

**Daemon (`poll > 0`):**

1. Refuse a second watcher: if the recorded daemon is genuinely live, print "already running" and exit 1. Liveness goes through `running_daemon_pid`: on POSIX it matches the recorded start-time so a **recycled** PID (an unrelated process that reused the number) does not read as our daemon; on Windows, where liveness cannot be probed (`_can_signal` is false), PID-file _presence_ is the signal. A stale or recycled PID is not refused — it is overwritten on start (on Windows a leftover file from a crash is cleared by `stop` or by hand).
2. Enter a `StopController` context (installs the `SIGTERM` handler — a no-op for cross-process stop on Windows), clear any stale `stop_file` sentinel, then `write_pid_file(pid_path)`. Ordering matters: the handler is armed before the PID is published.
3. Run `watch_loop(..., stop_event=controller.event, stop_file=stop_path)`.
4. A `KeyboardInterrupt` (Ctrl-C) prints "watch: stopped" and exits 0.
5. A `finally` removes the PID file **and** the stop-file on every exit path — clean exit, Ctrl-C, error, or a process that survived a `SIGKILL` long enough to reach here.
6. If a stop was requested (the event was set, or the stop-file is present), the shutdown was graceful → print "watch: stopped", exit 0; otherwise summarize the processed tasks.

```mermaid
flowchart TB
  start(["cmd_watch"]) --> gh{"PR enabled?"}
  gh -->|yes, gh missing| fail2["exit 2"]
  gh -->|ok| mode{"poll > 0?"}
  mode -->|no| single["watch_loop one tick<br/>(no PID, no handler)"]
  mode -->|yes| guard{"recorded PID live?"}
  guard -->|yes| refuse["exit 1 (already running)"]
  guard -->|no| arm["StopController (arm SIGTERM)<br/>then write PID file"]
  arm --> loop["watch_loop with stop_event"]
  loop --> fin["finally: remove PID file"]
```

### stop / restart

`cmd_stop` calls `stop_process(pid_path, timeout=args.timeout, stop_file=stop_path)` and maps the `StopOutcome` to one of five operator messages — no PID file, stale PID cleared, escalated to SIGKILL, stopped, or (Windows) "did not confirm shutdown … cleared its PID file" — always returning exit 0. `cmd_restart` stops the recorded daemon (a _different_ process), reports whether one was running (or timed out), then delegates to `cmd_watch(args)` to run a fresh loop in-process — so it never needs to remember the old daemon's flags.

`stop_process` always writes the `stop_file` sentinel and then waits for shutdown by a **platform-split** strategy (`_can_signal`):

- **POSIX** (`_stop_via_signal`) — read the PID **record** (absent → `found=False` no-op), probe liveness with the recorded start-time (dead **or recycled** → reap the file, `already_dead=True`, never signaling the innocent recycled process), send `SIGTERM`, then poll `is_running` until `timeout`; if still alive, escalate to `SIGKILL` (`timed_out=True`). A `ProcessLookupError` (or Windows winerror 87, via `_is_no_such_process`) racing between probe and signal is swallowed as already-dead.
- **Windows** (`_stop_via_pid_file`) — `os.kill` cannot reach an unrelated process, so wait for the daemon to remove its own PID file (it does so on a clean exit); its disappearance confirms shutdown. If the file persists past `timeout` (wedged, or a stale file from a crash), clear it and report `timed_out=True` (no hard kill — the operator stops any survivor by hand).

The PID file and stop-file are unlinked in every terminal branch — the backstop for a `SIGKILL`ed (or crashed) daemon that could not remove its own files.

### SIGTERM bridge — `StopController` (POSIX)

A context manager that maps configured signals (default `SIGTERM`) to a `threading.Event`; the handler only calls `self.event.set()` — it never raises ([process_control.py](../../../src/wastech_orchestrator/process_control.py)). `__exit__` restores the previous disposition of each signal so a leaked handler can't corrupt later code (or later pytest tests); a previous handler of `None` (not installed from Python) is left alone since it cannot be restored. `signal.signal` only works on the main thread, so the controller must be entered there — the CLI does. This is the POSIX fast-path only: on Windows a cross-process `stop` cannot deliver `SIGTERM`, so the bridge never fires there and the stop-file is the sole stop channel.

## Invariants & guarantees

- **Resume before pending.** A pass always reconciles the single in-flight task before considering new work; a `MANUAL_ACTION_REQUIRED` resume short-circuits the pass ([cli.py:552](../../../src/wastech_orchestrator/cli.py#L552)).
- **One task at a time.** Every pending pick is gated on a free slot via `acquire_slot("")` ([cli.py:560](../../../src/wastech_orchestrator/cli.py#L560)); the single-slot invariant itself is owned by [B07](B07-state-machine-and-store.md)'s active-task query.
- **`manual_action_required` always blocks continuation**, independent of auto-mode ([cli.py:564](../../../src/wastech_orchestrator/cli.py#L564)).
- **One daemon per artifact root.** A genuinely-live recorded daemon makes `cmd_watch` refuse (exit 1); a stale **or recycled** PID is overwritten. On POSIX liveness is the start-time-guarded `os.kill` probe; on Windows (`_can_signal` false) liveness cannot be probed, so PID-file presence is the proxy — a leftover file from a crash reads as live until cleared by `stop` or by hand.
- **PID identity is recycling-proof (POSIX).** The PID file is JSON `{pid, start_time}`; POSIX liveness (`is_running` / `running_daemon_pid` / `stop_process`) matches the recorded start-time, so a recycled PID never reads as our daemon. When the start-time can't be read it degrades to a bare-PID probe. Windows has neither a cross-process probe nor a start-time source, so it relies on the daemon's self-managed PID file instead ([process_control.py](../../../src/wastech_orchestrator/process_control.py)).
- **A stop never interrupts a tick.** Both stop channels — the `SIGTERM`-set event (POSIX) and the polled `stop_file` (cross-platform) — are consulted only at the top of the loop and around the between-tick wait, never raised mid-tick ([process_control.py](../../../src/wastech_orchestrator/process_control.py), [cli.py](../../../src/wastech_orchestrator/cli.py)).
- **PID file and stop-file are always removed on daemon exit** via `finally`, and `stop_process` reaps both in every branch ([process_control.py](../../../src/wastech_orchestrator/process_control.py)).
- **`stop` is idempotent.** Absent, malformed (incl. a pre-JSON bare integer), stale, or recycled PID files signal nothing; a stray stop-file is reaped ([process_control.py](../../../src/wastech_orchestrator/process_control.py)).
- **PID write is atomic** — temp file + `os.replace` — so a concurrent `stop` never reads a half-written PID ([process_control.py:41](../../../src/wastech_orchestrator/process_control.py#L41)).
- **`refresh_repo` only acts when the slot is free**, guaranteed downstream by the Git Manager no-op off `base_branch` ([orchestrator.py:634](../../../src/wastech_orchestrator/core/orchestrator.py#L634)).

## Dependencies

- **Uses:** B06 (`resume`, `run_task`, `acquire_slot`, `refresh_repo` — all the actual work), B07 (the active-task query backing `acquire_slot`'s single slot), B16 (the pending task files and `tasks/` lifecycle dirs that `select_pending` scans), B05 (`poll_interval_seconds`, `auto_mode.enabled`, `create_pull_request`), B27 (runtime logging). The execution spine `run_task`/`resume` invoke is the flow engine ([B28](B28-flow-engine.md)) over a node graph ([B29](B29-flow-definition-and-validation.md), [B30](B30-flow-node-runners.md)) — opaque to this block.
- **Used by:** B01 (dispatches `watch` / `stop` / `restart`).

## Tests

- `tests/test_cli_watch.py` — `pending_dir` resolves under the bound repo, not the cwd (`test_pending_dir_is_under_the_bound_repo`); `watch_loop` stop semantics: pre-set event skips the first tick, an event set mid-tick prevents a second tick, the no-event path uses `sleep_fn` and sleeps only between ticks, and the **stop-file** equivalents (a pre-existing sentinel skips the first tick; one created mid-tick prevents a second); daemon control: `stop` is idempotent with no PID file, clears a stale PID (POSIX path forced), `watch` refuses a second instance, writes-then-removes the PID file, **clears a stale stop-file on start and reaps it on exit**, `restart` stops the previous watcher then delegates the flags to `cmd_watch`; and `require_gh` fast-fail is exercised on (and skipped off) `create_pull_request`.
- `tests/test_process_control.py` — round-trip / parent-dir creation and tolerant `read_pid` (absent / empty / garbage); `is_running` truth table (signalable, no-such-process incl. the Windows winerror-87 case, `PermissionError`); `stop_file_path`/`stop_file_requested`; `running_daemon_pid` (start-time recycling guard when signalling; file-presence when not); `stop_process` **POSIX path** (`can_signal=True`: absent no-op, stale reap, graceful SIGTERM-only, SIGKILL escalation after timeout, stop-file write/reap) and **Windows file path** (`can_signal=False`: graceful via PID-file-disappearance, timeout when it persists); and `StopController` installing the handler, setting the event, and restoring the prior disposition on exit — all with injected OS seams, no real processes or signals.
