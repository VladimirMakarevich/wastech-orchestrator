"""Interactive operator console (``worc shell``) — a *client* over the watch daemon.

The console never hosts the engine: it spawns or attaches to a ``watch`` daemon (the only engine
host), reads ``state.db`` read-only, tails the daemon ``--log-file``, enqueues by dropping task
files into ``pending_dir(config)``, and dispatches every other verb onto the existing ``cmd_*``
functions via :func:`wastech_orchestrator.cli.main`. It adds no orchestration logic and introduces
no new git/network capability.

``prompt_toolkit`` is the optional ``[shell]`` extra: it is imported **only** inside
:func:`_run_interactive`, so importing this module (and the whole headless dispatch surface) works
without it. The interactive tail diverges from ``worc top``'s reader-thread model — here it is an
``asyncio`` task that prints through ``patch_stdout()`` so daemon log lines land above the prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import shlex
import shutil
import sys
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

_BANNER = "worc shell — client over the watch daemon. Type 'help' for commands, 'quit' to exit."

_HELP = """\
commands:
  enqueue <file>              copy a task file into the pending queue (daemon runs it next tick)
  ps | jobs                   snapshot: active task + node, queue, recent, log tail
  status [<id>]               persisted status of the active/latest task (or <id>)
  tasks                       list every known task (read-only)
  logs [<id>]                 tail the active/<id> agent stdout.log
  prs                         list orchestrator PRs awaiting merge
  merge-task <id>             merge a reviewed PR (refuses while the daemon is up)
  finalize ... | rerun ...    one-shot lifecycle verbs (refuse while the daemon is up)
  cancel <id>                 de-queue a pending task file (-> .worc/tasks/rejected)
  up                          start a watch daemon if none is running
  down [--force|--force-full] stop the daemon (idle: no prompt; busy: confirm/force)
  restart [...]               restart the daemon
  quit | exit                 leave (a spawned, idle daemon is stopped; attached: left running)
"""

# Verbs forwarded verbatim to the existing CLI dispatch; their own slot/daemon guards apply.
_FORWARD_VERBS = frozenset({"status", "tasks", "prs", "merge-task", "finalize", "rerun", "list"})


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
    dispatcher headless-testable without a TTY, a real daemon, or prompt_toolkit."""

    config: OrchestratorConfig
    selector: str
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


def spawn_or_attach_watch(
    config: OrchestratorConfig,
    *,
    selector: str,
    log_file: str | None,
    spawn_fn: Callable[..., object] = agent_process.spawn_detached,
    config_path: str | None = None,
    out: TextIO | None = None,
) -> DaemonHandle:
    """Attach to a live daemon (leave it running on exit) or spawn a managed ``watch`` child.

    A live PID file means a daemon is already up → attach (we can only tail its log if the operator
    told us the path via ``--log-file``). Otherwise spawn ``python -m wastech_orchestrator.cli watch
    --log-file <path> --queue <selector>`` as an argv list (no shell, stdin not inherited) so the
    console knows the log to tail and the queue it serves.
    """
    out = out if out is not None else sys.stdout
    pid_path = process_control.pid_file_path(cli.worc_home_for(config))
    pid = process_control.running_daemon_pid(pid_path)
    if pid is not None:
        log_path = Path(log_file) if log_file else None
        note = "" if log_path else " — pass --log-file to tail its log"
        print(f"shell: attached to running daemon (pid {pid}){note}", file=out)
        return DaemonHandle(attached=True, process=None, log_path=log_path, pid=pid)

    log_path = Path(log_file) if log_file else cli.worc_home_for(config) / "logs" / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "wastech_orchestrator.cli"]
    if config_path:
        argv += ["--config", config_path]
    argv += ["watch", "--log-file", str(log_path), "--queue", selector]
    proc = spawn_fn(argv)  # spawn_detached: argv list, shell=False, stdin not inherited
    spawned_pid = getattr(proc, "pid", None)
    print(f"shell: started watch daemon (pid {spawned_pid}) -> {log_path}", file=out)
    return DaemonHandle(attached=False, process=proc, log_path=log_path, pid=spawned_pid)


def dispatch(line: str, ctx: ShellContext) -> ShellResult:
    """Map one console line onto an existing verb or a thin helper. Adds no orchestration logic."""
    line = line.strip()
    if not line:
        return ShellResult()
    try:
        command, *rest = shlex.split(line)
    except ValueError as exc:  # unbalanced quotes
        print(f"shell: {exc}", file=ctx.out)
        return ShellResult()
    if command in ("quit", "exit"):
        return ShellResult(quit=True)
    if command in ("help", "?"):
        print(_HELP, file=ctx.out)
        return ShellResult()
    if command in ("enqueue", "run"):
        return _do_enqueue(ctx, rest)
    if command == "cancel":
        return _do_cancel(ctx, rest)
    if command in ("ps", "jobs"):
        return _do_ps(ctx)
    if command == "logs":
        return _do_logs(ctx, rest)
    if command == "up":
        return _do_up(ctx)
    if command == "down":  # the stop ladder lives in cmd_stop (Phase 3); forward flags verbatim
        return ShellResult(exit_code=ctx.run_cli(_argv(ctx, "stop", rest)))
    if command == "restart":
        return ShellResult(exit_code=ctx.run_cli(_argv(ctx, "restart", rest)))
    if command in _FORWARD_VERBS:
        return ShellResult(exit_code=ctx.run_cli(_argv(ctx, command, rest)))
    print(f"shell: unknown command {command!r} (try 'help')", file=ctx.out)
    return ShellResult()


def _do_enqueue(ctx: ShellContext, rest: Sequence[str]) -> ShellResult:
    if not rest:
        print("usage: enqueue <file>", file=ctx.out)
        return ShellResult()
    src = Path(rest[0])
    if not src.is_file():
        print(f"shell: no such file: {src}", file=ctx.out)
        return ShellResult()
    dest_dir = cli.pending_dir(ctx.config)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest_dir / src.name)
    print(f"shell: enqueued {src.name} (the daemon picks it up next tick)", file=ctx.out)
    return ShellResult()


def _find_pending(config: OrchestratorConfig, target: str) -> Path | None:
    for path in cli.select_pending(cli.pending_dir(config)):
        if target in (path.name, path.stem, cli._scan_pending_meta(path).task_id):
            return path
    return None


def _do_cancel(ctx: ShellContext, rest: Sequence[str]) -> ShellResult:
    if not rest:
        print("usage: cancel <id|file>", file=ctx.out)
        return ShellResult()
    target = rest[0]
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
            log_path=ctx.daemon.log_path if ctx.daemon else None,
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


def _do_logs(ctx: ShellContext, rest: Sequence[str]) -> ShellResult:
    if ctx.config.logging.artifacts == "minimal":
        print(
            "shell: logging.artifacts=minimal — only result.json is kept (no stdout.log)",
            file=ctx.out,
        )
        return ShellResult()
    task_id = rest[0] if rest else _active_task_id(ctx.config)
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
    pid_path = process_control.pid_file_path(cli.worc_home_for(ctx.config))
    if process_control.running_daemon_pid(pid_path) is not None:
        print("shell: a watch daemon is already running", file=ctx.out)
        return ShellResult()
    prior_log = str(ctx.daemon.log_path) if ctx.daemon and ctx.daemon.log_path else None
    ctx.daemon = spawn_or_attach_watch(
        ctx.config,
        selector=ctx.selector,
        log_file=prior_log,
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
    tailer = _LogTailer(ctx.daemon.log_path if ctx.daemon else None)

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
    """Leave an attached or busy daemon running; soft-stop a spawned, idle one (Phase-3 ladder)."""
    daemon = ctx.daemon
    if daemon is None:
        return
    if daemon.attached:
        print(f"shell: detached — daemon (pid {daemon.pid}) left running", file=ctx.out)
        return
    if cli.has_active_task(ctx.config):
        print(
            f"shell: a task is active — spawned daemon (pid {daemon.pid}) left running; "
            "stop later with 'worc stop'",
            file=ctx.out,
        )
        return
    print("shell: stopping the spawned daemon ...", file=ctx.out)
    ctx.run_cli(_argv(ctx, "stop", []))


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
    """Run the console: spawn/attach a daemon, dispatch commands, then shut the daemon down.

    ``lines`` drives a headless scripted run (tests); otherwise the interactive prompt_toolkit REPL
    runs — and is gated on the ``[shell]`` extra *before* anything is spawned, so a missing extra
    exits cleanly without leaving a daemon behind.
    """
    out = out if out is not None else sys.stdout
    selector = queue or config.orchestrator.queue
    if lines is None and not _prompt_toolkit_available():
        print(
            "worc shell needs the [shell] extra: pip install wastech-orchestrator[shell]", file=out
        )
        return 2
    daemon = spawn_or_attach_watch(
        config,
        selector=selector,
        log_file=log_file,
        spawn_fn=spawn_fn,
        config_path=config_path,
        out=out,
    )
    ctx = ShellContext(
        config=config,
        selector=selector,
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
