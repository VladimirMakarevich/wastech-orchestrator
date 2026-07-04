# worc shell as a reliable operator control surface

Status: **proposed** (2026-07-04) Date: 2026-07-04 Owner: Vladimir Makarevich

Make `worc shell` a dependable panel for driving and observing the orchestrator: it attaches to (or, on demand, reliably starts) the `watch` daemon, never claims a task until the operator asks, verifies the daemon actually came up, and surfaces the real error when it does not. Discovered during the P4 `wastech-mdlint` test campaign, where launching `worc shell` reported nothing and the eight pending tasks were never picked up — the auto-spawned daemon had died silently.

## The problem

`worc shell` auto-spawns the `watch` daemon at startup (`cli_shell.run` → `spawn_or_attach_watch`), and that spawn is both fragile and mute:

- **Silent failure.** `providers/process.py:spawn_detached` routes the child's stdout/stderr to `DEVNULL`, and `spawn_or_attach_watch` prints `started watch daemon (pid X)` without checking the child is alive. Any startup crash is invisible: the console claims success, the queue is served by nobody, and the operator has no signal. On the P4 run this presented exactly as "I typed `enqueue` / launched the shell and nothing happens."
- **No liveness verification.** Neither shell startup nor the `up` command confirms the daemon's PID file appeared or that its first poll ran. A dead child is indistinguishable from a healthy one, so `ps`/attach can report an attach to a daemon that is not actually serving.
- **Spawn robustness across install layouts.** The daemon is launched as `[sys.executable, "-m", "wastech_orchestrator.cli", "watch", ...]`. Direct `worc watch` (the console-script, via its shebang interpreter) works reliably; the `sys.executable -m` form is the fragile path across pipx-venv / macOS framework-python / `--user` layouts, where the interpreter that ends up running the child may not import the package.
- **No deliberate control of when work starts.** Because the daemon auto-starts at shell entry, the first eligible task is claimed immediately. The operator has no "open the panel, look around, then decide to start" mode.
- **Known usability gaps (§6 R2/R3, already tracked).** H1: the console `down` on a busy daemon with no flag falls into a blocking `input()` inside the REPL, violating the single-stdin-reader rule. M1: `dispatch` uses `shlex.split` in POSIX mode, breaking Windows absolute paths in `enqueue`/`cancel`. M2: attaching without `--log-file` shows "(no output)" because the daemon has no default log path to tail. M3: `quit` while the daemon is busy just leaves it running with no detach/stop choice offered.

The net effect: the interactive console — the surface meant to make the orchestrator easy to drive — is the least trustworthy way to run it today.

## Constraints

- **The shell is a client over the daemon, not a second engine host.** `cli_shell.dispatch` must stay a thin forwarder onto existing `cmd_*` verbs and add no orchestration/state-machine logic. The single-active-task invariant stays per-instance; "commit/push/PR only the orchestrator" is untouched.
- **Launch without shell interpolation.** Any process the shell starts stays an argv list with `shell=False` (the current `spawn_detached` contract); the M1 fix for operator-input parsing must not introduce shell-string execution.
- **Cross-platform (Windows / Linux / macOS) is mandatory.** The spawn/liveness/detach design must hold on all three: POSIX `start_new_session` process-group semantics vs Windows `taskkill /T` tree-kill, the M1 path parsing, and the stop ladder already differ per platform and must be branched and tested, not assumed.
- **No secrets in logs or artifacts.** The captured daemon startup stream (new — see Decision) passes through the same redaction as other logs; it must never persist raw env or credentials even on a crash dump.

## Alternatives considered

| Option | Why not chosen |
| --- | --- |
| Do nothing (workaround: run `worc watch` directly) | Works and is the current unblock, but leaves the advertised console broken and the silent-failure trap in place for every operator. |
| Minimal: keep auto-spawn, only add a liveness check + surface errors | Fixes the silence but not the fragility (still `sys.executable -m …`) and not the "decide when to start" need; a partial fix that still auto-claims on entry. |
| Shell as a pure client — never spawns, operator always runs `worc watch` in another terminal first | Most robust (nothing to spawn, nothing to silently fail), but pushes daemon lifecycle entirely onto the operator and loses the one-command convenience the console exists to provide. |
| Reliable on-demand spawn from the shell (**chosen**) | Keeps the single-surface convenience while removing the silent-failure and auto-claim problems; costs a bit more shell code (liveness poll, detach-on-quit, verb split). |

## Decision

`worc shell` becomes a reliable, on-demand control surface. Shell entry is **passive**: it attaches to a live daemon if one exists, otherwise it opens idle with the queue **not** being served and a banner saying so — it never auto-spawns and never claims a task on entry. The operator starts work explicitly: `up`/`watch` begins continuous serving of the queue, `run <id>` runs a single named task once. When the shell does start the daemon it launches it through the resolved `worc` console-script entrypoint (not `sys.executable -m`), redirects the child's early stdout/stderr to a startup log instead of `DEVNULL`, and then **verifies liveness** (PID file present / daemon's startup log line seen) before reporting success; if the daemon did not come up it prints the captured real error, not `started (pid X)`. `quit` **detaches** by default — the daemon and any in-flight task keep running so long P4-style tasks survive closing the panel, and reopening `worc shell` reattaches; stopping is only the explicit `down` (soft) / `down --force-full` (hard). The verb vocabulary is disambiguated — `enqueue <file>` adds to the queue, `up`/`watch` serve it, `run <id>` is a one-shot — with a slot-guard so `run` and a serving daemon cannot both claim the single active slot. The same ADR closes the §6 R2/R3 gaps H1 (no blocking `input()` inside the REPL), M1 (cross-platform path parsing in `enqueue`/`cancel`), M2 (a default daemon log path so attach can always tail), and M3 (quit offers detach/stop rather than silently leaving a busy daemon).

The cost of the rejected options: "do nothing" and "minimal" leave a control surface that lies on failure; "pure client" trades away the single-command ergonomics that justify having a console at all.

## Open questions

- **`quit` = detach by default — confirm.** Proposed default is detach (daemon + task survive), stop only via explicit `down`. Is detach-on-quit the right default for every case, or should `quit` prompt when a task is mid-stage?
- **Do we ship `run <id>` (one-shot) in this ADR, or only `up`/`watch`?** One-shot is closer to the operator's mental model ("run this task now") but adds a second claim path to slot-guard.
- **`run` verb migration.** Today `run` is an alias of `enqueue` (copies a file into `pending`). If `run <id>` becomes one-shot execution, the alias changes meaning — keep `run` as the enqueue alias and pick a different one-shot verb, or repoint `run` and accept the behavior change (greenfield, so a clean break is cheap)?
- **Liveness signal of record.** Key the check off the PID file, the daemon's first structured log line, or both — and the timeout before declaring the spawn dead.

## Implementation notes

Primary seams, all in `src/wastech_orchestrator/`:

- `cli_shell.py` — `run` (passive entry: attach-or-idle, drop the startup auto-spawn), `spawn_or_attach_watch` (console-script entrypoint instead of `sys.executable -m`; liveness verification + surfaced error), `_do_up` (the on-demand start with the same verification), `dispatch`/`_do_enqueue` (verb split `enqueue`/`up`/`run`, slot-guard), `_shutdown_daemon` (detach-on-`quit`, explicit `down` to stop — M3), and the M1 cross-platform argument parsing.
- `providers/process.py:spawn_detached` — stop discarding the child's early stdout/stderr; capture to a startup log path so a crash is recoverable (keep the argv-list / `shell=False` / POSIX `start_new_session` invariants).
- `process_control.py` — PID-file probe reused for the liveness check; the default daemon log path (M2) so attach can always tail.
- Cross-platform: preserve the POSIX process-group vs Windows `taskkill /T` split already in `spawn_detached`/the stop ladder; test spawn + liveness + detach on both.

This is a shell/console-reliability change only — no flow-engine, state-machine, or provider-adapter changes, no config schema bump expected.
