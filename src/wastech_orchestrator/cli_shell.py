"""Interactive operator console (``worc shell``) — a *client* over the watch daemon.

The console never hosts the engine. Entry is **passive**: it attaches to a live ``watch`` daemon if
one exists, else opens idle with the queue *not* being served — it never auto-spawns and never
claims a task on entry. The operator starts serving explicitly with ``up``/``watch``, which spawns a
daemon (the only engine host) and *verifies it came up* before reporting success. Beyond that the
console reads ``state.db`` read-only, tails the daemon ``--log-file``, enqueues by dropping task
files into ``pending_dir(config)``, and dispatches every other verb onto the existing ``cmd_*``
functions via :func:`wastech_orchestrator.cli.main`. It adds no orchestration logic and introduces
no new git/network capability. ``quit`` detaches: the daemon (and any in-flight task) keeps running.

``prompt_toolkit`` is the optional ``[shell]`` extra: it is imported **only** inside
:func:`_run_interactive`, so importing this module (and the whole headless dispatch surface) works
without it. The interactive tail diverges from ``worc top``'s reader-thread model — here it is an
``asyncio`` task that prints through ``patch_stdout()`` so daemon log lines land above the prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import shlex
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from wastech_orchestrator import cli, process_control
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers import process as agent_process
from wastech_orchestrator.state_store import StateStore

_TAIL_INTERVAL_SECONDS = 1.0
_LOGS_TAIL_LINES = 40
# How long to wait for a just-spawned daemon to prove it is alive (its PID file to appear) before
# declaring the spawn dead and surfacing the startup log. A daemon writes its PID file right after
# preflight + entering the loop, so a few seconds is ample; a crash (bad argv, import, preflight
# abort) either exits the child early (fast-fail) or never writes the file (times out).
_LIVENESS_TIMEOUT_SECONDS = 10.0
_LIVENESS_POLL_SECONDS = 0.1
_STARTUP_LOG_TAIL_LINES = 20

_BANNER = "worc shell — client over the watch daemon. Type 'help' for commands, 'quit' to exit."

_HELP = """\
commands:
  enqueue <file>              copy a task file into the pending queue (queued; not run until 'up')
  up | watch                  start serving the queue: spawn a watch daemon (verifies it came up)
  ps | jobs                   snapshot: active task + node, queue, recent, log tail
  status [<id>]               persisted status of the active/latest task (or <id>)
  tasks                       list every known task (read-only)
  logs [<id>]                 tail the active/<id> agent stdout.log
  prs                         list orchestrator PRs awaiting merge
  merge-task <id>             merge a reviewed PR (refuses while the daemon is up)
  finalize ... | rerun ...    one-shot lifecycle verbs (refuse while the daemon is up)
  cancel <id>                 de-queue a pending task file (-> .worc/tasks/rejected)
  down [--force|--force-full] stop the daemon (idle: no prompt; busy: needs --force/--force-full)
  restart [...]               restart the daemon
  quit | exit                 leave; the daemon (and any in-flight task) keeps running — reopen to
                              reattach, or stop it with 'down' / 'worc stop'
"""

# Verbs forwarded verbatim to the existing CLI dispatch; their own slot/daemon guards apply.
_FORWARD_VERBS = frozenset({"status", "tasks", "prs", "merge-task", "finalize", "list"})


@dataclass
class DaemonHandle:
    """The watch daemon the console supervises: ``attached`` (leave running on exit) or spawned."""

    attached: bool
    process: object | None  # the Popen for a spawned child, else None
    log_path: Path | None
    pid: int | None


@dataclass
class ShellResult:
    """The outcome of one dispatched command line."""

    quit: bool = False
    exit_code: int = 0


@dataclass
class ShellContext:
    """Everything a dispatched command needs; the seams (``spawn_fn``/``run_cli``/``out``) make the
    dispatcher headless-testable without a TTY, a real daemon, or prompt_toolkit.

    ``log_path`` is the daemon log resolved once at entry (``--log-file`` or the M2 default), so the
    live tail follows the queue whether a daemon is attached at entry or started later with ``up`` —
    the file is written to that path either way. ``daemon`` is ``None`` when the shell is idle (no
    daemon serving the queue): entry is passive and never auto-spawns.
    """

    config: OrchestratorConfig
    selector: str
    log_path: Path
    config_path: str | None = None
    daemon: DaemonHandle | None = None
    spawn_fn: Callable[..., object] = agent_process.spawn_detached
    run_cli: Callable[[list[str]], int] = cli.main
    out: TextIO = field(default_factory=lambda: sys.stdout)


def _prompt_toolkit_available() -> bool:
    return importlib.util.find_spec("prompt_toolkit") is not None


def _argv(ctx: ShellContext, command: str, rest: Sequence[str]) -> list[str]:
    """An argv for :func:`cli.main` that targets the console's resolved config (``--config`` is a
    parent-parser flag, so it must precede the subcommand)."""
    prefix = ["--config", ctx.config_path] if ctx.config_path else []
    return [*prefix, command, *rest]


def daemon_log_path(config: OrchestratorConfig, log_file: str | None) -> Path:
    """The daemon log the console spawns-with and tails: ``--log-file`` or the M2 default.

    Defaulting to ``.worc/logs/daemon.log`` (not ``None``) is M2: the tail always has a path, so
    attach/``up`` can follow the queue without the operator hand-coordinating a log path.
    """
    return Path(log_file) if log_file else cli.worc_home_for(config) / "logs" / "daemon.log"


def _startup_log_path(config: OrchestratorConfig) -> Path:
    """Where a spawned daemon's raw stdout/stderr is captured for crash recovery (see
    :func:`wastech_orchestrator.providers.process.spawn_detached`'s ``capture_path``)."""
    return cli.worc_home_for(config) / "logs" / "daemon-startup.log"


def _watch_launcher() -> list[str]:
    """The argv prefix that reliably re-invokes this orchestrator's CLI to spawn the daemon.

    Prefer the resolved ``worc`` / ``wastech-orchestrator`` console-script (its shebang pins the
    interpreter that installed the package, so the child always imports it) over
    ``sys.executable -m wastech_orchestrator.cli`` — the fragile path across pipx-venv /
    framework-python / ``--user`` layouts. Falls back to ``-m`` only when no console-script is on
    PATH (e.g. an editable checkout run without an entry point).
    """
    for script in ("worc", "wastech-orchestrator"):
        resolved = shutil.which(script)
        if resolved:
            return [resolved]
    return [sys.executable, "-m", "wastech_orchestrator.cli"]


def attach_watch(
    config: OrchestratorConfig,
    *,
    log_file: str | None,
    out: TextIO | None = None,
) -> DaemonHandle | None:
    """Attach to a live ``watch`` daemon if one is running, else return ``None`` (idle) — no spawn.

    Passive entry (the Decision): a live PID file → attach and leave it running on exit. We tail the
    M2 default log path (or ``--log-file``) — the convention a console-spawned daemon writes to — so
    an attached daemon started the same way is tailable without extra coordination.
    """
    out = out if out is not None else sys.stdout
    pid_path = process_control.pid_file_path(cli.worc_home_for(config))
    pid = process_control.running_daemon_pid(pid_path)
    if pid is None:
        return None
    log_path = daemon_log_path(config, log_file)
    print(f"shell: attached to running daemon (pid {pid}) — tailing {log_path}", file=out)
    return DaemonHandle(attached=True, process=None, log_path=log_path, pid=pid)


def _wait_until_alive(
    pid_path: Path,
    proc: object,
    *,
    timeout: float,
    poll: float,
    ready_probe: Callable[[], int | None],
    sleep_fn: Callable[[float], None],
    now_fn: Callable[[], float],
) -> int | None:
    """Poll for the spawned daemon's PID file (its liveness signal of record) until ``timeout``.

    Returns the live PID once the daemon records it, or ``None`` if the child exits first (fast-fail
    on ``proc.poll()``) or the timeout elapses. The PID file is the signal because the daemon writes
    it only after passing preflight and entering the loop — its presence means "actually serving",
    which a bare "process launched" cannot promise (the exact P4 false-positive this closes).
    """
    poll_proc: Callable[[], int | None] = getattr(proc, "poll", lambda: None)
    deadline = now_fn() + timeout
    while True:
        pid = ready_probe()
        if pid is not None:
            return pid
        if poll_proc() is not None:
            return None  # the child already exited — a startup crash; do not keep waiting
        if now_fn() >= deadline:
            return None
        sleep_fn(poll)


def start_watch(
    config: OrchestratorConfig,
    *,
    selector: str,
    log_file: str | None,
    spawn_fn: Callable[..., object] = agent_process.spawn_detached,
    config_path: str | None = None,
    out: TextIO | None = None,
    ready_probe: Callable[[], int | None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
    timeout: float = _LIVENESS_TIMEOUT_SECONDS,
    poll: float = _LIVENESS_POLL_SECONDS,
) -> DaemonHandle | None:
    """Reliably spawn a ``watch`` daemon and verify it came up; return its handle or ``None``.

    Launches through the resolved ``worc`` console-script (:func:`_watch_launcher`) with the parent
    flags ``--config``/``--log-file`` **before** the ``watch`` subcommand (they are parent-parser
    options — appending them after ``watch`` is why the old auto-spawn died on an argparse error to
    a ``DEVNULL``'d stderr). The child's stdout/stderr are captured to a startup log, then we verify
    liveness (:func:`_wait_until_alive`) before reporting success; if the daemon did not come up we
    print the captured real error and return ``None`` (the shell stays idle) rather than a false
    ``started (pid X)``.
    """
    out = out if out is not None else sys.stdout
    log_path = daemon_log_path(config, log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    startup_log = _startup_log_path(config)
    argv = [*_watch_launcher()]
    if config_path:
        argv += ["--config", config_path]  # parent flag → before the subcommand
    argv += ["--log-file", str(log_path)]  # parent flag → before the subcommand (the argparse fix)
    argv += ["watch", "--queue", selector]
    proc = spawn_fn(argv, capture_path=str(startup_log))
    pid_path = process_control.pid_file_path(cli.worc_home_for(config))
    probe = ready_probe or (lambda: process_control.running_daemon_pid(pid_path))
    pid = _wait_until_alive(
        pid_path,
        proc,
        timeout=timeout,
        poll=poll,
        ready_probe=probe,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
    )
    if pid is None:
        detail = _startup_error(startup_log)
        print(f"shell: watch daemon failed to start{detail}", file=out)
        return None
    print(f"shell: serving the queue — watch daemon (pid {pid}) -> {log_path}", file=out)
    return DaemonHandle(attached=False, process=proc, log_path=log_path, pid=pid)


def _startup_error(startup_log: Path) -> str:
    """The tail of the captured startup log as a printable suffix (empty when nothing was captured).

    This is the real error — the argparse/import/preflight message the daemon wrote to stderr before
    dying — surfaced instead of a bare "failed". Never raises: an unreadable/absent log is "".
    """
    try:
        text = startup_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = [line for line in text.splitlines() if line.strip()][-_STARTUP_LOG_TAIL_LINES:]
    if not tail:
        return f" (no output captured; see {startup_log})"
    body = "\n".join(f"  {line}" for line in tail)
    return f"; its startup output was:\n{body}"


def _split_verb(line: str) -> tuple[str, str]:
    """``(verb, raw remainder)`` — the verb is the first whitespace-delimited token; the remainder
    is the rest of the line verbatim (stripped).

    Keeping the remainder raw (not tokenized) is the M1 fix for the path-taking verbs: a Windows
    absolute path (``enqueue C:\\Users\\x\\task.md``) survives intact, where POSIX ``shlex`` would
    strip its backslashes and split on any embedded space. Multi-argument verbs re-tokenize the
    remainder OS-aware via :func:`_tokens`.
    """
    stripped = line.strip()
    parts = stripped.split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


def _tokens(remainder: str) -> list[str]:
    """Tokenize a command's argument string OS-aware (M1): POSIX-mode ``shlex`` on POSIX (escapes,
    quotes) and non-POSIX on Windows (keeps ``\\`` literal). For id/flag-taking verbs only —
    path-taking verbs use the raw remainder (:func:`_split_verb`)."""
    return shlex.split(remainder, posix=os.name != "nt")


def _unquote(arg: str) -> str:
    """Strip one layer of matching surrounding quotes so a quoted path with spaces works too."""
    if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in ("'", '"'):
        return arg[1:-1]
    return arg


def dispatch(line: str, ctx: ShellContext) -> ShellResult:
    """Map one console line onto an existing verb or a thin helper. Adds no orchestration logic."""
    command, remainder = _split_verb(line)
    if not command:
        return ShellResult()
    if command in ("quit", "exit"):
        return ShellResult(quit=True)
    if command in ("help", "?"):
        print(_HELP, file=ctx.out)
        return ShellResult()
    # Path-taking verbs: the argument is the raw (unquoted) remainder — never POSIX-tokenized (M1).
    if command == "enqueue":
        return _do_enqueue(ctx, _unquote(remainder))
    if command == "cancel":
        return _do_cancel(ctx, _unquote(remainder))
    if command == "logs":
        return _do_logs(ctx, _unquote(remainder))
    if command in ("ps", "jobs"):
        return _do_ps(ctx)
    if command in ("up", "watch"):
        return _do_up(ctx)
    # Id/flag-taking verbs re-tokenize the remainder OS-aware.
    try:
        rest = _tokens(remainder)
    except ValueError as exc:  # unbalanced quotes
        print(f"shell: {exc}", file=ctx.out)
        return ShellResult()
    if command == "down":
        # The stop ladder lives in cmd_stop; forward flags verbatim, but --non-interactive so a busy
        # daemon is refused-with-instructions rather than dropping into input() in the REPL (H1).
        return ShellResult(exit_code=ctx.run_cli(_argv(ctx, "stop", ["--non-interactive", *rest])))
    if command == "restart":
        return ShellResult(
            exit_code=ctx.run_cli(_argv(ctx, "restart", ["--non-interactive", *rest]))
        )
    if command == "rerun":
        # rerun's confirmation prompt fights the REPL's own stdin reader exactly like down/restart
        # (H1); forward --non-interactive so it refuses-with-instructions (pass --yes) instead of
        # blocking inside input().
        return ShellResult(exit_code=ctx.run_cli(_argv(ctx, "rerun", ["--non-interactive", *rest])))
    if command in _FORWARD_VERBS:
        return ShellResult(exit_code=ctx.run_cli(_argv(ctx, command, rest)))
    print(f"shell: unknown command {command!r} (try 'help')", file=ctx.out)
    return ShellResult()


def _do_enqueue(ctx: ShellContext, arg: str) -> ShellResult:
    if not arg:
        print("usage: enqueue <file>", file=ctx.out)
        return ShellResult()
    src = Path(arg)
    if not src.is_file():
        print(f"shell: no such file: {src}", file=ctx.out)
        return ShellResult()
    dest_dir = cli.pending_dir(ctx.config)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest_dir / src.name)
    served = " — 'up' to start serving" if ctx.daemon is None else " (the daemon runs it next tick)"
    print(f"shell: enqueued {src.name}{served}", file=ctx.out)
    return ShellResult()


def _find_pending(config: OrchestratorConfig, target: str) -> Path | None:
    for path in cli.select_pending(cli.pending_dir(config)):
        if target in (path.name, path.stem, cli._scan_pending_meta(path).task_id):
            return path
    return None


def _do_cancel(ctx: ShellContext, arg: str) -> ShellResult:
    if not arg:
        print("usage: cancel <id|file>", file=ctx.out)
        return ShellResult()
    target = arg
    match = _find_pending(ctx.config, target)
    if match is None:
        print(
            f"shell: {target!r} is not a pending file; an active task has no clean per-task cancel "
            "— use 'down' (soft) or 'down --force-full' (hard, POSIX only)",
            file=ctx.out,
        )
        return ShellResult()
    rejected = cli.worc_home_for(ctx.config) / "tasks" / "rejected"
    rejected.mkdir(parents=True, exist_ok=True)
    shutil.move(str(match), str(rejected / match.name))
    print(f"shell: cancelled pending {match.name} -> {rejected}", file=ctx.out)
    return ShellResult()


def _do_ps(ctx: ShellContext) -> ShellResult:
    db_path = Path(cli.worc_home_for(ctx.config)) / "state.db"
    store = StateStore.open_readonly(db_path) if db_path.is_file() else None
    try:
        snapshot = cli.build_top_snapshot(
            ctx.config,
            store,
            selector=ctx.selector,
            log_path=ctx.log_path,
            log_tail_lines=cli._TOP_LOG_TAIL_LINES,
            recent_limit=cli._LIST_RECENT_DEFAULT,
        )
    finally:
        if store is not None:
            store.close()
    print(cli.render_top(snapshot), file=ctx.out)
    return ShellResult()


def _active_task_id(config: OrchestratorConfig) -> str | None:
    db_path = Path(cli.worc_home_for(config)) / "state.db"
    if not db_path.is_file():
        return None
    store = StateStore.open_readonly(db_path)
    try:
        active = store.find_active_tasks()
        return active[0].task_id if active else None
    finally:
        store.close()


def _latest_stdout_log(worc_home: Path, task_id: str) -> Path | None:
    task_dir = worc_home / "logs" / task_id
    if not task_dir.is_dir():
        return None
    logs = sorted(task_dir.rglob("stdout.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _do_logs(ctx: ShellContext, arg: str) -> ShellResult:
    if ctx.config.logging.artifacts == "minimal":
        print(
            "shell: logging.artifacts=minimal — only result.json is kept (no stdout.log)",
            file=ctx.out,
        )
        return ShellResult()
    task_id = arg if arg else _active_task_id(ctx.config)
    if task_id is None:
        print("shell: no task id given and none active", file=ctx.out)
        return ShellResult()
    path = _latest_stdout_log(cli.worc_home_for(ctx.config), task_id)
    if path is None:
        print(f"shell: no stdout.log yet for {task_id}", file=ctx.out)
        return ShellResult()
    for entry in cli.tail_lines(path, _LOGS_TAIL_LINES):
        print(entry, file=ctx.out)
    return ShellResult()


def _do_up(ctx: ShellContext) -> ShellResult:
    """Start serving the queue: spawn a verified watch daemon if none is running (``up``/``watch``).

    On-demand and idempotent: a live daemon short-circuits. A verified-failed spawn leaves the shell
    idle (``ctx.daemon`` stays ``None``) with the real error already printed by :func:`start_watch`.
    """
    pid_path = process_control.pid_file_path(cli.worc_home_for(ctx.config))
    if process_control.running_daemon_pid(pid_path) is not None:
        print("shell: a watch daemon is already running", file=ctx.out)
        return ShellResult()
    ctx.daemon = start_watch(
        ctx.config,
        selector=ctx.selector,
        log_file=str(ctx.log_path),
        spawn_fn=ctx.spawn_fn,
        config_path=ctx.config_path,
        out=ctx.out,
    )
    return ShellResult()


class _LogTailer:
    """Stateful tail of the daemon log: each :meth:`poll` returns only lines added since the last.

    Rotation-aware — when the file shrinks (the rotating handler rolled over) the cursor resets and
    the new file is re-read from the top. Pure/sync, so it is unit-tested against a growing file.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._seen = 0

    def poll(self) -> list[str]:
        if self._path is None or not self._path.is_file():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        if len(lines) < self._seen:
            self._seen = 0  # rotated / truncated — start over from the new file
        new = lines[self._seen :]
        self._seen = len(lines)
        return new


def _run_scripted(ctx: ShellContext, lines: Sequence[str]) -> int:
    """Headless dispatch over a pre-seeded line list — the testable stand-in for the REPL."""
    for line in lines:
        if dispatch(line, ctx).quit:
            break
    return 0


async def _tail_loop(
    tailer: _LogTailer, out: TextIO, interval: float = _TAIL_INTERVAL_SECONDS
) -> None:
    while True:
        for line in tailer.poll():
            print(line, file=out)
        await asyncio.sleep(interval)


def _run_interactive(ctx: ShellContext) -> int:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    session: PromptSession[str] = PromptSession("worc> ")
    # Tail the resolved daemon log regardless of attach/idle state — if the operator starts serving
    # later with `up`, the daemon writes to this same path and the tail follows it.
    tailer = _LogTailer(ctx.log_path)

    async def _loop() -> None:
        tail = asyncio.create_task(_tail_loop(tailer, ctx.out))
        try:
            with patch_stdout():
                print(_BANNER, file=ctx.out)
                while True:
                    try:
                        line = (await session.prompt_async()).strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if dispatch(line, ctx).quit:
                        break
        finally:
            tail.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tail

    asyncio.run(_loop())
    return 0


def _shutdown_daemon(ctx: ShellContext) -> None:
    """Detach on quit: leave the daemon (and any in-flight task) running (the Decision — M3).

    ``quit`` never stops the daemon — a long P4-style task survives closing the panel and reopening
    ``worc shell`` reattaches. Stopping is only the explicit ``down`` (soft) / ``down --force-full``
    (hard). Idle (nothing serving) → nothing to say. This is print-only; it never blocks on input (a
    prompt here would violate the REPL's single-stdin-reader rule — H1).
    """
    daemon = ctx.daemon
    if daemon is None:
        return
    print(
        f"shell: detached — daemon (pid {daemon.pid}) left running (any in-flight task continues). "
        "Reopen 'worc shell' to reattach, or stop it with 'worc stop' / 'worc stop --force-full'.",
        file=ctx.out,
    )


def run_shell(
    config: OrchestratorConfig,
    *,
    config_path: str | None = None,
    queue: str | None = None,
    log_file: str | None = None,
    spawn_fn: Callable[..., object] = agent_process.spawn_detached,
    run_cli: Callable[[list[str]], int] = cli.main,
    lines: Sequence[str] | None = None,
    out: TextIO | None = None,
) -> int:
    """Run the console: attach to a live daemon (else open idle), dispatch commands, then detach.

    Entry is passive — it never auto-spawns and never claims a task (the Decision); serving starts
    only on an explicit ``up``. ``lines`` drives a headless scripted run (tests); otherwise the
    interactive prompt_toolkit REPL runs — gated on the ``[shell]`` extra *before* anything happens,
    so a missing extra exits cleanly.
    """
    out = out if out is not None else sys.stdout
    selector = queue or config.orchestrator.queue
    if lines is None and not _prompt_toolkit_available():
        print(
            "worc shell needs the [shell] extra: pip install wastech-orchestrator[shell]", file=out
        )
        return 2
    daemon = attach_watch(config, log_file=log_file, out=out)  # passive: attach if live, else idle
    if daemon is None:
        print(
            "shell: no watch daemon running — the queue is NOT being served. "
            "Type 'up' to start serving, or 'enqueue <file>' to queue a task first.",
            file=out,
        )
    ctx = ShellContext(
        config=config,
        selector=selector,
        log_path=daemon_log_path(config, log_file),
        config_path=config_path,
        daemon=daemon,
        spawn_fn=spawn_fn,
        run_cli=run_cli,
        out=out,
    )
    try:
        return _run_scripted(ctx, lines) if lines is not None else _run_interactive(ctx)
    finally:
        _shutdown_daemon(ctx)
