# Interactive operator console — remediation plan (post-implementation review)

Status: **in progress** (2026-07-02 — review + remediation plan; **Phase R1 implemented**, suite green, Windows validation pending) Owner: Vladimir Makarevich Parent ADR: [cli-upgrade.md](cli-upgrade.md)

> This document is the output of a deep re-review of the [Interactive operator console](cli-upgrade.md) capstone (roadmap step 13) after it landed. A third-party review flagged that the feature was implemented "not fully / not in full scope". This traces the shipped code against the ADR requirement-by-requirement, records where it diverges, and gives a phased fix plan. It writes no code — it is the remediation backlog for the console.

## TL;DR

The presentation and dispatch surface is **substantially complete and conformant**: `worc top` and `worc shell` exist, are wired as clients over the daemon (never a second engine host), reuse the existing `cmd_*` verbs, filter+sort the queue exactly as `watch_once` does, surface the parked/gate markers, ship `prompt_toolkit` as an optional `[shell]` extra with a lazy import, and the stop-ladder **decision logic** (idle/busy × `--force`/`--force-full`, Windows soft-degrade) is correct and well-tested.

The gap is concentrated in **one critical correctness defect and a cluster of cross-platform / single-reader / ergonomic issues**:

- **🔴 C1 — the marquee Phase-3 capability (`--force-full` hard stop) was non-functional and actively dangerous. ✅ FIXED in R1 (2026-07-02).** The process-group topology was **inverted**: `start_new_session=True` was on the **agent** (`providers/process.py`), not the daemon, and the daemon never led its own group. So the group-kill both (a) **orphaned the very agent it was meant to kill** and (b) for a console-spawned daemon **SIGKILLed the operator's own console/shell**. The test suite was green only because it injected fake `getpgid`/`killpg` and never exercised the real topology. R1 inverts the topology (daemon leads the group, agent inherits it) and adds a real Windows `taskkill /F /T` tree-kill; a real-topology POSIX test now guards it. Windows real-smoke validation is pending (owner).
- **🟠 H1 — two stdin readers.** Console `down` (busy, interactive, no flag) falls through to `_confirm_yes` → `input()` **inside** the `prompt_toolkit` REPL, violating the ADR's load-bearing "exactly one stdin reader" invariant.
- **🟡 M1–M3 / ⚪ L1–L5 — cross-platform (`shlex.split` mangles Windows paths, already a known-but-unfixed follow-up), attach-without-log ergonomics, quit-while-busy UX, redundant tailers, forwarding overhead, and a functional-map doc gap.**

Net: the console _looks_ done and its happy path works, but its single genuinely-new capability (hard stop) is broken in a way tests can't see, and it trips two of the ADR's own invariants (one stdin reader; cross-platform mandatory). Fixing C1 + H1 is the real "make it fully implemented" work; the rest is polish.

## Conformance matrix (ADR requirement → shipped code)

| # | ADR requirement | Status | Evidence / gap |
| --- | --- | --- | --- |
| P1.1 | `worc top` read-only live monitor | ✅ conformant | `cmd_top` / `_run_top_loop` / `render_top` ([cli.py](../../../../src/wastech_orchestrator/cli.py) 2494–2643) |
| P1.2 | Queue filtered to served queue + priority-sorted like `watch_once` | ✅ conformant | `build_top_snapshot` uses `scan_pending_sorted(pending_dir, selector)`, not raw `select_pending` |
| P1.3 | Parked (`running` + `blocked_since`) marker | ✅ conformant | `_ActiveView.parked_since`; render emits "paused — every provider unavailable since …" |
| P1.4 | Durable max-turns gate-pending marker | ✅ conformant | `_has_pending_gate` via `iter_task_interactions`; non-durable next-task gate correctly not surfaced |
| P1.5 | Reuse existing `recent_tasks(limit)` (don't re-add) | ✅ conformant | `store.recent_tasks(recent_limit)` |
| P1.6 | Tail daemon `--log-file`; `q` to quit | ✅ conformant | `tail_lines`; `_stdin_quit_watcher` |
| P2.1 | `worc shell` prompt_toolkit REPL, dispatcher only (no orchestration) | ✅ conformant | [cli_shell.py](../../../../src/wastech_orchestrator/cli_shell.py) `dispatch` forwards to `cmd_*` |
| P2.2 | `[shell]` optional extra, lazy import, clean "install the extra" fallback | ✅ conformant | `pyproject.toml` `shell = ["prompt_toolkit>=3"]`; `_prompt_toolkit_available()` guard before spawn |
| P2.3 | Spawn-or-attach the daemon; pass `--log-file` and `--queue`; argv list, no shell | ✅ conformant | `spawn_or_attach_watch` → `spawn_detached` (argv, `shell=False`, stdin `DEVNULL`) |
| P2.4 | `enqueue` into config-resolved `pending_dir(config)` | ⚠️ partial | Works on POSIX; **M1** — `shlex.split` (POSIX mode) mangles Windows absolute paths |
| P2.5 | Slot-guard mutating verbs; `merge-task`/`prs --sync` refuse while daemon PID live | ✅ conformant (delegated) | Forwarded to the existing `cmd_*`, whose own guards apply |
| P2.6 | `cancel` de-queues pending → `.worc/tasks/rejected`, else routes to stop ladder | ⚠️ partial | Same **M1** `shlex.split` path issue for a file arg |
| P2.7 | One stdin reader (prompt_toolkit only; background never `input()`) | ❌ violated | **H1** — `down`→`cmd_stop`→`_confirm_yes()`→`input()` inside the REPL |
| P2.8 | Attach leaves daemon running; can tail only if `--log-file` known | ⚠️ weak | **M2** — a detached `worc watch` has no default log path, so attach usually shows "(no output)" |
| P2.9 | Quit while busy+spawned → offer detach (default)/soft/`--force-full` | ⚠️ partial | **M3** — `_shutdown_daemon` just leaves it running with a note; no soft/force-full offer |
| P3.1 | Stop-ladder decision: idle→stop; busy+no-flag→refuse; `--force`/`YES`→soft; `--force-full`→hard | ✅ conformant | `_resolve_stop_level` ([cli.py](../../../../src/wastech_orchestrator/cli.py) 2118) |
| P3.2 | Soft rung rides the stop-file on both platforms; Windows waits for PID-file removal | ✅ conformant | `_stop_via_signal` / `_stop_via_pid_file`; `_can_signal` split |
| P3.3 | `--force-full` hard rung: group-kill so **nothing is orphaned** (POSIX); Windows now tree-kills | ✅ fixed (R1) | POSIX kills the daemon's group (daemon leads it, agent inherits); Windows `taskkill /F /T`. **Windows smoke pending** |
| P3.4 | Agents launch in a group so a group-kill never orphans them | ✅ fixed (R1) | `run_process` no longer `setsid`s (inherits the daemon group); `spawn_detached` + `cmd_watch` make the daemon the group leader |
| P3.5 | Ladder shared by bare `worc stop`/`restart` and console `down`/`quit` | ✅ conformant | `_gated_stop` used by `cmd_stop` and `cmd_restart`; console `down`→`stop` |
| DOC | `/sync-docs`: functional map + operations + configuration/README | ⚠️ partial | **L5** — `operations.md` + `README.md` updated; `docs/functional/` has no `top`/`shell`/stop-ladder block |

## Findings (detail)

### 🔴 C1 — `--force-full` hard stop: inverted process-group topology (correctness + safety)

**What the ADR asked for** ([§ Stop safety](cli-upgrade.md#stop-safety-idlebusy-gate--two-force-levels), point 4): the hard rung must "terminate the active agent process group immediately … so the daemon (or `stop`) can signal the whole group **without orphaning** it." Recovery is the next `resume()`.

**What shipped:**

- The **agent** launches with `start_new_session=True` ([providers/process.py:103](../../../../src/wastech_orchestrator/providers/process.py#L103)) — i.e. `setsid()`, making the agent a **session and process-group leader of a brand-new group** (`pgid == agent_pid`), detached from the daemon's group.
- The **daemon** establishes **no** process group: `cmd_watch` only writes a PID file (no `setsid`/`setpgid` — [cli.py:2080-2084](../../../../src/wastech_orchestrator/cli.py#L2080-L2084)), and `spawn_detached` launches the console's `watch` child **without** `start_new_session` ([providers/process.py:144-152](../../../../src/wastech_orchestrator/providers/process.py#L144-L152)).
- The hard rung resolves and kills the **daemon's** group: `killpg(getpgid(daemon_pid), SIGKILL)` ([process_control.py:371](../../../../src/wastech_orchestrator/process_control.py#L371)).

**Why it is broken.** `killpg` targets the daemon's group. The agent set its own session, so it is **not in the daemon's group** — it survives as an orphan. This is exactly the failure the rung was built to prevent. There is no fallback: the orchestrator records the agent subprocess PID nowhere, so `stop` has no other handle on it.

**Why it is dangerous.** For the primary shell use case, the daemon is spawned by the console via `spawn_detached` with **no** new session, so it **inherits the console's process group**. Then `getpgid(daemon_pid)` returns the **console's** group and `killpg(…, SIGKILL)` kills the **operator's console/shell itself** (and every sibling in that group) — while _still_ orphaning the agent. A directly-launched foreground `worc watch` is saved from the collateral only by shell job control (it gets its own foreground group), but the agent is orphaned there too. Checks subprocesses (also `run_process`, also `setsid`) are likewise orphaned.

**Why tests don't catch it.** `test_stop_full_kills_the_process_group_on_posix` injects `getpgid_fn=lambda _pid: 9000` and asserts `killpg` was called with `(9000, KILL)` ([tests/test_process_control.py:379-396](../../../../tests/test_process_control.py#L379-L396)). It validates the _plumbing_ but never the _topology_ — that a real agent is a member of the killed group. Green suite, broken feature.

**Fix (POSIX).** Invert the topology to what the ADR actually needs:

1. The **daemon** must lead its own group. For the console-spawned daemon, pass `start_new_session=True` in `spawn_detached` (POSIX). For a directly-launched foreground `worc watch`, shell job control already gives it its own group; for non-interactive launches (scripts/systemd) either document `setsid worc watch` or have `cmd_watch` best-effort `os.setpgrp()` when it is not already a group leader — **without** detaching the controlling terminal, so foreground `Ctrl-C` still delivers `SIGINT`.
2. The **agent** must **not** create a new session: remove `start_new_session=True` from `run_process` so the agent (and any checks child it spawns) **inherits the daemon's group**.
3. Then `killpg(getpgid(daemon_pid))` = the daemon's own group = daemon + active agent + checks child, killed together, nothing orphaned, no collateral outside the daemon's group.

**Behavioral note to accept and document:** with the agent back in the daemon's foreground group, `Ctrl-C` on a foreground `worc watch` now delivers `SIGINT` to the agent too (it dies mid-stage instead of being orphaned) — a strict improvement, but a change from today's silent-orphan behavior; call it out in operations docs.

**New tests required (the ones that would have caught this):** a POSIX-gated integration test that launches a real daemon-like parent which spawns a real child sharing its group, then asserts `stop_process(level="full")` reaps **both** via a real `os.killpg`; and a test asserting `spawn_detached` sets `start_new_session=True` (POSIX) while `run_process` does **not** (kwargs interception, as the existing agent-launch tests do).

### 🟠 H1 — two stdin readers: console `down` calls `input()` inside the REPL

`down` with no flag while a task is active and stdin is a TTY forwards to `cli.main(["stop"])` → `cmd_stop` → `_gated_stop` → `_resolve_stop_level(interactive=sys.stdin.isatty())` → `_confirm_yes()` → `input()` ([cli.py:1274-1283](../../../../src/wastech_orchestrator/cli.py#L1274-L1283)). Inside `worc shell` the `prompt_toolkit` `PromptSession` under `patch_stdout()` owns stdin; a raw `input()` on the same terminal is precisely the "second stdin reader" the ADR forbids ([§ Constraints](cli-upgrade.md#constraints-that-bound-any-solution) #4). It is fragile and produces a confusing nested prompt.

**Fix:** the console must never fall through to `input()`. Either (a) confirm via `prompt_toolkit`'s own `prompt`/`confirm` when the `down` decision needs a `YES`, or (b) make the in-shell `down` require an explicit `--force`/`--force-full` and print a one-line "a task is active — `down --force` (soft) or `down --force-full` (hard)" instead of prompting. Option (b) is simpler and keeps the single-reader invariant airtight. Thread an `interactive=False`-equivalent (or a `confirm_fn` seam) so the forwarded stop never reads stdin from inside the REPL.

### 🟡 M1 — `enqueue`/`cancel` mangle Windows absolute paths (`shlex.split` POSIX mode)

`dispatch` tokenizes with `shlex.split(line)` in default POSIX mode ([cli_shell.py:145](../../../../src/wastech_orchestrator/cli_shell.py#L145)), which treats `\` as an escape, so `enqueue C:\Users\x y\task.md` → `['enqueue', 'C:Usersx', 'ytask.md']` and the file is never copied. Two tests already fail on Windows. This is **already recorded** as a follow-up (2026-07-01) but left unfixed, and it sits squarely inside this feature's cross-platform mandate ([CLAUDE.md hard invariants](../../../../CLAUDE.md)).

**Fix:** tokenize OS-aware — `shlex.split(line, posix=(os.name != "nt"))` — or split only the leading verb and treat the remainder as a raw path; add a Windows-path `enqueue`/`cancel` regression.

### 🟡 M2 — attach can't tail without `--log-file`; the daemon has no default log path

On attach, if `--log-file` was not passed, `log_path` is `None` and `top`/`ps` render "(no output)" ([cli_shell.py:122-125](../../../../src/wastech_orchestrator/cli_shell.py#L122-L125)); a detached `worc watch` still writes to a rotating file only when explicitly given `--log-file` (no default — [cli.py:193-197](../../../../src/wastech_orchestrator/cli.py#L193-L197)). So the common "start `worc watch` in one terminal, attach with `worc shell`/`worc top` in another" flow shows an empty log by default.

**Fix (ergonomic):** give the daemon a **default** operator-log path under the runtime root (e.g. `.worc/logs/daemon.log`) when running in daemon mode, and have `top`/attach default their tail target to that same path. This makes attach useful without the operator having to coordinate a path by hand. (The spawned-child path already defaults to `.worc/logs/daemon.log`; this just makes the direct-launch and attach paths symmetric.)

### 🟡 M3 — quit while busy+spawned just leaves the daemon running (no offer)

The ADR wants quit-while-busy on a spawned child to **offer** detach (default) / soft / `--force-full` ([§ CLI surface](cli-upgrade.md#cli-surface-two-deliverables-shippable-independently), `quit` row). `_shutdown_daemon` instead unconditionally leaves a busy spawned daemon running with a note ([cli_shell.py:370-376](../../../../src/wastech_orchestrator/cli_shell.py#L370-L376)). Defaulting to detach is defensible, but there is no path to soft/hard-stop on the way out.

**Fix (small):** on quit with a busy spawned child, print the choices and accept a one-key answer via `prompt_toolkit` (never raw `input()` — see H1), defaulting to detach. Low priority; the current behavior is safe, just less capable than specified.

### ⚪ L1 — log tailing re-reads the whole file each poll

`tail_lines` and `_LogTailer.poll` both `read_text()` the entire log every refresh ([cli.py:2356-2369](../../../../src/wastech_orchestrator/cli.py#L2356-L2369), [cli_shell.py:303-314](../../../../src/wastech_orchestrator/cli_shell.py#L303-L314)) — O(filesize) against a 10 MB rotating file every 1–2 s. Deliberately rotation-immune, which is the reason to keep it, but consider a rotation-aware byte-offset read if operator logs grow. Weigh simplicity vs. cost; low priority at current scale.

### ⚪ L2 — two divergent tailers

`top` uses `tail_lines` (full re-read, last-N); the shell's live stream uses `_LogTailer` (line-count cursor). Two mechanisms with overlapping intent. Consolidate onto one rotation-aware tailer to cut the duplication and the "burst larger than the tail is dropped" asymmetry.

### ⚪ L3 — console `down`/`restart` re-enter the full CLI each time

Forwarding `down`/`restart` through `cli.main([...])` ([cli_shell.py:164-167](../../../../src/wastech_orchestrator/cli_shell.py#L164-L167)) re-loads config and re-runs `configure_logging`, which can disturb the console's deliberately-quiet logging and re-parses config on every invocation. Consider calling a `_gated_stop`-style helper directly with the already-loaded `ctx.config`. Minor.

### ⚪ L4 — `logs` in the shell tails only per-attempt `stdout.log`, never the daemon log

The ADR's `logs` row lists "tail artifact `stdout.log` **/ daemon log**" ([§ CLI surface](cli-upgrade.md#cli-surface-two-deliverables-shippable-independently)). `_do_logs` only tails the per-attempt `stdout.log` ([cli_shell.py:255-272](../../../../src/wastech_orchestrator/cli_shell.py#L255-L272)). The daemon log is already streamed above the prompt, so this is mostly redundant — but a `logs --daemon` affordance (or documenting the split) would close the spec. Trivial.

### ⚪ L5 — functional-map docs not updated

`worc top`/`worc shell`/the stop-ladder appear in [operations.md](../../../operations.md) and `README.md`, but `docs/functional/` carries no CLI/daemon block for them and still describes `stop`/`restart` without the ladder or the agent-group isolation ([§ Проверки и документация](cli-upgrade.md#проверки-и-документация) asked for this). This partly folds into the standing "Full re-sync of functional-map" follow-up (2026-06-19) but the console block is net-new. Add it during the C1 doc pass.

## Remediation plan (phased)

Checks + docs run **once at the end** (`/run-checks`, then `/sync-docs` + `npx prettier@3 --write "**/*.md"`), per the parent ADR's convention.

### Phase R1 — fix the hard stop (C1) on **both platforms** — **the "fully implemented" work**

> **✅ Implemented 2026-07-02.** Suite green (ruff + mypy clean, 1836 passed / 1 skipped). `run_process` no longer `setsid`s; `spawn_detached` sets `start_new_session` on POSIX; `cmd_watch` calls `process_control.ensure_own_process_group()`; Windows gets a real `taskkill /F /T` via the injected `hard_kill_fn` seam (`agent_process.hard_kill_tree`) with a new `StopOutcome.tree_killed`. Docs synced (operations/README). **Windows real-smoke validation pending (owner)** — see the [follow_ups row](../../follow_ups.md) (2026-07-02).

This phase now closes the whole hard-stop capability, POSIX and Windows, folding in the former "Windows hard stop" follow-up (see [§ Windows hard stop — now in scope](#windows-hard-stop--now-in-scope)).

**POSIX (fix the inverted topology):**

1. [providers/process.py](../../../../src/wastech_orchestrator/providers/process.py) — **remove** `start_new_session=True` from `run_process` (agent inherits the daemon's group); **add** `start_new_session=True` to `spawn_detached` on POSIX (the console-spawned daemon leads its own group).
2. [cli.py](../../../../src/wastech_orchestrator/cli.py) `cmd_watch` — for the looping daemon on POSIX, best-effort become a process-group leader when not already one, **without** detaching the controlling terminal (preserve foreground `Ctrl-C`). Guard the `setpgrp`/`setpgid` call (it raises if already a leader).

**Windows (make the hard stop real, not a soft-degrade):**

3. [providers/process.py](../../../../src/wastech_orchestrator/providers/process.py) — add a `hard_kill_tree(pid)` helper that runs `["taskkill", "/F", "/T", "/PID", str(pid)]` (argv, `shell=False`), tolerating a "no such process" exit (dead/recycled PID). `taskkill /T` kills by the **process tree** (parent → children), so it reaches the agent (a child of the daemon) **without** needing a `CREATE_NEW_PROCESS_GROUP` spawn flag — and it does **not** kill the daemon's parent (the console), so there is no collateral. (The `CREATE_NEW_PROCESS_GROUP` flag stays out of R1: it is only needed for a graceful `CTRL_BREAK_EVENT`, which remains the separate deferred Windows-graceful follow-up.)
4. [process_control.py](../../../../src/wastech_orchestrator/process_control.py) — the Windows `level="full"` branch stops degrading to soft: it hard-kills the daemon tree via an **injected `hard_kill_fn` seam** (the CLI wires the default to `hard_kill_tree`). `process_control.py` **keeps its no-subprocess invariant** — it never calls `taskkill` itself; it invokes the seam. Add a `tree_killed` outcome flag; `degraded_to_soft` is only set when no `hard_kill_fn` was supplied (defensive fallback).
5. [cli.py](../../../../src/wastech_orchestrator/cli.py) — wire the default `hard_kill_fn` into `_gated_stop`; update `cmd_stop`/`cmd_restart` messages (a Windows tree-kill reports "hard-stopped (killed its process tree)", no more "unavailable on Windows").

**Tests (no Windows box needed):**

6. POSIX-gated **real-topology** integration test (parent spawns a group-sharing child; `stop_process(level="full")` reaps both via real `os.killpg`); assert `spawn_detached` sets `start_new_session=True` on POSIX and `run_process` does not (kwargs interception).
7. Windows path via the seam pattern the module already uses: force `can_signal=False`, inject a fake `hard_kill_fn`, assert it is called with the daemon PID and that the outcome reports a tree-kill (not `degraded_to_soft`); assert `hard_kill_tree` builds the `taskkill /F /T /PID` argv with `shell=False` and swallows a "no such process" exit (subprocess interception).

**Docs:** note the foreground-`Ctrl-C`-now-kills-the-agent behavior change in [operations.md](../../../operations.md); update the `--force-full` docs to "works on POSIX and Windows".

**Validation (needs a real environment):** a **real Windows smoke pass** (Windows 10/11 + the pinned Python) — start a daemon, run a task, `stop --force-full`, confirm the daemon **and** its agent subprocess die and `resume()` recovers on next start. Same pattern as the prior real-Windows cross-platform passes. This is the one step that cannot be faked on POSIX; gate "rely on it in prod" behind it.

**AC-R1a (POSIX):** `stop --force-full` (and console `down --force-full`) while a task is active kills the daemon **and** the active agent, orphans nothing, and never signals a process outside the daemon's own group — verified against a real spawned child, not injected seams.

**AC-R1b (Windows):** `stop --force-full` hard-kills the daemon tree via the injected `taskkill` seam (no soft-degrade); unit-verified on POSIX via the seam, and confirmed once on a real Windows box.

### Phase R2 — single stdin reader (H1) + Windows paths (M1)

1. [cli_shell.py](../../../../src/wastech_orchestrator/cli_shell.py) — the in-shell `down`/`restart` must never trigger `input()`. Either require an explicit `--force`/`--force-full` inside the shell (print the two options when busy and no flag) or confirm via `prompt_toolkit`. Thread a `confirm_fn`/`interactive=False` seam into the forwarded stop.
2. [cli_shell.py](../../../../src/wastech_orchestrator/cli_shell.py) `dispatch` — `shlex.split(line, posix=(os.name != "nt"))` (or verb-only split); Windows-path `enqueue`/`cancel` regression. Resolves the standing 2026-07-01 follow-up.

**AC-R2:** inside `worc shell`, no code path calls `input()`; `enqueue`/`cancel` accept a Windows absolute path; the two currently-failing Windows shell tests pass.

### Phase R3 — ergonomics (M2, M3, L-series, docs)

1. **M2** — default daemon `--log-file` to `.worc/logs/daemon.log` in daemon mode; default `top`/attach tail to that path.
2. **M3** — quit-while-busy-spawned offers detach (default)/soft/`--force-full` via `prompt_toolkit`.
3. **L2/L3** — consolidate onto one rotation-aware tailer; have console `down`/`restart` reuse the loaded config instead of re-entering `cli.main`.
4. **L1/L4** — decide on incremental tailing (or keep + document the trade-off); document or add `logs --daemon`.
5. **L5** — add a `docs/functional/` CLI/daemon block for `top`/`shell` and re-derive the `stop`/`restart` description under the ladder + agent-group isolation.

**AC-R3:** attaching `worc top`/`worc shell` to a plainly-started `worc watch` shows live log output with no manual path coordination; the functional map documents the console and the stop ladder.

## Windows hard stop — now in scope

Originally a deferred follow-up (2026-06-28), the Windows hard stop is **pulled into R1**. Rationale: once R1 makes `--force-full` actually work on POSIX, "works on Linux/macOS, silently soft-degrades on Windows" becomes a sharp violation of the mandatory cross-platform invariant ([CLAUDE.md](../../../../CLAUDE.md)) — before R1 it did not work anywhere (parity), after R1 it would be lopsided. The spawn-half (`CREATE_NEW_PROCESS_GROUP`) is one guarded kwarg in the same `spawn_detached` edit R1 already makes; the stop-half (`taskkill /F /T`) is routed through an **injected seam** so `process_control.py` keeps its no-subprocess invariant.

**Does this need a Windows machine?** For **implementation and unit tests — no**: the module's existing seam pattern (force `can_signal=False`, inject fakes for `hard_kill_fn` and the spawn kwargs) proves the logic on POSIX, exactly as the current Windows stop path is tested. For **final validation — yes**: `taskkill /F /T` tree-kill and `CREATE_NEW_PROCESS_GROUP` semantics can only be confirmed for real on Windows, so a one-time real Windows smoke pass gates "rely on it in prod" (matches the prior real-Windows cross-platform passes; the standing "Windows CI runner matrix" follow-up would automate it).

When R1 lands, close the 2026-06-28 "Windows hard stop (`taskkill`)" row in [follow_ups.md](../../follow_ups.md).

## Out of scope (unchanged deferrals — already tracked)

These stay in [follow_ups.md](../../follow_ups.md); this remediation does not pull them in:

- **Clean cooperative per-task cancel** (unwind the flow, mark `cancelled`, no group-kill). (2026-06-28 row.)
- **HITL / gate _answer_ in the console** (Telegram stays the answer channel; the console only surfaces a pending durable gate). (2026-06-28 row.)
- **Textual TUI** and **daemon control socket / IPC**. (2026-06-28 rows.)

## Note on the ADR text

[cli-upgrade.md § Stop safety](cli-upgrade.md#stop-safety-idlebusy-gate--two-force-levels) point 4 is the source of the C1 confusion: it says "agents must launch in **their own process group** (`start_new_session=True`) … so the daemon can signal the whole group without orphaning it." As written that is self-contradictory — an agent in _its own_ new session is by definition outside the daemon's group, so a daemon-group-kill cannot reach it. The implementation followed the letter (`start_new_session` on the agent) and inherited the contradiction. Correct the ADR alongside R1: the **daemon** leads the group; **agents inherit it** and must **not** `setsid`.
