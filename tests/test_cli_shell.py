"""``worc shell`` (Phase 2 of the operator console): headless dispatch, spawn/attach, the scripted
REPL, and the lazy ``[shell]`` extra. No real TTY, no real daemon, no engine."""

from __future__ import annotations

import asyncio
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
        log_path=cli_shell.daemon_log_path(config, None),
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


def test_clear_forwards_to_the_cli_verb(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    # `clear` is forwarded like status/tasks — the screen-wipe lives once in cmd_clear.
    calls: list[list[str]] = []
    ctx = _ctx(make_git_config(tmp_path / "clone"), run_cli=lambda argv: calls.append(argv) or 0)
    result = cli_shell.dispatch("clear", ctx)
    assert result.quit is False
    assert calls == [["--config", "/cfg.yaml", "clear"]]


def test_clear_listed_in_help(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    ctx = _ctx(make_git_config(tmp_path / "clone"))
    cli_shell.dispatch("help", ctx)
    assert "clear" in _out(ctx)


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


def test_run_verb_no_longer_aliases_enqueue(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    # `run` was an enqueue alias; the new vocabulary frees it for a future one-shot, so it is now an
    # unknown command (the operator uses `enqueue <file>` + `up`).
    ctx = _ctx(make_git_config(tmp_path / "clone"))
    src = tmp_path / "task.md"
    src.write_text("---\nid: t1\ntitle: T\n---\nx\n", encoding="utf-8")
    cli_shell.dispatch(f"run {src}", ctx)
    assert "unknown command 'run'" in _out(ctx)
    assert not (cli.pending_dir(ctx.config) / "task.md").exists()


def test_enqueue_path_with_spaces_is_not_split(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    # The raw remainder (not POSIX-tokenized) keeps a path with an embedded space intact.
    config = make_git_config(tmp_path / "clone")
    spaced = tmp_path / "a dir" / "my task.md"
    spaced.parent.mkdir(parents=True)
    spaced.write_text("---\nid: t1\ntitle: T\n---\nx\n", encoding="utf-8")
    ctx = _ctx(config)
    cli_shell.dispatch(f"enqueue {spaced}", ctx)
    assert (cli.pending_dir(config) / "my task.md").is_file()
    assert "enqueued my task.md" in _out(ctx)


def test_enqueue_quoted_path_is_unquoted(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    spaced = tmp_path / "a dir" / "t.md"
    spaced.parent.mkdir(parents=True)
    spaced.write_text("---\nid: t1\ntitle: T\n---\nx\n", encoding="utf-8")
    ctx = _ctx(config)
    cli_shell.dispatch(f'enqueue "{spaced}"', ctx)
    assert (cli.pending_dir(config) / "t.md").is_file()


def test_split_verb_keeps_backslash_path_raw() -> None:
    # A Windows-style absolute path survives verb-splitting (POSIX shlex would strip the backslashes
    # and split on the embedded space).
    verb, remainder = cli_shell._split_verb(r"enqueue C:\Users\x y\task.md")
    assert verb == "enqueue"
    assert remainder == r"C:\Users\x y\task.md"


def test_unquote_strips_one_matching_layer() -> None:
    assert cli_shell._unquote('"C:\\a b\\t.md"') == "C:\\a b\\t.md"
    assert cli_shell._unquote("'x y'") == "x y"
    assert cli_shell._unquote("plain") == "plain"


def test_up_and_watch_short_circuit_when_daemon_running(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    pid_path = process_control.pid_file_path(cli.worc_home_for(config))
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    process_control.write_pid_file(pid_path, pid=os.getpid())  # our own pid → "live"
    spawned = {"v": False}
    ctx = cli_shell.ShellContext(
        config=config,
        selector="default",
        log_path=cli_shell.daemon_log_path(config, None),
        spawn_fn=lambda *_a, **_k: spawned.__setitem__("v", True) or _FakeProc(1),
        run_cli=lambda _argv: 0,
        out=io.StringIO(),
    )
    cli_shell.dispatch("up", ctx)
    cli_shell.dispatch("watch", ctx)  # alias of up
    assert spawned["v"] is False  # already running → no second spawn
    assert ctx.out.getvalue().count("already running") == 2


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
    cli_shell.dispatch("down --force", ctx)  # down maps to stop; console forces --non-interactive
    cli_shell.dispatch("restart", ctx)
    cli_shell.dispatch("rerun t1 --continue", ctx)  # console forces --non-interactive
    assert calls == [
        ["--config", "/cfg.yaml", "status", "t1"],
        ["--config", "/cfg.yaml", "merge-task", "t1"],
        ["--config", "/cfg.yaml", "stop", "--non-interactive", "--force"],
        ["--config", "/cfg.yaml", "restart", "--non-interactive"],
        ["--config", "/cfg.yaml", "rerun", "--non-interactive", "t1", "--continue"],
    ]


def test_forward_surfaces_the_guard_exit_code(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    # The real cmd_finalize/merge-task refuse (exit 1) while the daemon is up; the console surfaces
    # that verbatim — the slot/daemon guard is reused, not re-implemented.
    ctx = _ctx(make_git_config(tmp_path / "clone"), run_cli=lambda _argv: 1)
    assert cli_shell.dispatch("finalize t1 --as done", ctx).exit_code == 1


def test_interactive_dispatch_runs_nested_cli_off_the_event_loop(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def run_cli(argv: list[str]) -> int:
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append(argv)
        return 0

    ctx = _ctx(make_git_config(tmp_path / "clone"), run_cli=run_cli)
    result = asyncio.run(cli_shell._dispatch_interactive("rerun t1 --yes", ctx))

    assert result.exit_code == 0
    assert calls == [
        ["--config", "/cfg.yaml", "rerun", "--non-interactive", "t1", "--yes"],
    ]


def test_forwarded_parser_exit_is_command_local(
    capsys: pytest.CaptureFixture[str], make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    ctx = _ctx(make_git_config(tmp_path / "clone"), run_cli=cli.main)
    result = cli_shell.dispatch("status --bogus", ctx)
    assert result.quit is False
    assert result.exit_code == 2
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err


# --- reliable spawn (start_watch) + attach ----------------------------------------------


def test_start_watch_builds_argv_with_parent_flags_before_subcommand(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    captured: dict[str, object] = {}

    def fake_spawn(argv: list[str], **kwargs: object) -> _FakeProc:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc(999)

    handle = cli_shell.start_watch(
        config,
        selector="backend",
        log_file=None,
        spawn_fn=fake_spawn,
        config_path="/cfg.yaml",
        out=io.StringIO(),
        ready_probe=lambda: 999,  # daemon "came up" → skip the real PID-file poll
    )
    assert handle is not None
    assert handle.attached is False
    assert handle.pid == 999
    argv = captured["argv"]
    assert isinstance(argv, list)
    watch_at = argv.index("watch")
    # The argparse fix: --config/--log-file are PARENT flags and must precede the subcommand (the
    # old code appended --log-file after `watch`, so the daemon died on 'unrecognized arguments').
    assert argv.index("--config") < watch_at
    assert argv.index("--log-file") < watch_at
    assert argv.index("--queue") > watch_at
    assert argv[argv.index("--config") + 1] == "/cfg.yaml"
    assert argv[argv.index("--queue") + 1] == "backend"
    assert argv[argv.index("--log-file") + 1].endswith("daemon.log")
    # The child's stdout/stderr are captured for crash recovery (not DEVNULL'd).
    assert str(captured["kwargs"]).find("capture_path") != -1


def test_watch_launcher_prefers_console_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_shell.shutil, "which", lambda name: "/usr/local/bin/worc" if name == "worc" else None
    )
    assert cli_shell._watch_launcher() == ["/usr/local/bin/worc"]


def test_watch_launcher_falls_back_to_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_shell.shutil, "which", lambda _name: None)
    assert cli_shell._watch_launcher() == [sys.executable, "-m", "wastech_orchestrator.cli"]


def test_start_watch_surfaces_startup_error_when_daemon_dies(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")

    class _DeadProc:
        pid = 555

        def poll(self) -> int:
            return 2  # already exited (argparse-style failure) → fast-fail, no timeout wait

    def fake_spawn(argv: list[str], *, capture_path: str, **_k: object) -> _DeadProc:
        # Emulate the daemon writing an argparse error to its captured stderr before dying.
        Path(capture_path).write_text(
            "wastech-orchestrator: error: unrecognized arguments: --oops\n", encoding="utf-8"
        )
        return _DeadProc()

    out = io.StringIO()
    handle = cli_shell.start_watch(
        config,
        selector="default",
        log_file=None,
        spawn_fn=fake_spawn,
        out=out,
        ready_probe=lambda: None,  # PID file never appears
    )
    assert handle is None  # verified-dead → shell stays idle, no false "started"
    text = out.getvalue()
    assert "failed to start" in text
    assert "unrecognized arguments: --oops" in text  # the REAL error is surfaced


def test_wait_until_alive_times_out_without_pid_file() -> None:
    clock = {"t": 0.0}
    handle = cli_shell._wait_until_alive(
        Path("/nope/orchestrator.pid"),
        _FakeProc(1),  # no poll() → treated as still running
        timeout=1.0,
        poll=0.5,
        ready_probe=lambda: None,
        sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
        now_fn=lambda: clock["t"],
    )
    assert handle is None


def test_attach_watch_attaches_to_live_daemon_without_spawning(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    pid_path = process_control.pid_file_path(cli.worc_home_for(config))
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    process_control.write_pid_file(pid_path, pid=os.getpid())  # our own pid → "live"

    out = io.StringIO()
    handle = cli_shell.attach_watch(config, log_file="d.log", out=out)
    assert handle is not None
    assert handle.attached is True
    assert handle.pid == os.getpid()
    assert "attached to running daemon" in out.getvalue()


def test_attach_watch_returns_none_when_idle(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    assert cli_shell.attach_watch(config, log_file=None, out=io.StringIO()) is None


# --- scripted run + shutdown ------------------------------------------------------------


def test_run_shell_passive_entry_idle_and_enqueue_then_quit(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    src = tmp_path / "task.md"
    src.write_text("---\nid: t1\ntitle: T\n---\nx\n", encoding="utf-8")
    calls: list[list[str]] = []
    spawned = {"v": False}
    out = io.StringIO()
    rc = cli_shell.run_shell(
        config,
        config_path=None,
        spawn_fn=lambda *_a, **_k: spawned.__setitem__("v", True) or _FakeProc(123),
        run_cli=lambda argv: calls.append(argv) or 0,
        lines=[f"enqueue {src}", "ps", "quit"],
        out=out,
    )
    assert rc == 0
    assert (cli.pending_dir(config) / "task.md").is_file()
    assert spawned["v"] is False  # passive entry: never auto-spawns
    assert calls == []  # quit detaches; idle → nothing to stop
    assert "NOT being served" in out.getvalue()  # idle banner


def test_run_shell_detaches_attached_daemon_on_quit(
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
    assert calls == []  # detach-on-quit → never stopped
    assert "left running" in out.getvalue()
    # Part A: scripted quit prints the loud warning (never blocks) before detaching.
    assert "WARNING" in out.getvalue()
    assert "still serving the queue" in out.getvalue()


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


def test_run_interactive_continues_after_forwarded_parser_exit(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    prompt_toolkit = pytest.importorskip("prompt_toolkit")
    import prompt_toolkit.patch_stdout as patch_stdout_mod

    class _ScriptedSession:
        def __init__(self, *_a: object, **_k: object) -> None:
            self._it = iter(["status --bogus", "help", "quit"])

        async def prompt_async(self) -> str:
            try:
                return next(self._it)
            except StopIteration as exc:
                raise EOFError from exc

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _ScriptedSession)
    monkeypatch.setattr(patch_stdout_mod, "patch_stdout", lambda: contextlib.nullcontext())

    ctx = _ctx(make_git_config(tmp_path / "clone"), run_cli=cli.main)
    rc = cli_shell._run_interactive(ctx)
    assert rc == 0
    assert "commands:" in _out(ctx)


def test_run_interactive_quit_confirmation_declines_then_accepts(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    prompt_toolkit = pytest.importorskip("prompt_toolkit")
    import prompt_toolkit.patch_stdout as patch_stdout_mod

    class _ScriptedSession:
        def __init__(self, *_a: object, **_k: object) -> None:
            # line 'quit' → decline ('n') → still in REPL → line 'quit' → confirm ('y') → exit.
            self._it = iter(["quit", "n", "quit", "y"])

        async def prompt_async(self, message: str = "") -> str:
            try:
                return next(self._it)
            except StopIteration as exc:
                raise EOFError from exc

    config = make_git_config(tmp_path / "clone")
    _with_live_daemon(config)
    monkeypatch.setattr(prompt_toolkit, "PromptSession", _ScriptedSession)
    monkeypatch.setattr(patch_stdout_mod, "patch_stdout", lambda: contextlib.nullcontext())

    ctx = _ctx(config)
    rc = cli_shell._run_interactive(ctx)
    assert rc == 0
    out = _out(ctx)
    assert "WARNING" in out
    assert "quit cancelled" in out  # the first quit was declined


# --- Part A: quit safety (warning + confirmation) ---------------------------------------


def _with_live_daemon(config: OrchestratorConfig) -> None:
    pid_path = process_control.pid_file_path(cli.worc_home_for(config))
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    process_control.write_pid_file(pid_path, pid=os.getpid())  # our own pid → "live"


class _FakePrompter:
    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    async def prompt_async(self, message: str) -> str:
        self.prompts.append(message)
        if not self._answers:
            raise EOFError
        return self._answers.pop(0)


def test_quit_warning_none_without_daemon(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    assert cli_shell._quit_warning(_ctx(make_git_config(tmp_path / "clone"))) is None


def test_quit_warning_idle_serving(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    _with_live_daemon(config)
    warning = cli_shell._quit_warning(_ctx(config))
    assert warning is not None
    assert "still serving the queue" in warning


def test_quit_warning_busy(
    monkeypatch: pytest.MonkeyPatch, make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    config = make_git_config(tmp_path / "clone")
    _with_live_daemon(config)
    monkeypatch.setattr(cli, "has_active_task", lambda _cfg: True)
    warning = cli_shell._quit_warning(_ctx(config))
    assert warning is not None
    assert "actively running" in warning


def test_confirm_quit_no_daemon_exits_without_prompting(
    make_git_config: _ConfigFactory, tmp_path: Path
) -> None:
    ctx = _ctx(make_git_config(tmp_path / "clone"))
    session = _FakePrompter("n")  # would decline if consulted
    assert asyncio.run(cli_shell._confirm_quit(ctx, session)) is True
    assert session.prompts == []  # no daemon → never prompted


def test_confirm_quit_accepts_on_yes(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    _with_live_daemon(config)
    ctx = _ctx(config)
    session = _FakePrompter("y")
    assert asyncio.run(cli_shell._confirm_quit(ctx, session)) is True
    assert session.prompts  # was consulted


def test_confirm_quit_declines_on_no(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    _with_live_daemon(config)
    session = _FakePrompter("n")
    assert asyncio.run(cli_shell._confirm_quit(_ctx(config), session)) is False


def test_confirm_quit_declines_on_eof(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    _with_live_daemon(config)
    session = _FakePrompter()  # no answers → prompt_async raises EOFError
    assert asyncio.run(cli_shell._confirm_quit(_ctx(config), session)) is False


# --- Part B: shell promote verb + atomic enqueue ----------------------------------------


def test_promote_verb_moves_staged_file(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    ctx = _ctx(config)
    prep = cli.preparing_dir(config)
    prep.mkdir(parents=True)
    (prep / "t1.md").write_text(
        "---\nid: t1\ntitle: T\n---\n## Description\n\nx\n", encoding="utf-8"
    )
    cli_shell.dispatch("promote t1", ctx)
    assert (cli.pending_dir(config) / "t1.md").is_file()
    assert not (prep / "t1.md").exists()
    assert "promoted t1.md -> pending" in _out(ctx)


def test_promote_all_verb(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    ctx = _ctx(config)
    prep = cli.preparing_dir(config)
    prep.mkdir(parents=True)
    (prep / "a.md").write_text("---\nid: a\ntitle: A\n---\n## Description\n\nx\n", encoding="utf-8")
    cli_shell.dispatch("promote --all", ctx)
    assert (cli.pending_dir(config) / "a.md").is_file()
    assert "promoted a.md -> pending" in _out(ctx)


def test_promote_verb_usage_when_empty(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    ctx = _ctx(make_git_config(tmp_path / "clone"))
    cli_shell.dispatch("promote", ctx)
    assert "usage: promote" in _out(ctx)


def test_enqueue_is_atomic_no_temp_left(make_git_config: _ConfigFactory, tmp_path: Path) -> None:
    config = make_git_config(tmp_path / "clone")
    ctx = _ctx(config)
    src = tmp_path / "task.md"
    src.write_text("---\nid: t1\ntitle: T\n---\n## Description\n\nx\n", encoding="utf-8")
    cli_shell.dispatch(f"enqueue {src}", ctx)
    pending = cli.pending_dir(config)
    assert (pending / "task.md").is_file()
    assert list(pending.glob("*.tmp")) == []  # no partial-write temp left behind


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

    handle = cli_shell.start_watch(
        config,
        selector="default",
        log_file=str(log),
        spawn_fn=spawn_fn,
        out=io.StringIO(),
        ready_probe=lambda: 4321,  # the real daemon writes a PID file; the tiny child does not
    )
    assert handle is not None
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
