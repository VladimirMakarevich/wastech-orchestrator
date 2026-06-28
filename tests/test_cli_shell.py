"""``worc shell`` (Phase 2 of the operator console): headless dispatch, spawn/attach, the scripted
REPL, and the lazy ``[shell]`` extra. No real TTY, no real daemon, no engine."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from wastech_orchestrator import cli, cli_shell, process_control
from wastech_orchestrator.config.schema import OrchestratorConfig

_ConfigFactory = Callable[..., OrchestratorConfig]


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def _ctx(
    config: OrchestratorConfig,
    *,
    daemon: cli_shell.DaemonHandle | None = None,
    run_cli: Callable[[list[str]], int] | None = None,
) -> cli_shell.ShellContext:
    return cli_shell.ShellContext(
        config=config,
        selector="default",
        config_path="/cfg.yaml",
        daemon=daemon,
        spawn_fn=lambda *_a, **_k: _FakeProc(4321),
        run_cli=run_cli or (lambda _argv: 0),
        out=io.StringIO(),
    )


def _out(ctx: cli_shell.ShellContext) -> str:
    assert isinstance(ctx.out, io.StringIO)
    return ctx.out.getvalue()


# --- dispatch routing -------------------------------------------------------------------


def test_dispatch_quit_help_empty(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    ctx = _ctx(make_git_config(tmp_path / "clone"))
    assert cli_shell.dispatch("quit", ctx).quit is True
    assert cli_shell.dispatch("exit", ctx).quit is True
    assert cli_shell.dispatch("", ctx).quit is False
    assert cli_shell.dispatch("help", ctx).quit is False
    assert "commands:" in _out(ctx)


def test_dispatch_unknown_command(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    ctx = _ctx(make_git_config(tmp_path / "clone"))
    assert cli_shell.dispatch("frobnicate", ctx).quit is False
    assert "unknown command" in _out(ctx)


def test_enqueue_copies_file_into_pending(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    ctx = _ctx(config)
    src = tmp_path / "task.md"
    src.write_text("---\nid: t1\ntitle: T\n---\nbody\n", encoding="utf-8")
    cli_shell.dispatch(f"enqueue {src}", ctx)
    assert (cli.pending_dir(config) / "task.md").is_file()
    assert "enqueued task.md" in _out(ctx)


def test_enqueue_missing_file(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    ctx = _ctx(make_git_config(tmp_path / "clone"))
    cli_shell.dispatch("enqueue /nope/missing.md", ctx)
    assert "no such file" in _out(ctx)


def test_cancel_moves_pending_to_rejected(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    ctx = _ctx(config)
    pending = cli.pending_dir(config)
    pending.mkdir(parents=True)
    (pending / "t1.md").write_text("---\nid: t1\ntitle: T\n---\nx\n", encoding="utf-8")
    cli_shell.dispatch("cancel t1", ctx)
    assert not (pending / "t1.md").exists()
    assert (cli.worc_home_for(config) / "tasks" / "rejected" / "t1.md").is_file()
    assert "cancelled pending t1.md" in _out(ctx)


def test_cancel_active_task_explains_stop_ladder(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    ctx = _ctx(config)
    cli.pending_dir(config).mkdir(parents=True)
    cli_shell.dispatch("cancel not-pending", ctx)
    assert "no clean per-task cancel" in _out(ctx)


def test_ps_renders_a_snapshot(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    ctx = _ctx(make_git_config(tmp_path / "clone"))
    cli_shell.dispatch("ps", ctx)
    out = _out(ctx)
    assert "ACTIVE" in out and "QUEUE" in out and "RECENT" in out


def test_forward_verbs_call_run_cli_with_config(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    ctx = _ctx(make_git_config(tmp_path / "clone"), run_cli=lambda argv: calls.append(argv) or 0)
    cli_shell.dispatch("status t1", ctx)
    cli_shell.dispatch("merge-task t1", ctx)
    cli_shell.dispatch("down --force", ctx)  # down maps to stop
    cli_shell.dispatch("restart", ctx)
    assert calls == [
        ["--config", "/cfg.yaml", "status", "t1"],
        ["--config", "/cfg.yaml", "merge-task", "t1"],
        ["--config", "/cfg.yaml", "stop", "--force"],
        ["--config", "/cfg.yaml", "restart"],
    ]


def test_forward_surfaces_the_guard_exit_code(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    # The real cmd_finalize/merge-task refuse (exit 1) while the daemon is up; the console surfaces
    # that verbatim — the slot/daemon guard is reused, not re-implemented.
    ctx = _ctx(make_git_config(tmp_path / "clone"), run_cli=lambda _argv: 1)
    assert cli_shell.dispatch("finalize t1 --as done", ctx).exit_code == 1


# --- spawn / attach ---------------------------------------------------------------------


def test_spawn_watch_builds_argv(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    captured: dict[str, object] = {}

    def fake_spawn(argv: list[str], **kwargs: object) -> _FakeProc:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc(999)

    handle = cli_shell.spawn_or_attach_watch(
        config,
        selector="backend",
        log_file=None,
        spawn_fn=fake_spawn,
        config_path="/cfg.yaml",
        out=io.StringIO(),
    )
    assert handle.attached is False
    assert handle.pid == 999
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[:3] == [sys.executable, "-m", "wastech_orchestrator.cli"]
    assert argv[argv.index("--config") + 1] == "/cfg.yaml"
    assert argv[argv.index("--queue") + 1] == "backend"
    assert argv[argv.index("--log-file") + 1].endswith("daemon.log")
    assert "watch" in argv
    # The shell-false / stdin-DEVNULL guarantee now lives in process.spawn_detached (tested there).


def test_attach_to_live_daemon_does_not_spawn(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    pid_path = process_control.pid_file_path(cli.worc_home_for(config))
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    process_control.write_pid_file(pid_path, pid=os.getpid())  # our own pid → "live"
    spawned = {"v": False}

    def fake_spawn(*_a: object, **_k: object) -> _FakeProc:
        spawned["v"] = True
        return _FakeProc(1)

    out = io.StringIO()
    handle = cli_shell.spawn_or_attach_watch(
        config, selector="default", log_file="d.log", spawn_fn=fake_spawn, out=out
    )
    assert handle.attached is True
    assert handle.pid == os.getpid()
    assert spawned["v"] is False
    assert "attached to running daemon" in out.getvalue()


# --- scripted run + shutdown ------------------------------------------------------------


def test_run_shell_scripted_enqueue_then_quit(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    src = tmp_path / "task.md"
    src.write_text("---\nid: t1\ntitle: T\n---\nx\n", encoding="utf-8")
    calls: list[list[str]] = []
    rc = cli_shell.run_shell(
        config,
        config_path=None,
        spawn_fn=lambda *_a, **_k: _FakeProc(123),
        run_cli=lambda argv: calls.append(argv) or 0,
        lines=[f"enqueue {src}", "ps", "quit"],
        out=io.StringIO(),
    )
    assert rc == 0
    assert (cli.pending_dir(config) / "task.md").is_file()
    # Spawned (no PID file) + idle (no state.db) → shutdown soft-stops via run_cli(["stop"]).
    assert ["stop"] in calls


def test_run_shell_scripted_leaves_attached_daemon_running(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    pid_path = process_control.pid_file_path(cli.worc_home_for(config))
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    process_control.write_pid_file(pid_path, pid=os.getpid())
    calls: list[list[str]] = []
    out = io.StringIO()
    rc = cli_shell.run_shell(
        config,
        spawn_fn=lambda *_a, **_k: _FakeProc(1),
        run_cli=lambda argv: calls.append(argv) or 0,
        lines=["quit"],
        out=out,
    )
    assert rc == 0
    assert calls == []  # attached → never stopped
    assert "left running" in out.getvalue()


def test_run_shell_without_extra_returns_2_and_does_not_spawn(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    monkeypatch.setattr(cli_shell, "_prompt_toolkit_available", lambda: False)
    spawned = {"v": False}
    out = io.StringIO()
    rc = cli_shell.run_shell(
        config,
        lines=None,  # interactive path → gated on the extra
        spawn_fn=lambda *_a, **_k: spawned.__setitem__("v", True) or _FakeProc(1),
        out=out,
    )
    assert rc == 2
    assert "pip install wastech-orchestrator[shell]" in out.getvalue()
    assert spawned["v"] is False  # gated before anything is spawned


# --- the log tailer ---------------------------------------------------------------------


def test_log_tailer_returns_only_new_lines(tmp_path: Path) -> None:
    path = tmp_path / "d.log"
    path.write_text("a\nb\n", encoding="utf-8")
    tailer = cli_shell._LogTailer(path)
    assert tailer.poll() == ["a", "b"]
    assert tailer.poll() == []
    path.write_text("a\nb\nc\n", encoding="utf-8")
    assert tailer.poll() == ["c"]


def test_log_tailer_resets_on_rotation(tmp_path: Path) -> None:
    path = tmp_path / "d.log"
    path.write_text("x\ny\nz\n", encoding="utf-8")
    tailer = cli_shell._LogTailer(path)
    assert tailer.poll() == ["x", "y", "z"]
    path.write_text("fresh\n", encoding="utf-8")  # rotated: shorter than seen
    assert tailer.poll() == ["fresh"]


def test_log_tailer_absent_path() -> None:
    assert cli_shell._LogTailer(None).poll() == []


# --- interactive REPL smoke (needs the extra) -------------------------------------------


def test_run_interactive_quit_smoke(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    prompt_toolkit = pytest.importorskip("prompt_toolkit")
    import prompt_toolkit.patch_stdout as patch_stdout_mod

    class _FakeSession:
        def __init__(self, *_a: object, **_k: object) -> None:
            self._it = iter(["", "quit"])  # blank line (no-op) then quit

        async def prompt_async(self) -> str:
            try:
                return next(self._it)
            except StopIteration as exc:
                raise EOFError from exc

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _FakeSession)
    monkeypatch.setattr(patch_stdout_mod, "patch_stdout", lambda: contextlib.nullcontext())

    rc = cli_shell._run_interactive(_ctx(make_git_config(tmp_path / "clone")))
    assert rc == 0


# --- spawn → tail → reap integration (a real child process) -----------------------------


def test_spawned_child_log_is_tailable_then_reaped(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    log = tmp_path / "daemon.log"

    def spawn_fn(argv: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        # Ignore the real `watch` argv; launch a tiny child that writes to the same --log-file then
        # idles — proving the spawn handle's log_path is what actually gets tailed.
        target = argv[argv.index("--log-file") + 1]
        script = (
            f"import time, pathlib; "
            f"pathlib.Path(r'{target}').write_text('boot\\nready\\n', encoding='utf-8'); "
            f"time.sleep(30)"
        )
        return subprocess.Popen([sys.executable, "-c", script])

    handle = cli_shell.spawn_or_attach_watch(
        config, selector="default", log_file=str(log), spawn_fn=spawn_fn, out=io.StringIO()
    )
    assert isinstance(handle.process, subprocess.Popen)
    try:
        tailer = cli_shell._LogTailer(handle.log_path)
        lines: list[str] = []
        for _ in range(60):  # up to ~3s for the child to write
            lines += tailer.poll()
            if lines:
                break
            time.sleep(0.05)
        assert lines == ["boot", "ready"]
    finally:
        handle.process.terminate()
        handle.process.wait(timeout=5)
