# Interactive operator console (non-blocking, live, attended)

Status: **proposed** (2026-06-23 — design + recommendation; not locked — see [§ Decision (recommended)](#decision-recommended)) Date: 2026-06-23 · **Refreshed 2026-06-28** Owner: Vladimir Makarevich

> **Refresh note (2026-06-28).** This ADR was drafted before roadmap steps 1–12 (+7a) landed (see [implementation-roadmap.md](implementation-roadmap.md)). It is the **capstone** (step 13) and now builds on those finished seams; the review has been folded in inline. The load-bearing changes: (1) **the stop ladder is no longer POSIX-only** — `process_control.py` is now a documented POSIX/Windows split (stop-file sentinel `orchestrator.stop` + self-removed PID file on Windows, where `os.kill`/process-groups can't reach the daemon cross-process), so the hard `--force-full` rung needs a **platform split**, not a `killpg`; (2) **`recent_tasks()` already exists** (landed with `worc list`, step 6 — reuse, don't add); (3) the pending queue is **priority-ranked + queue-partitioned**, not FIFO; (4) the task dir is **config-resolved** (`paths.tasks_dir` → `pending_dir(config)`), not hardcoded `tasks/`; (5) **`prs`/`merge-task` are built** (drop the future tense), plus a new `tasks` verb; (6) **`logging.*`** now decides which artifacts exist (so `logs <id>` tailing is conditional); (7) the transient **resumable-pause** has a concrete shape (`running` + `blocked_since`); (8) two **Telegram confirmation gates** now exist the console should _surface_. CONFIG is at v23, DB at v13 — neither bumped by the console itself.

Detail file for the backlog idea _"can we make working through the terminal more comfortable — not block operator input while a task runs, the way Claude Code does in the terminal?"_ It records the original sketch, what the orchestrator's terminal experience already looks like (traced), why the generic "add a REPL" answer is half-aimed here, the improved design (an attended **console that is a client over the existing daemon**, not a second engine host), the rejected alternatives, and a phased plan ([§ План реализации](#план-реализации)).

> **Related:** [Task discovery: `worc list` + shell completion](archive/done/cli-task-list-and-completion.md) is the small, dependency-free sibling of this item — a one-shot `worc list` + shell-level Tab-completion for task ids. It **landed** (roadmap step 6), and with it the shared `recent_tasks(limit)` read helper that `worc top` (Phase 1) now **reuses**, so the two share a read-surface and do not collide. Note the read helpers (`recent_tasks` plus the active/pending/recent triple `cmd_list` already computes via `_list_sections`) live **inline in `cli.py`**, not a separate module — `worc top` reuses them directly (or via a small extraction). The in-console prompt_toolkit completer stays with `worc shell` here.

## The idea (original)

> Can we make working through the terminal more comfortable — not block operator input, the way Claude Code does in the terminal?

The original note answered generically: a foreground CLI command always blocks the shell until it exits; what Claude Code does is closer to an interactive REPL/TUI that owns stdin and runs work in the background. So: keep the plain commands, add an interactive mode (`mytool shell` → `run` / `jobs` / `cancel` / `logs`), built on [`prompt_toolkit`][1] with `patch_stdout()` so background log lines print _above_ the prompt; spawn external work with [`asyncio.create_subprocess_exec`][2]; reach for [Textual][3] if a full panel/TUI is wanted. The one firm rule it landed on is right and survives intact: **exactly one layer may read stdin — the REPL/TUI; background work must never call `input()`.**

That is sound generic CLI advice. The improvement below is to **aim it at this orchestrator specifically** — because three of the sketch's load-bearing assumptions are false here, and getting them wrong would fight the architecture rather than extend it.

## TL;DR (findings)

1. **The orchestrator is already non-blocking — for the path that matters.** `watch` is a long-running daemon ([cli.py:1096-1161](../../src/wastech_orchestrator/cli.py#L1096-L1161)) that scans `tasks/pending`, runs one task to completion, sleeps, repeats ([watch_loop](../../src/wastech_orchestrator/cli.py#L723-L762)); `stop`/`restart`/`status` are one-shot commands you issue from any other shell while it runs ([cli.py:1164-1224](../../src/wastech_orchestrator/cli.py#L1164-L1224)); and the coding agents run as **fully-captured subprocesses** (stdin `DEVNULL`, stdout streamed to a file, stderr piped — [providers/process.py:15-17,62](../../src/wastech_orchestrator/providers/process.py#L15-L17)), so they never occupy the operator's terminal. The only genuinely blocking command is `worc run <file>` ([cli.py:765-791](../../src/wastech_orchestrator/cli.py#L765-L791)) — and that is the deliberate one-shot/CI path. **So the missing thing is not "unblock stdin." It is a single attended place to watch the queue + the one active task + live progress and issue control commands, without juggling terminals and re-running `status`.**
2. **There is no "jobs" plural — the slot is single by invariant.** `acquire_slot` refuses a second task whenever `find_active_tasks()` shows another non-terminal task ([orchestrator.py:408-410](../../src/wastech_orchestrator/core/orchestrator.py#L408-L410)); the slot is DB-driven (a non-terminal `tasks.status`), not a lock file. So the sketch's `asyncio.create_task` fan-out of N parallel jobs **directly contradicts** the architecture. The real model is: **one active task + a queue of pending files + a tail of recent terminal tasks.** **The queue is no longer FIFO** (it was when this ADR was first drafted): since `task-priority` (step 3) and `multi-instance-task-queues` (step 5) landed, `watch_once` filters pending files by `queue` (`config.orchestrator.queue` or `--queue`) and sorts the survivors **priority-descending** (`high|mid|low`, filename as tie-break) via `_scan_pending_meta` ([cli.py:888-963](../../src/wastech_orchestrator/cli.py#L888-L963)). So the console's view must mirror that order **and** queue filter — not raw `select_pending` filename order — or `top` will show a different sequence (and other queues' files) than the daemon will actually run.
3. **A console must be a _client_ over the daemon, never a second engine host.** The biggest design error available is running the pipeline _inside_ the REPL process (the sketch's `asyncio.to_thread(blocking_command)`). `run_task` is a deeply synchronous, single-slot, durable-state pipeline ([orchestrator.py:365-406](../../src/wastech_orchestrator/core/orchestrator.py#L365-L406)); hosting it in a second process would duplicate the daemon and the slot logic. The right shape: the console **spawns or attaches to the `watch` daemon** (the only engine host), **polls `state.db` read-only** (`StateStore.open_readonly`, the same path `status` uses), **tails the daemon's log file**, and **enqueues** by dropping task files into the **config-resolved** pending dir — `pending_dir(config)` = `<repo>/<paths.tasks_dir>/pending` ([cli.py:787-789](../../src/wastech_orchestrator/cli.py#L787-L789)), **not** a hardcoded `tasks/pending` (since `configurable-tasks-dir`, step 4). The engine stays where it is.
4. **Cancellation is honestly limited — and the console must say so.** There is no mid-stage cancel: the only graceful interruption is the daemon's **between-ticks stop**, which lets the in-flight task finish its current stage. That stop is now **cross-platform** (since `windows-cross-platform-support`, step 1): the watch loop polls both a `threading.Event` (POSIX `SIGTERM`) **and** a stop-file sentinel `orchestrator.stop`, exiting between ticks when either fires ([watch_loop](../../src/wastech_orchestrator/cli.py#L966-L1017), [process_control.py:37,94-105](../../src/wastech_orchestrator/process_control.py#L37-L105)). So a console `cancel` can only **de-queue a pending file** or **stop/restart the daemon between ticks** — it cannot kill a running agent mid-stage. True mid-task cancellation (cooperative flag + subprocess termination) is a separate, larger feature.
5. **HITL — and now two confirmation gates — are already Telegram, not stdin.** Human-in-the-loop questions go out over Telegram and the task waits on the reply or a timeout ([core/hitl.py](../../src/wastech_orchestrator/core/hitl.py), [notify/telegram.py](../../src/wastech_orchestrator/notify/telegram.py)). Since this ADR was drafted, three more Telegram round-trips landed: a one-way **step-trace** push per node (`telegram.trace`, step 9), a fail-closed **next-task gate** (`auto_mode.confirm_next_task`, step 10), and a durable **max-turns gate** (`max_turns_gate`, step 10). Bridging any of these into the console prompt stays a **distinct, deferred feature** — Telegram remains the answer channel. **But the read-only `top` view should _surface_ that a gate is pending** (read, not answer): the max-turns gate leaves a durable `turn-gate-*.json` interaction with `status: "waiting"` under `hitl/` ([hitl.py:286-300,325-366](../../src/wastech_orchestrator/core/hitl.py#L286-L366)), discoverable via `iter_task_interactions`, so `top` can render "awaiting operator (max-turns gate)". The next-task gate is **non-durable** (no artifact — re-asked each tick), so the console cannot reliably show it; note the asymmetry.
6. **The command set already exists — the console is a dispatcher.** `status`, `finalize`, `rerun`, `stop`, `restart`, `list`, and now `prs` / `merge-task <id>` / `tasks` (all built — `orchestrator-driven-pr-merge`, step 11) are the verbs ([cmd_prs](../../src/wastech_orchestrator/cli.py#L1251), [cmd_merge_task](../../src/wastech_orchestrator/cli.py#L1355), [cmd_tasks](../../src/wastech_orchestrator/cli.py#L1419)). A console reuses these `cmd_*` functions; it adds no orchestration logic, keeping "the core does not know the CLI" intact and the surface DRY. One nuance the console must respect: `merge-task` and `prs --sync` slot-guard by **refusing while the watch daemon's PID file is live** (`running_daemon_pid` — [cli.py:1364](../../src/wastech_orchestrator/cli.py#L1364)), i.e. they refuse whenever the daemon is up at all (idle or busy) — stronger than a `find_active_tasks()` "task active" probe. A console supervising a spawned daemon must expect these two verbs to refuse until that daemon is stopped.
7. **Stopping is unconditional today, a hard mid-stage stop is not even possible, and the stop path is now cross-platform.** `worc stop` ([cmd_stop](../../src/wastech_orchestrator/cli.py#L1727-L1749)) takes only `--timeout` — **no idle/busy distinction, no confirmation**. On **POSIX** it writes the stop-file then fires `SIGTERM` (immediate wakeup) and escalates to `SIGKILL` after the timeout **on the daemon PID only** ([_stop_via_signal](../../src/wastech_orchestrator/process_control.py#L313-L369)); on **Windows** it cannot signal cross-process at all, so it writes the stop-file and **waits for the daemon to remove its own PID file**, with **no `SIGKILL` escalation** ([_stop_via_pid_file](../../src/wastech_orchestrator/process_control.py#L372-L405), platform-selected by [_can_signal](../../src/wastech_orchestrator/process_control.py#L40-L50)). And because agents still launch via plain `subprocess.run` with **no process group / no `start_new_session`** ([process.py:88-100](../../src/wastech_orchestrator/providers/process.py#L88-L100)), even the POSIX `SIGKILL` would **orphan the running agent**. So the "finish the current step, then stop softly" behavior exists (it _is_ the between-ticks stop, signal-or-stop-file) but is neither gated nor named, and an "instantly stop all agents" behavior has **no mechanism at all on either platform** (`os.killpg`/`getpgid`/`taskkill` appear nowhere in `src/`). Both are addressed by the stop-safety design below (§ Stop safety) — which, post-Windows, must be a **platform split**, not POSIX-only signal escalation.

Net: ~70% of this is **presentation + process supervision** over machinery that already exists (the daemon, the PID file, read-only `state.db`, file logging, the lifecycle dirs, the one-shot `cmd_*` verbs — now including `recent_tasks`, `prs`/`merge-task`/`tasks`, and the cross-platform stop-file primitive). The genuinely new code is (a) a small **prompt_toolkit event loop** that tails a log stream above a live prompt and supervises/attaches a `watch` child, and (b) the **stop ladder**, which must be a POSIX/Windows split — its hard rung is the only part with no existing mechanism on either platform.

## How the operator interacts today (traced)

1. **`worc run <file>`** — runs exactly one task file synchronously to completion and returns ([cli.py:765-791](../../src/wastech_orchestrator/cli.py#L765-L791) → [run_task](../../src/wastech_orchestrator/core/orchestrator.py#L365-L406)). Blocks the shell. This is the CI/script path and stays as-is.
2. **`worc watch`** — the daemon. With `poll_interval > 0` it loops: refresh `base_branch`, `watch_once` (resume any in-flight task, then pick **the highest-priority pending task in the served queue** when the slot is free), sleep ([watch_loop](../../src/wastech_orchestrator/cli.py#L966-L1017), [watch_once](../../src/wastech_orchestrator/cli.py#L888-L963)). `poll_interval <= 0` is a single pass. It writes a PID file `orchestrator.pid` so `stop`/`restart` can find it. Tasks flow through the lifecycle dirs under the **config-resolved** `<paths.tasks_dir>/{pending,processing,done,failed}` (default `tasks/`); `tasks/rejected` lives under the `.worc/` runtime root, not `paths.tasks_dir`.
3. **`worc stop` / `worc restart`** — locate the daemon via its PID record and request a graceful, between-ticks stop. Cross-platform since step 1: it **always writes a stop-file** (`orchestrator.stop`), then on **POSIX** also sends `SIGTERM` (→ a `threading.Event`) and escalates to `SIGKILL` after `--timeout`; on **Windows** it sends no signal and instead waits for the daemon to delete its own PID file (no `SIGKILL` escalation) ([cmd_stop](../../src/wastech_orchestrator/cli.py#L1727-L1749), [stop_process](../../src/wastech_orchestrator/process_control.py#L251-L405)). The watch loop polls **both** the event and the stop-file between ticks ([watch_loop](../../src/wastech_orchestrator/cli.py#L966-L1017)). Graceful, never mid-stage — and **unconditional**: no active-task check, no confirmation, and (on POSIX) the timeout `SIGKILL` targets only the daemon PID (the agent subprocess is not in its own group, so it would be orphaned, not killed). Recovery after any abrupt exit is already handled: `resume()` re-enters the persisted flow node on the next start.
4. **`worc status`** — read-only, DB-only report of the active or latest task ([cli.py:1225-1282](../../src/wastech_orchestrator/cli.py#L1225-L1282)), via `StateStore.open_readonly`. It is a **one-shot snapshot of one task** — re-run it to refresh, and it does not show the queue.
5. **Logging** — configured once at startup to stderr (logfmt/JSON) or, with `--log-file`, a rotating file (10 MB, 5 backups; **still no default path** — so the console must pass `--log-file <known path>` when it spawns the daemon). Since `log-management` (step 12), `logging.level` sets default verbosity (the `--log-level` flag overrides) and `logging.artifacts` (`minimal|standard|full`, default `standard`) governs which per-attempt provider files survive: `minimal` keeps only `result.json`, `standard` adds `stdout.log`/`stderr.log`, `full` keeps everything ([LoggingConfig](../../src/wastech_orchestrator/config/schema.py#L385-L398)). Agent stdout goes to artifact files, not the console. So a separate process can observe progress two ways: poll `state.db` read-only, and tail the daemon's `--log-file`. **Caveat for the console's `logs <id>`:** tailing `stdout.log`/`stderr.log` only works under `standard`/`full`; under `minimal` only `result.json` exists.

The gap the operator actually feels: to follow a run they keep one shell on `worc watch` (or its log) and another re-running `worc status`, and they enqueue by hand-copying files into the configured pending dir (`pending_dir(config)`). There is no single attended surface.

## Constraints that bound any solution

From [.agents/rules/architecture.md](../../.agents/rules/architecture.md) and the code as it stands:

1. **Single processing slot.** At most one active task. The console shows _one_ active task + a queue + recent terminal tasks — never parallel jobs. Mutating one-shot verbs (`finalize`/`rerun`) assume an idle slot, so the console must refuse them while a task is active — the same guard those commands already apply; `merge-task`/`prs --sync` go further and refuse while the **daemon PID is live at all** (`running_daemon_pid`), so the console must stop a supervised daemon before invoking them.
2. **Only the orchestrator does commit / push / PR / merge.** The console issues none of these directly; it enqueues files and invokes existing `cmd_*` verbs. No new git/network capability is introduced.
3. **The core does not know the CLI.** The console lives in the CLI layer ([cli.py](../../src/wastech_orchestrator/cli.py)) and dispatches to existing `cmd_*` functions; it adds no logic to `core/`.
4. **One stdin reader.** The prompt*toolkit loop is the \_only* stdin reader; the daemon child runs with stdin not inherited (it already does — [providers/process.py:16](../../src/wastech_orchestrator/providers/process.py#L16)). Background log lines print via `patch_stdout()` so they never corrupt the input line.
5. **No secrets in logs/artifacts.** The console only **renders** existing redacted log/DB content; the `RedactionFilter` and stderr-redaction already in place are unchanged. The console writes nothing new to logs.
6. **No new dependency in the hot path.** `prompt_toolkit` (and any later Textual) ships as an **optional extra** (`pip install wastech-orchestrator[shell]`); the daemon and headless/CI installs never import it. `worc shell` degrades to a clear "install the extra" message if it is absent.
7. **Read-only DB access is safe concurrently.** The console polls via `StateStore.open_readonly` — the exact path `status` already uses while a daemon may be active ([state_store.py:236](../../src/wastech_orchestrator/state_store.py#L236)); it never opens a writable connection.

## Design

### Shape: a console client over the daemon (not an engine host)

```text
┌─ worc shell (prompt_toolkit, owns stdin) ─────────────────┐
│  • renders a live header: active task + node, queue, recent│
│  • tails the daemon log → prints above the prompt          │
│  • dispatches commands → existing cmd_* / enqueue          │
│           │ spawns or attaches                             │
│           ▼                                                │
│   worc watch (the ONLY engine host — unchanged)            │
│     run_task → flow engine → captured agent subprocesses   │
│           │ writes                                         │
│           ▼                                                │
│   state.db  +  --log-file  +  <paths.tasks_dir>/pending|…  │
└────────────────────────────────────────────────────────────┘
```

The console never imports the engine. It supervises a `watch` **child** (or attaches to a detached one), reads `state.db` read-only, tails the log file, and enqueues files. This is the whole architectural decision; everything else is ergonomics.

### CLI surface (two deliverables, shippable independently)

- **`worc top`** _(Phase 1 — the read-only core, also usable standalone)_ — a live, auto-refreshing, **read-only** monitor: a header (active `task_id` / current node / attempt, from the flow checkpoint; plus a **"parked (resumable)"** marker when the active task is `running` with a non-null `blocked_since`, and a **"gate pending"** marker when a durable max-turns gate waits — see § Live view) + the pending queue (scanned from `pending_dir(config)` with the same `_scan_pending_meta` front-matter read `watch_once` uses, **filtered to the served queue and sorted priority-descending** so it matches what the daemon will actually run) + a tail of recent terminal tasks (the **existing** `recent_tasks(limit)` / `latest_task`) + the last N daemon log lines. No stdin beyond `q` to quit. Polls `state.db` read-only on an interval. This alone delivers most of the "see what's happening without blocking" value and carries near-zero architectural risk. (Equivalently surfaced as `worc status --follow` if a line-based refresh is preferred over a full-screen view.)
- **`worc shell`** _(Phase 2 — interactive control on top of the monitor)_ — a `prompt_toolkit` `PromptSession` with `patch_stdout()`. The live header/log stream from `top` renders above the prompt; the prompt accepts commands that map onto existing verbs:

  | console command | maps to | notes |
  | --- | --- | --- |
  | `run <file>` / `enqueue <file>` | copy file into `pending_dir(config)` | **non-blocking**: the daemon picks it up next tick; foreground blocking `run` stays the bare CLI command. The file's `priority`/`queue` front-matter sets its rank and which daemon serves it |
  | `ps` / `jobs` | the `top` view | one active + queue + recent — never parallel (≈ a live `worc list` / `worc tasks`) |
  | `status [<id>]` | `cmd_status` | one-shot snapshot inline |
  | `logs [<id>]` | tail artifact `stdout.log` / daemon log | read-only; only when `logging.artifacts` ≠ `minimal` (else only `result.json` exists). Distinct from the top-level `worc logs clean` |
  | `prs` / `merge-task <id>` | `cmd_prs` / `cmd_merge_task` | **built** (step 11); both refuse while the daemon PID is live (`running_daemon_pid`), so the console must stop the supervised daemon first |
  | `tasks` | `cmd_tasks` | read-only full task list (superset of recent); overlaps `ps` |
  | `finalize` / `rerun` | `cmd_finalize` / `cmd_rerun` | slot-guarded (refuse while a task is active) |
  | `up` / `down [--force\|--force-full]` / `restart` | spawn child / `cmd_stop` / `cmd_restart` | daemon lifecycle; `down` follows the **stop ladder** (§ Stop safety): idle → no prompt; busy → confirm `YES` / `--force` (soft) or `--force-full` (hard — **POSIX-only**; Windows degrades to soft) |
  | `cancel <id>` | de-queue pending file (→ `.worc/tasks/rejected`), or `down` | **best-effort** — see § Stop safety / Cancellation |
  | `quit` / Ctrl-D | detach (busy) or stop the supervised child (idle), exit | busy + spawned child → offer **detach** (default) / soft / `--force-full`; detached daemons are always left running |

  `worc shell` is a **dispatcher**: each command calls an existing `cmd_*` function or a thin enqueue/tail helper. It adds no orchestration logic.

### Daemon: spawn vs attach

On start, `worc shell` checks the PID file ([read_pid_record](../../src/wastech_orchestrator/process_control.py#L97-L120)):

- **A live daemon is recorded** → **attach**: tail its `--log-file`, poll its `state.db`, leave it running on exit. (Detached/unattended runs keep using bare `worc watch`.)
- **No live daemon** → offer to **spawn** one as a managed child (`worc watch --log-file <known path> [--queue <name>]`, launched as an argv list, no shell — same no-shell-out invariant as everywhere else), capture its output into the `patch_stdout` stream, and `down` it on `quit`. Spawning requires the daemon to log to a file the console knows; the console picks that path and passes `--log-file`. **Pass the queue through** too (`--queue`, or honor `orchestrator.queue`) so the spawned daemon serves the intended queue, and **filter the `top` queue view to that same selector** — otherwise the console shows pending files the daemon will silently ignore.

This gives the "one process to sit in" feel (spawn) without precluding the detached daemon (attach).

### Live view: where the data comes from

- **Active task + node**: read-only `state.db` poll — `find_active_tasks()` + the flow checkpoint (`current_node` / counters) the orchestrator already persists.
- **Parked (resumable) indicator**: a task that is `Status.RUNNING` **with a non-null `tasks.blocked_since`** is parked on a sustained provider outage (the "B-lite soft pause" `transient-provider-failure-recovery` added — [_park](../../src/wastech_orchestrator/core/orchestrator.py#L1602-L1621)). The console renders it as "paused — every provider down since `<blocked_since>`" by reading the `blocked_since` column. **No new status** (it stays `running`); a column read, which fits the read-only model.
- **Pending gate indicator**: a durable max-turns gate leaves a `turn-gate-*.json` interaction with `status: "waiting"` under `hitl/` ([iter_task_interactions](../../src/wastech_orchestrator/core/hitl.py#L325-L366)); the console surfaces "awaiting operator" without bridging the answer. (The next-task gate is non-durable and not reliably observable.)
- **Queue**: scan `pending_dir(config)` and apply the **same queue filter + priority sort as `watch_once`** (`_scan_pending_meta`), so the displayed order matches what the daemon runs — not raw `select_pending` filename order.
- **Recent terminal**: the **existing** `recent_tasks(limit)` ([state_store.py:646-659](../../src/wastech_orchestrator/state_store.py#L646-L659)) plus `latest_task` — landed with `worc list` (step 6); reuse, don't add.
- **Progress lines**: tail the daemon `--log-file` (already redacted, structured). Agent stdout artifact files are tailed on demand by `logs <id>` — present only when `logging.artifacts` ≠ `minimal`.

No new tables, no schema bump for the console — purely new **read** views + presentation over columns the sibling features already added (`blocked_since`).

### Stop safety: idle/busy gate + two force levels

Stopping must be **safe when a task is in flight and frictionless when nothing is running**. This is one behavior shared by the bare `worc stop`/`restart` commands **and** the console's `down`/`quit` — implementing it once in `stop_process` + `cmd_stop` upgrades both. The model is a three-rung **stop ladder**, keyed on whether a task is active (a read-only `find_active_tasks()` probe — the daemon may be alive but idle between ticks, which counts as idle).

**Cross-platform caveat (post step 1).** `process_control.py` is now a documented POSIX/Windows split ([_can_signal](../../src/wastech_orchestrator/process_control.py#L40-L50)): the graceful stop is **always** driven by the `orchestrator.stop` sentinel file, and only **POSIX** adds `SIGTERM`→`SIGKILL` on top; **Windows cannot signal the daemon cross-process at all** and instead waits for it to remove its own PID file. So the ladder below is **not** a POSIX-only signal escalation — the soft rung rides on the stop-file on both platforms, and the hard rung is **POSIX-only** (Windows has no built-in mechanism — `os.killpg`/`taskkill` are absent from `src/`).

| invocation | **idle** (no active task) | **busy** (a task is active) |
| --- | --- | --- |
| `worc stop` / console `down` (no flag) | **stop, no prompt** | **refuse** — interactive TTY: prompt `Type YES to stop (a task is active)`; non-interactive (CI/pipe): exit non-zero, "pass `--force` or `--force-full`" |
| `--force` _(or typed `YES`)_ | stop, no prompt | **soft stop**: stop-file (+ POSIX `SIGTERM`) → the daemon finishes the **current step**, then exits between ticks (today's between-ticks path — now explicitly named and gated; works on both platforms) |
| `--force-full` | stop, no prompt | **hard stop (POSIX only)**: terminate the **active agent process group immediately** (mid-stage), then exit — recovery is the existing `resume()` on next start. **On Windows this degrades** to the soft stop (no cross-process group kill); a wedged daemon needs a `taskkill` backstop, tracked as a follow-up |

Design points:

1. **Idle is always frictionless.** With no active task there is nothing to lose, so any form (no flag, `--force`, `--force-full`) just stops — no prompt. The gate fires **only** when `find_active_tasks()` is non-empty.
2. **Busy without an explicit go-ahead is refused.** Consequential by default: an interactive operator is prompted for `YES`; a non-interactive caller (no TTY) must pass `--force`/`--force-full` (there is no one to prompt). Mirrors the existing `-y/--yes` precedent on `finalize`, inverted to "confirm only when busy".
3. **`YES`/`--force` is the _soft_ rung; hard kill needs the explicit `--force-full` flag.** The interactive `YES` shortcut maps to the graceful path only — destroying in-flight agent work is deliberate enough to require typing the dedicated flag, never an interactive shortcut.
4. **Soft already exists on both platforms; hard is the genuinely new, POSIX-only capability.** The soft rung is the current between-ticks stop (stop-file + POSIX `SIGTERM`). The hard rung needs plumbing that does not exist today (§ TL;DR #7) and is **POSIX-only**: agents must launch in **their own process group** (`start_new_session=True` in [providers/process.py](../../src/wastech_orchestrator/providers/process.py#L88-L100) — **not set today**) so the daemon (or `stop`) can signal the whole group without orphaning it; `stop --force-full` then resolves the daemon's group (`os.getpgid(pid)`) and `os.killpg(...)` (daemon + active agent + any checks subprocess) and lets `resume()` recover. **None of `start_new_session`/`getpgid`/`killpg` exist in `src/` yet.** On **Windows** these primitives are unavailable (`os.killpg`/`os.getpgid` raise; `start_new_session` is a no-op) and `os.kill` can't reach the daemon cross-process; the real equivalent is `taskkill /F /T`, an explicitly deferred Windows follow-up (recorded in [windows-cross-platform-support.md](archive/done/windows-cross-platform-support.md) / [follow_ups.md](follow_ups.md)). So `--force-full` must be a **platform split** mirroring the existing `_can_signal` one — POSIX does the group-kill; Windows **degrades to soft** plus a clear "hard stop unavailable on Windows; stop via Task Manager / `taskkill`" message. The OS seams (`getpgid`/`killpg`) should be injectable like the existing `kill_fn`/`now_fn`.
5. **`restart`** reuses the same ladder for its stop half (a busy `restart` confirms/forces exactly like `stop`).

`cancel <id>` rides on this: a **pending** task (file in `pending_dir(config)`, no active run) is simply moved out (into `.worc/tasks/rejected`, which lives under the runtime root, **not** `paths.tasks_dir`) so the daemon never picks it up; an **active** task has no clean per-task cancel — `cancel` routes to the stop ladder (`down` soft, or `--force-full` to kill now on POSIX). The console must **state the rung plainly** rather than imply an instant kill at the soft level (and that the hard kill is POSIX-only).

Still deferred (§ Out of scope): a **clean cooperative cancel** that unwinds the flow and marks the task `cancelled` _without_ a hard group-kill — a richer feature than `--force-full`'s "kill now, rely on resume".

### Dependency & packaging

Add `prompt_toolkit` under a new `shell` optional-dependency group in [pyproject.toml](../../pyproject.toml) (`[project.optional-dependencies] shell = [...]`). `worc top`/`worc shell` import it lazily and, if missing, exit with `install wastech-orchestrator[shell]`. The daemon, the engine, and all non-console commands never import it — the hot path stays lean.

## Rejected alternatives

- **B — in-process engine REPL (the sketch verbatim).** Run `run_task` inside the REPL via `asyncio.to_thread`, fan out "jobs" as `asyncio.create_task`. **Rejected:** it makes a second engine host competing with the daemon, contradicts the single-slot invariant (the "jobs" plural does not exist), and re-implements slot/resume/PID logic the daemon already owns. The console-as-client (above) reuses all of it.
- **Textual full TUI now.** A panel/log/job-table TUI is genuinely nice, but it is a heavier dependency and more surface than the first cut needs. **Deferred:** `prompt_toolkit` + `patch_stdout` covers "live logs above a prompt" with far less. Revisit Textual only if operators want panes/mouse/hotkeys (§ Out of scope).
- **A _clean cooperative_ per-task cancel.** Unwinding the flow gracefully, marking the task `cancelled`, and cleaning up partial state — distinct from the in-scope `--force-full` hard group-kill (which kills now and leans on `resume()`). **Deferred** to a follow-up: `--force-full` covers "stop the agents now"; the clean unwind is a richer feature few operators need before the hard stop exists.
- **HITL-over-stdin in the console.** Bridging Telegram HITL prompts into the console prompt is desirable but a separate feature with its own routing/timeout semantics. **Deferred**; Telegram stays the HITL channel for now.
- **A daemon control _socket_/IPC.** A Unix-socket RPC between console and daemon would enable richer live control. **YAGNI for now:** read-only `state.db` polling + log tailing + file enqueue + signals cover the first cut without a new protocol. Revisit if the console needs to push commands into a running task.

## Decision (recommended)

Add an **attended operator console that is a client over the existing `watch` daemon, not a second engine host.** Ship in two independently-useful pieces: **`worc top`** (a read-only, auto-refreshing live monitor — active task + node, a **parked/gate-pending** indicator, the **queue-filtered, priority-sorted** pending queue, recent terminal tasks via the existing `recent_tasks`, a tail of the daemon log; polls `state.db` read-only) and **`worc shell`** (a `prompt_toolkit` + `patch_stdout` REPL layering command dispatch onto that view — `enqueue`/`status`/`logs`/`prs`/`merge-task`/`tasks`/`finalize`/`rerun`/`up`/`down`/`restart`/best-effort `cancel`). The console **spawns or attaches to** the daemon (passing `--log-file` and `--queue`), **enqueues** by dropping files into the config-resolved `pending_dir(config)`, **slot-guards** mutating verbs, and reuses the existing `cmd_*` functions. `prompt_toolkit` is an **optional `[shell]` extra**; the daemon never imports it.

Stopping follows a **three-rung stop ladder** (§ Stop safety), shared by the bare `worc stop`/`restart` and the console's `down`/`quit`: **idle → stop with no prompt**; **busy + no flag → refuse** (interactive: confirm `YES`; non-interactive: require a flag); **busy + `--force`/`YES` → soft stop** (finish the current step, then exit between ticks — today's between-ticks behavior, now gated and named, working on both platforms via the stop-file); **busy + `--force-full` → hard stop** (kill the active agent's process group immediately, recover via `resume()`). The soft rung exists today; the hard rung adds the missing, **POSIX-only** plumbing (launch agents with `start_new_session=True` so a group-kill never orphans them) and must be a **POSIX/Windows split** — on Windows it degrades to soft, since `os.killpg`/`taskkill` are unavailable/unbuilt (the accepted Windows stop limitation). **The console adds no task status and no schema change** — only new read views, presentation, and the stop-ladder gate. (For context: CONFIG is now v23 and DB v13 because of the sibling features the console renders — `blocked_since`, the gates, `logging.*` — not because of the console.)

This reframes the original idea: the orchestrator is _already_ non-blocking (daemon + captured subprocesses + one-shot control verbs); what is missing is a single attended surface to watch the single-slot queue, drive control, and **stop safely** — so the upgrade is **presentation + process supervision + a stop gate**, not a new execution model.

Deliberately **out of scope** (YAGNI / greenfield-MVP): an in-process engine REPL (Option B); parallel "jobs" (single-slot invariant); a full Textual TUI (revisit if panes/hotkeys are wanted); a **clean cooperative per-task cancel** that unwinds the flow and marks the task `cancelled` (distinct from the in-scope `--force-full` hard kill); HITL-over-stdin (Telegram stays the channel); a daemon control socket/IPC.

## Building blocks (kept from the original sketch)

The library choices the original note surfaced are the right ones for the recommended design:

- **`prompt_toolkit` + `patch_stdout()`** — the input layer that lets daemon log lines print above a live prompt without corrupting it. ([python-prompt-toolkit][1])
- **`asyncio.create_subprocess_exec`** — for spawning/supervising the `watch` child and streaming its output (argv list, no shell). ([Python docs][2])
- **Textual** — the deferred richer option if a full panel TUI is ever wanted. ([Textual][3])

Trimmed skeleton (the console event loop — _supervise + tail + dispatch_, not run the engine):

```python
# cli_shell.py (illustrative)
import asyncio
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

async def tail_daemon(log_path, child=None):
    """Stream the daemon's log file (or the spawned child's output) above the prompt."""
    ...  # follow log_path; print(line) — patch_stdout keeps the prompt intact

async def main(config):
    session = PromptSession("worc> ")
    child = await spawn_or_attach_watch(config)      # spawn `worc watch --log-file <p> --queue <q>` OR attach to PID file
    tail = asyncio.create_task(tail_daemon(log_path, child))
    with patch_stdout():
        while True:
            try:
                line = (await session.prompt_async()).strip()
            except (EOFError, KeyboardInterrupt):
                break
            cmd, *rest = line.split() or [""]
            if cmd in {"enqueue", "run"}:   enqueue(rest[0])          # copy into pending_dir(config)
            elif cmd in {"ps", "jobs"}:     render_view(open_readonly(db))  # 1 active + queue + recent
            elif cmd == "status":           cmd_status(...)
            elif cmd in {"down"}:           cmd_stop(...)             # stop ladder: idle→go; busy→YES/--force (soft) | --force-full (hard)
            elif cmd in {"quit", "exit"}:   break                    # busy+spawned → offer detach (default) / soft / --force-full
            # … finalize / rerun / prs / merge-task / cancel (best-effort) …
    tail.cancel()
    await stop_child_if_spawned(child)
```

[1]: https://python-prompt-toolkit.readthedocs.io/ "Prompt Toolkit — Read the Docs"
[2]: https://docs.python.org/3/library/asyncio-subprocess.html "asyncio Subprocesses — Python docs"
[3]: https://textual.textualize.io/ "Textual"

## План реализации

Раздел на русском по просьбе владельца. Рекомендованное решение — **операторская консоль как клиент над существующим демоном `watch` (не второй хост движка)**: `worc top` (read-only live-монитор) + `worc shell` (prompt_toolkit-REPL поверх него). Деление на «Фазы» ниже — логическая структура работ, а не отдельные итерации/мержи. **Проверки и документация — один раз в самом конце** (`/run-checks`, затем `/sync-docs` и `prettier` по докам) — см. [§ Проверки и документация](#проверки-и-документация).

> **Обновлено 2026-06-28.** Этот ADR — капстоун (шаг 13); шаги 1–12 (+7a) дорожной карты уже реализованы, и план ниже учитывает их по факту: `recent_tasks()` **уже существует** (шаг 6) — переиспользуем, не пишем заново; путь к задачам **конфигурируемый** (`paths.tasks_dir` → `pending_dir(config)`), не хардкод `tasks/`; очередь **приоритетная + по очередям** (`priority`/`queue`), не FIFO; `prs`/`merge-task` **готовы** (шаг 11, плюс новый `tasks`); `logging.*` (шаг 12) решает, какие артефакты вообще существуют; транзиентная пауза имеет конкретный вид (`running` + `blocked_since`); появились две Telegram-гейта, которые консоль должна **показывать** (но не отвечать). Главное: **стоп-лестница больше не POSIX-only** — `process_control.py` теперь платформенный сплит (стоп-файл `orchestrator.stop` + самоудаляемый PID на Windows), поэтому жёсткий `--force-full` — только POSIX, на Windows деградирует в мягкий.

Целевой сквозной сценарий: оператор запускает `worc shell` → консоль поднимает (или подхватывает) демон `watch` (с `--log-file` и `--queue`), логи демона текут **над** промптом → оператор делает `enqueue task.md` (файл падает в `pending_dir(config)` = `<repo>/<paths.tasks_dir>/pending`, демон берёт его на следующем тике по приоритету и очереди, ввод не блокируется) → `ps` показывает одну активную задачу + очередь (отфильтрованную по очереди и отсортированную по приоритету) + недавние терминальные → `down` при активной задаче спрашивает `YES` (или принимает `--force`) и мягко гасит после текущего шага; `--force-full` убивает агентов сразу **(только POSIX; на Windows деградирует в мягкий стоп)**; при idle гасит без вопросов.

### Зафиксированные решения (ответы на форки)

1. **Консоль — клиент, не хост движка.** Движок остаётся только в демоне `watch`; консоль читает `state.db` read-only (`StateStore.open_readonly`), тейлит лог-файл демона, кладёт файлы в `tasks/pending`, диспатчит существующие `cmd_*`. Вариант B (in-process REPL с `to_thread`) отклонён — см. [§ Rejected alternatives](#rejected-alternatives).
2. **Один слот, не «jobs».** Вид — одна активная задача + очередь + недавние терминальные. Никакого параллельного запуска.
3. **Два независимо полезных артефакта.** `worc top` (read-only, фаза 1) и `worc shell` (интерактив, фаза 2). `top` ценен сам по себе.
4. **`prompt_toolkit` — опциональный extra `[shell]`.** Демон и headless/CI его не импортируют; `worc top`/`shell` без него падают с понятным сообщением.
5. **Без нового статуса задачи и без bump схемы** — только новые read-вью + презентация.
6. **Стоп — лестница из трёх ступеней (idle/busy + два уровня force), кросс-платформенно.** idle → стоп без подтверждения; busy без флага → отказ (интерактив: подтверждение `YES`; не-интерактив: требуется флаг); busy + `--force`/`YES` → мягкий стоп (доделать текущий шаг, выйти между тиками — сегодняшнее поведение через стоп-файл, на обеих платформах, теперь под гейтом); busy + `--force-full` → жёсткий стоп **(только POSIX: убить группу процессов активного агента сразу; на Windows деградирует в мягкий — `os.killpg`/`taskkill` недоступны/не реализованы)**, восстановление через `resume()`. Применяется и к базовым `worc stop`/`restart`, и к консольным `down`/`quit`. См. [§ Stop safety](#stop-safety-idlebusy-gate--two-force-levels).
7. **Отмена — best-effort поверх лестницы стопа.** `cancel` снимает pending-файл или маршрутизирует в стоп-лестницу (`down` мягко / `--force-full` жёстко). Чистый кооперативный per-task cancel (размотать флоу + пометить `cancelled` без group-kill) — отдельная фича (см. [§ Out of scope](#decision-recommended)).

### Фаза 1 — read-surface + live-монитор `worc top`

- **[state_store.py](../../src/wastech_orchestrator/state_store.py)** — `recent_tasks(limit) -> list[TaskRow]` **уже существует** ([state_store.py:646-659](../../src/wastech_orchestrator/state_store.py#L646-L659), приземлён вместе с `worc list`, шаг 6) — **переиспользуем, не добавляем**. Без bump схемы.
- **[cli.py](../../src/wastech_orchestrator/cli.py)** — сабпарсер `top` + `cmd_top`: периодический read-only-опрос `state.db` (активная задача + чекпойнт ноды; **индикатор «parked»** при `running` + ненулевом `blocked_since`; **индикатор «gate pending»** при ждущем `turn-gate-*.json` через `iter_task_interactions`), скан `pending_dir(config)` тем же `_scan_pending_meta` + **фильтром по очереди и сортировкой по приоритету, как в `watch_once`** (а не сырой `select_pending`), недавние через существующий `recent_tasks`, хвост лог-файла демона; перерисовка раз в N секунд; выход по `q`. Чисто чтение. Хелперы чтения сейчас **инлайн в `cli.py`** (`_list_sections`/`_task_entry`/…) — переиспользуем напрямую или мелкой экстракцией. (Опционально: тот же вывод как `status --follow`.)
- **Тесты:** `recent_tasks` уже покрыт (шаг 6); `cmd_top` рендер из фейкового `state.db` + временного `pending_dir` (одна активная + очередь с разными `priority`/`queue` + терминальные + parked-задача с `blocked_since`) без сети и без движка.

### Фаза 2 — интерактивная консоль `worc shell` + supervise/attach демона

- **[pyproject.toml](../../pyproject.toml)** — новая группа `[project.optional-dependencies] shell = ["prompt_toolkit>=3"]`. Ленивая загрузка; при отсутствии — выход с подсказкой `install wastech-orchestrator[shell]`.
- **`cli_shell.py`** (новый модуль в пакете) — `PromptSession` + `patch_stdout()`: рендер шапки/лога из фазы 1 над промптом; диспетчер команд на существующие `cmd_*` + тонкие хелперы `enqueue`/`tail`; **spawn-or-attach** демона через `read_pid_record` ([process_control.py:131](../../src/wastech_orchestrator/process_control.py#L131)) — живой демон подхватываем (tail лога + read-only DB, на выходе оставляем), иначе поднимаем `worc watch --log-file <path> [--queue <name>]` дочерним процессом (argv-список, без shell) и гасим на `quit` через `cmd_stop`. **`--queue` обязательно прокидываем** (или из `orchestrator.queue`), и `top`-вид фильтруем по тому же селектору.
- **[cli.py](../../src/wastech_orchestrator/cli.py)** — сабпарсер `shell` + `cmd_shell` (вход в loop). Команды: `enqueue/run <file>` (копия в `pending_dir(config)`), `ps/jobs`, `status [<id>]`, `logs [<id>]` (хвост `stdout.log` — только при `logging.artifacts` ≠ `minimal`), `prs`/`merge-task <id>`/`tasks` (**готовы**, шаг 11), `finalize`/`rerun`, `up`/`down [--force|--force-full]`/`restart`, `cancel <id>` (best-effort), `quit`. **Slot-guard:** мутационные `finalize`/`rerun` отказывают при активной задаче (`find_active_tasks`); `merge-task`/`prs --sync` уже отказывают, **пока жив PID демона** (`running_daemon_pid`) — консоль со своим демоном должна это учитывать (сначала погасить демон). `down`/`quit` идут через стоп-лестницу (Фаза 3).
- **Тесты:** диспетч `enqueue` → файл в `pending_dir`; `ps` рендер (с приоритетом/очередью); slot-guard (`finalize` при активной → refuse; `merge-task` при живом демоне → refuse); spawn/attach логика через фейковый PID-файл и фейковый `watch` (скилл `fake-cli` для дочернего процесса); `cancel` pending → файл уехал в `.worc/tasks/rejected`. prompt_toolkit-loop тестируем через инъекцию заранее заданного списка строк (не реальный TTY).

### Фаза 3 — стоп-лестница (idle/busy + `--force`/`--force-full`) + изоляция группы процессов (платформенный сплит)

Апгрейдит **базовые** `worc stop`/`restart` (не только консоль) — поэтому ценен сам по себе и не зависит от фаз 1–2; консольные `down`/`quit` его переиспользуют. **Важно (после шага 1):** `process_control.py` уже платформенный сплит ([_can_signal](../../src/wastech_orchestrator/process_control.py#L40-L50)) — мягкий стоп везде на стоп-файле (`orchestrator.stop`), POSIX добавляет `SIGTERM`/`SIGKILL`, Windows ждёт самоудаления PID-файла. Поэтому жёсткий `--force-full` — **только POSIX**; на Windows `os.killpg`/`os.getpgid` недоступны, `os.kill` не достаёт демон кросс-процессно, а эквивалент `taskkill /F /T` — отложенный, не реализованный follow-up.

- **[providers/process.py](../../src/wastech_orchestrator/providers/process.py)** — запускать агента в собственной группе процессов: `start_new_session=True` в `subprocess.run(...)` ([process.py:88-100](../../src/wastech_orchestrator/providers/process.py#L88-L100)) — **сейчас не выставлен**. Без этого `--force-full` осиротит агента, а не убьёт. На Windows `start_new_session` — no-op (нужен `creationflags=CREATE_NEW_PROCESS_GROUP` + `taskkill`, отложено). (Поведение `timeout`/захвата stdout/stderr не меняется; argv-список, без shell.)
- **[process_control.py](../../src/wastech_orchestrator/process_control.py)** — `stop_process(...)` получает уровень жёсткости: `soft` (текущее: стоп-файл + POSIX `SIGTERM`, эскалация в `SIGKILL` по `--timeout`; на Windows — ожидание PID-файла — мягкая остановка между тиками, на обеих платформах) и `full` **(POSIX-only)** (резолв группы демона `os.getpgid(pid)` + `os.killpg(pgid, SIGKILL)` — убить демон + активного агента + субпроцесс проверок разом; **на Windows — деградация в `soft` + понятное сообщение «жёсткий стоп недоступен на Windows»**). Все OS-seam'ы (`getpgid`/`killpg`) — инъектируемые, как уже сделано для `kill_fn`/`now_fn`. Сплит по образцу существующего `_can_signal`.
- **[cli.py](../../src/wastech_orchestrator/cli.py)** — `stop`/`restart` получают `--force`/`--force-full` (взаимоисключающие) рядом с `--timeout` ([cli.py:236-243](../../src/wastech_orchestrator/cli.py#L236-L243)); `cmd_stop` сначала read-only-проба `find_active_tasks()`: **idle → стоп без подтверждения** (любая форма); **busy без флага** → интерактивный TTY: запрос `YES` (мягко), не-TTY → выход не-ноль с подсказкой про флаги; **busy + `--force`/`YES`** → `stop_process(level=soft)`; **busy + `--force-full`** → `stop_process(level=full)` (POSIX) / деградация в soft (Windows). Подтверждение по образцу `-y/--yes` у `finalize`, но инвертировано в «спрашиваем только когда busy».
- **Тесты:** idle → стоп без промпта (все три формы); busy без флага + не-TTY → refuse, ничего не сигналим; busy + `--force` → soft путь; busy + `--force-full` на POSIX → `killpg` группы (через инъекцию `getpgid`/`killpg` + `can_signal=True`); busy + `--force-full` на Windows (`can_signal=False`) → деградация в soft + сообщение; `YES` в TTY → soft; агент стартует с `start_new_session=True` на POSIX (через перехват kwargs `subprocess.run`); `--force-full` на idle = обычный стоп.

### Инварианты (соблюдены)

- **Один слот**: вид — одна активная + очередь; мутационные команды slot-guard'ятся.
- **Коммит/пуш/PR/merge — только оркестратор**: консоль лишь enqueue'ит файлы и зовёт существующие `cmd_*`; новых git/сетевых возможностей нет.
- **Ядро не знает CLI**: консоль живёт в слое `cli.py`, логики в `core/` не добавляет; стоп-лестница — в `cli.py`/`process_control.py`.
- **Один читатель stdin**: prompt_toolkit-loop; дочерний демон stdin не наследует; фоновые логи — через `patch_stdout()`.
- **Без секретов**: консоль только рендерит уже редактированные лог/DB-данные; своих логов не пишет.
- **Без shell-интерполяции**: дочерний `watch` и агенты запускаются argv-списком; `start_new_session` не вводит shell.
- **Безопасный стоп**: жёсткий стоп возможен только по явному `--force-full`; восстановление после любого резкого выхода — существующий `resume()`.
- **Без новой зависимости в hot-path**: `prompt_toolkit` — extra `[shell]`, демон его не импортирует.

### Связи и хвосты

- **`prs`/`merge-task`/`tasks`** ([orchestrator-driven-pr-merge.md](archive/done/orchestrator-driven-pr-merge.md)) — **уже реализованы** (шаг 11); консоль подхватывает их как обычные `cmd_*`. Нюанс: `merge-task`/`prs --sync` отказывают, пока жив PID демона.
- **Жёсткий стоп на Windows (`taskkill`)** — `--force-full` сейчас POSIX-only; Windows-эквивалент (`creationflags=CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T`) — отложенный follow-up (унаследован из [windows-cross-platform-support.md](archive/done/windows-cross-platform-support.md)). До него на Windows жёсткий стоп деградирует в мягкий. Записать в [follow_ups.md](follow_ups.md).
- **Чистый кооперативный per-task cancel** — отдельный backlog-айтем: размотать флоу и пометить задачу `cancelled` **без** group-kill (богаче, чем `--force-full`). Записать в [follow_ups.md](follow_ups.md).
- **HITL/гейты в консоли** — мост Telegram-HITL ([core/hitl.py](../../src/wastech_orchestrator/core/hitl.py)) и двух confirmation-гейтов в промпт консоли (ответ, не только показ); отдельная фича. Telegram остаётся каналом ответа; консоль на старте лишь **показывает** ждущий durable-гейт. Записать в [follow_ups.md](follow_ups.md).
- **Textual-TUI** — богатый вариант с панелями/хоткеями, если понадобится. Записать в [follow_ups.md](follow_ups.md).
- **Control-socket/IPC** — если консоли понадобится пушить команды в активную задачу (а не только enqueue + сигналы). Записать в [follow_ups.md](follow_ups.md).

### Проверки и документация

Всё — **один раз после всех фаз**.

- **Проверки:** `ruff check .`, `mypy src`, `pytest` (через `/run-checks`); затем `npx prettier@3 --write "**/*.md"` по затронутым докам.
- **Документация (`/sync-docs`):** [Functional Map](../functional/index.md) (блок CLI/демона — добавить `top`/`shell` рядом с `watch`/`status`/`stop`/`restart`; отметить, что консоль — клиент демона, не хост движка; обновить `stop`/`restart` под стоп-лестницу + изоляцию группы процессов агента); [docs/operations.md](../operations.md) — операторская заметка про `worc top`/`worc shell` (spawn/attach, enqueue в `tasks/pending`, slot-guard) и про стоп-лестницу (`idle`/`busy`, `--force` мягко vs `--force-full` жёстко); [docs/configuration.md](../configuration.md)/README — extra `[shell]` и `pip install wastech-orchestrator[shell]`; при изменении топологии — модель C4 в [docs/likec4](../likec4).
- **Отложенные хвосты** — в [follow_ups.md](follow_ups.md) (см. § Связи и хвосты): чистый кооперативный per-task cancel, HITL-в-консоли, Textual-TUI, control-socket.
