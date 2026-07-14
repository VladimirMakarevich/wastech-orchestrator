# B02 — Watch Daemon and Task Scheduling

> Reconstructed from code (`cli.py` watch surface, `process_control.py`) and tests (`tests/test_cli_watch.py`, `tests/test_process_control.py`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/cli.py`, `src/wastech_orchestrator/process_control.py`

## Responsibility

Turn the one-shot `run` pipeline into a stoppable polling service: discover task files promoted (or git-pushed) into `tasks/pending`, resume any single in-flight task first, then feed pending tasks to the orchestrator one at a time under the auto-mode rule. The daemon owns its lifecycle — PID/stop/children handles, a refuse-second-instance guard, and a cross-platform stop predicate (POSIX event plus sentinel everywhere) that also reaches FlowEngine for cooperative node-boundary parking. All scheduling logic lives in `cli.py`; PID/signal plumbing lives in print-free, seam-injectable `process_control.py`.

The block schedules; it does not execute. Task execution, resumption, slot ownership, and base-branch refresh are all `Orchestrator` methods owned by [B06](B06-orchestrator-pipeline.md); this block only sequences calls to them.

## Public surface

- `select_pending` ([cli.py:535](../../../src/wastech_orchestrator/cli.py#L535)) — pending `.md`/`.json` task files in a deterministic (sorted) order.
- `watch_once` ([cli.py:542](../../../src/wastech_orchestrator/cli.py#L542)) — one scheduling pass: resume, then drain pending under the auto-mode rule.
- `watch_loop` ([cli.py](../../../src/wastech_orchestrator/cli.py)) — `refresh_repo` → `watch_once` → chunked sleep/event-wait, bounded by `poll_interval`/`max_iterations`; the same event/sentinel predicate is injected into active flow execution.
- `cmd_watch` / `cmd_stop` / `cmd_restart` ([cli.py:935](../../../src/wastech_orchestrator/cli.py#L935), [cli.py:1002](../../../src/wastech_orchestrator/cli.py#L1002), [cli.py:1021](../../../src/wastech_orchestrator/cli.py#L1021)) — the operator subcommands, dispatched by [B01](B01-cli-and-operator-commands.md).
- `pending_dir` ([cli.py:522](../../../src/wastech_orchestrator/cli.py#L522)) — `<repo>/tasks/pending`.
- `pid_file_path`, `stop_file_path`, `stop_file_requested`, `write_pid_file`, `read_pid`, `is_running`, `running_daemon_pid`, `stop_process`, `StopController`, `_can_signal` ([process_control.py:36](../../../src/wastech_orchestrator/process_control.py#L36) onward) — the PID/stop-file/signal plumbing.

## Behavior

### Single scheduling pass — `watch_once`

A pass resumes the in-flight task **first**, before touching the pending queue ([cli.py:552](../../../src/wastech_orchestrator/cli.py#L552)). `orchestrator.resume()` returns the resumed task's terminal result, or `None` when the slot is free ([orchestrator.py:643](../../../src/wastech_orchestrator/core/orchestrator.py#L643)). If the resume lands in `MANUAL_ACTION_REQUIRED`, the pass returns immediately and **no pending task is picked** ([cli.py:555](../../../src/wastech_orchestrator/cli.py#L555)) — a stuck human-in-the-loop task blocks the queue rather than piling new work behind it.

Otherwise the pass reads each pending file's `id`/`depends_on`/`priority`/`queue` with the cheap `_scan_pending_meta` front-matter scan, **drops any task whose `queue` does not equal this instance's selector** (`--queue` if given, else `config.orchestrator.queue`; a missing/malformed queue folds to `"default"` in the scan), then sorts the surviving list by `(priority_rank, filename)` so eligible tasks run `high → mid → low` with filename as the tie-break (an unrecognised/absent `priority` sorts as `mid`), then iterates. The queue filter runs before the sort and the `depends_on` map is built, so out-of-queue tasks are invisible to this instance — a cross-queue dependency is just an unmerged dependency that keeps the dependent WAITING. `depends_on` stays stronger than priority: a higher-priority task still waiting on an unmerged dependency is skipped, so a lower-priority eligible task runs ahead of it. For each file the pass re-checks the slot via `acquire_slot("")` ([cli.py:560](../../../src/wastech_orchestrator/cli.py#L560)). `acquire_slot` is true iff no task with a non-empty id is active ([orchestrator.py:377](../../../src/wastech_orchestrator/core/orchestrator.py#L377)); the empty-string argument means "is the slot free for _anyone_". A busy slot breaks the loop. On a free slot, a pending file whose `id` already reached a terminal status and whose stored `source_path` is that same file is **skipped** (`_already_settled`) — a `manual_action_required` task keeps its file in `pending/` for the operator, and re-running it would only re-reject it as `duplicate_task_id` and quarantine the operator's file; the skip is non-blocking (like a WAITING dependency) and does not consume the auto-mode-off "one task" budget. A _different_ file colliding on a used id is not the task's own leftover, so it falls through to `orchestrator.run_task(str(task_file))` and the gate's loud duplicate reject. Resolving a settled task (`rerun`/`finalize`) stays the operator's call.

**Next-task confirmation gate (idea 27, opt-in).** When `orchestrator.auto_mode.confirm_next_task` is on, the pass calls `_confirm_next_task` immediately after the dependency check and **before** `run_task` — an `orchestrator.notifier.ask_human` approve/deny carrying the task id + front-matter title only (no diff/prompt). Approve → claim and run it. Deny / timeout / transport error → `break` (leave the task pending, stop chaining for this cycle — fail-closed STOP; a silent operator never advances an autonomous claim). It gates **new claims only**: the resume-first step above is never gated. It is non-durable by design — a daemon restart mid-prompt simply re-asks next tick (the task is still pending). Preflight rejects the gate when `telegram.enabled` is false (B05), so an enabled gate always has a transport. `_PendingScan` now also carries the task `title` for the prompt.

Auto-mode gating after each `run_task` ([cli.py:558](../../../src/wastech_orchestrator/cli.py#L558), [cli.py:564](../../../src/wastech_orchestrator/cli.py#L564)):

- `MANUAL_ACTION_REQUIRED` → break (always blocks continuation, regardless of auto-mode).
- auto-mode **off** → break after exactly one task.
- auto-mode **on** → continue to the next pending task (the next iteration re-checks the slot).

So `auto_mode=false` processes at most one task per pass; `auto_mode=true` drains consecutive pending tasks until the slot is busy, the queue empties, or a manual gate fires.

### The loop — `watch_loop`

Each iteration runs `orchestrator.refresh_repo()` then extends results with one `watch_once` ([cli.py:598](../../../src/wastech_orchestrator/cli.py#L598)). `refresh_repo` is a best-effort fetch + ff-only pull of `base_branch` that the Git Manager no-ops unless the working copy is on `base_branch` — i.e. only when the slot is free after terminal cleanup — so it never disturbs an active or interrupted task branch ([orchestrator.py:634](../../../src/wastech_orchestrator/core/orchestrator.py#L634)). This is how a task pushed to git **after** the daemon started becomes visible.

Termination is checked at the **top of the loop**, before the tick, and during/after the between-tick wait. A stop is requested by either of two channels (`_stop_requested`): the `stop_event` (set by the POSIX `SIGTERM` handler) or the presence of the `stop_file` sentinel (the cross-platform channel `stop` writes). The idle wait polls in short chunks so the file channel is responsive even when `poll_interval` is large. During an active `watch_once`, the same predicate is injected through the composition root into FlowEngine and Router: the engine completes the current node and its post-node bookkeeping, saves the transition, then raises `FlowCancelled` before the next node; the Core parks the task resumably. A single long-running node still needs the timeout escalation or explicit `--force-full`.

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

`cmd_stop` calls `stop_process` with the PID/stop/children paths plus the injected process-tree killer, then maps `StopOutcome` to an operator message: absent/stale watcher, cooperative stop, explicit hard stop, automatic hard escalation after the grace timeout, or an unconfirmed timeout when no hard-kill seam exists. `cmd_restart` starts its replacement only after the prior watcher was confirmed stopped or hard-killed; an unconfirmed timeout returns non-zero and keeps the original PID handle.

`stop_process` always writes the `stop_file` sentinel and then waits for shutdown by a **platform-split** strategy (`_can_signal`):

- **POSIX** (`_stop_via_signal`) — read the PID **record** (absent → `found=False` no-op), probe liveness with the recorded start-time (dead **or recycled** → reap the file, `already_dead=True`, never signaling the innocent recycled process), send `SIGTERM`, then poll `is_running` until `timeout`; if still alive, escalate to `SIGKILL` (`timed_out=True`). A `ProcessLookupError` (or Windows winerror 87, via `_is_no_such_process`) racing between probe and signal is swallowed as already-dead.
- **Windows** (`_stop_via_pid_file`) — `os.kill` cannot reach an unrelated process, so wait for the daemon to remove its own PID file (it does so on a clean exit). If the file persists past `timeout`, invoke the injected `taskkill /F /T` tree-kill for the daemon and recorded agent, return `timed_out=True` + `tree_killed=True`, and reap all runtime handles. Without a hard-kill seam, keep PID/stop/children intact and report the unconfirmed timeout; this blocks a duplicate watcher and preserves the target for a later `--force-full`.

Confirmed terminal branches unlink PID/stop/children. An unconfirmed Windows timeout deliberately retains all three. With no PID record, POSIX reaps stale handles because it can probe liveness; Windows preserves them because PID absence cannot prove that an older watcher is dead. A fresh watcher and the exiting watcher both clean their own stale handles.

### SIGTERM bridge — `StopController` (POSIX)

A context manager that maps configured signals (default `SIGTERM`) to a `threading.Event`; the handler only calls `self.event.set()` — it never raises ([process_control.py](../../../src/wastech_orchestrator/process_control.py)). `__exit__` restores the previous disposition of each signal so a leaked handler can't corrupt later code (or later pytest tests); a previous handler of `None` (not installed from Python) is left alone since it cannot be restored. `signal.signal` only works on the main thread, so the controller must be entered there — the CLI does. This is the POSIX fast-path only: on Windows a cross-process `stop` cannot deliver `SIGTERM`, so the bridge never fires there and the stop-file is the sole stop channel.

## Invariants & guarantees

- **Resume before pending.** A pass always reconciles the single in-flight task before considering new work; a `MANUAL_ACTION_REQUIRED` resume short-circuits the pass ([cli.py:552](../../../src/wastech_orchestrator/cli.py#L552)).
- **One task at a time.** Every pending pick is gated on a free slot via `acquire_slot("")` ([cli.py:560](../../../src/wastech_orchestrator/cli.py#L560)); the single-slot invariant itself is owned by [B07](B07-state-machine-and-store.md)'s active-task query.
- **`manual_action_required` always blocks continuation**, independent of auto-mode ([cli.py:564](../../../src/wastech_orchestrator/cli.py#L564)).
- **A settled id is never re-run.** A pending file whose `id` already reached a terminal status and is that task's own leftover (`source_path` match) is skipped by `_already_settled` — the daemon leaves `manual_action_required` files in `pending/` for the operator rather than re-picking them into a `duplicate_task_id` reject.
- **One daemon per artifact root.** A genuinely-live recorded daemon makes `cmd_watch` refuse (exit 1); a stale **or recycled** PID is overwritten. On POSIX liveness is the start-time-guarded `os.kill` probe; on Windows (`_can_signal` false) liveness cannot be probed, so PID-file presence is the proxy — a leftover file from a crash reads as live until cleared by `stop` or by hand.
- **PID identity is recycling-proof (POSIX).** The PID file is JSON `{pid, start_time}`; POSIX liveness (`is_running` / `running_daemon_pid` / `stop_process`) matches the recorded start-time, so a recycled PID never reads as our daemon. When the start-time can't be read it degrades to a bare-PID probe. Windows has neither a cross-process probe nor a start-time source, so it relies on the daemon's self-managed PID file instead ([process_control.py](../../../src/wastech_orchestrator/process_control.py)).
- **A soft stop is node-boundary cooperative.** It never interrupts the running node during the grace period; the next node remains checkpointed and untouched. `--force-full` interrupts immediately, and timeout escalation bounds a wedged node.
- **Runtime handles survive uncertainty.** PID/stop/children are removed after confirmed exit or hard kill; an unconfirmed Windows timeout retains them so escalation and duplicate-watcher prevention remain possible.
- **`stop` is idempotent without cancelling itself.** Absent/malformed/stale PID records signal nothing. POSIX reaps provably stale handles; Windows preserves an existing stop request when PID evidence is unavailable.
- **PID write is atomic** — temp file + `os.replace` — so a concurrent `stop` never reads a half-written PID ([process_control.py:41](../../../src/wastech_orchestrator/process_control.py#L41)).
- **`refresh_repo` only acts when the slot is free**, guaranteed downstream by the Git Manager no-op off `base_branch` ([orchestrator.py:634](../../../src/wastech_orchestrator/core/orchestrator.py#L634)).

## Dependencies

- **Uses:** B06 (`resume`, `run_task`, `acquire_slot`, `refresh_repo` — all the actual work), B07 (the active-task query backing `acquire_slot`'s single slot), B16 (the pending task files and `tasks/` lifecycle dirs that `select_pending` scans), B05 (`poll_interval_seconds`, `auto_mode.enabled`, `create_pull_request`), B27 (runtime logging). The execution spine `run_task`/`resume` invoke is the flow engine ([B28](B28-flow-engine.md)) over a node graph ([B29](B29-flow-definition-and-validation.md), [B30](B30-flow-node-runners.md)) — opaque to this block.
- **Used by:** B01 (dispatches `watch` / `stop` / `restart`).

## Tests

- `tests/test_cli_watch.py` — loop stop-channel behavior, daemon PID/sentinel lifecycle, successful restart delegation, and refusal to start a replacement after an unconfirmed stop.
- `tests/test_process_control.py` — POSIX graceful/SIGKILL behavior; Windows PID-file confirmation, handle retention without a kill seam, automatic tree-kill escalation with daemon + agent cleanup, and a later explicit full stop after an unconfirmed soft timeout.
- `tests/core/test_flow_engine.py` / `tests/core/test_orchestrator.py` — boundary cancellation saves the untouched next node, parks without fallback or terminal artifacts, and resumes that node exactly once.
