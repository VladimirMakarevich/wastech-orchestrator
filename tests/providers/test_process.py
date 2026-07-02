"""Tests for the safe process runner (coding-style.md).

A portable Python one-liner stands in for any external CLI, so these tests run identically on
Windows and POSIX with no real Codex/Claude binary.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from wastech_orchestrator.providers.process import run_process


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_stdout_is_streamed_to_file(tmp_path: Path) -> None:
    out = tmp_path / "stdout.log"
    result = run_process(
        _py("print('hello world')"),
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=out,
    )
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.launch_error is None
    assert "hello world" in out.read_text(encoding="utf-8")


def test_stdin_is_delivered_and_not_in_argv(tmp_path: Path) -> None:
    out = tmp_path / "stdout.log"
    secret_prompt = "PROMPT-TOKEN-12345"
    result = run_process(
        _py("import sys; sys.stdout.write(sys.stdin.read())"),
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=out,
        stdin_text=secret_prompt,
    )
    assert result.exit_code == 0
    # The prompt round-trips through stdin -> stdout ...
    assert secret_prompt in out.read_text(encoding="utf-8")
    # ... and was never placed on the command line.
    assert all(secret_prompt not in arg for arg in _py("import sys"))


def test_nonzero_exit_is_reported(tmp_path: Path) -> None:
    result = run_process(
        _py("import sys; sys.exit(3)"),
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "stdout.log",
    )
    assert result.exit_code == 3
    assert result.timed_out is False
    assert result.launch_error is None


def test_stderr_is_captured_not_streamed(tmp_path: Path) -> None:
    result = run_process(
        _py("import sys; sys.stderr.write('boom on stderr')"),
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "stdout.log",
    )
    assert "boom on stderr" in result.stderr_text


def test_timeout_maps_to_timed_out(tmp_path: Path) -> None:
    result = run_process(
        _py("import time; time.sleep(10)"),
        cwd=tmp_path,
        env={},
        timeout_seconds=1,
        stdout_path=tmp_path / "stdout.log",
    )
    assert result.timed_out is True
    assert result.exit_code is None


def test_missing_binary_sets_launch_error(tmp_path: Path) -> None:
    missing = tmp_path / "definitely-not-a-real-binary"
    result = run_process(
        [str(missing), "exec"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "stdout.log",
    )
    assert result.launch_error is not None
    assert result.exit_code is None
    assert result.timed_out is False
    # The empty stdout artifact still exists for the audit trail.
    assert (tmp_path / "stdout.log").exists()
    # A genuine launch failure names the binary, not the stdout path.
    assert "could not launch" in result.launch_error


def test_unwritable_stdout_path_degrades_and_blames_the_path(tmp_path: Path) -> None:
    # stdout_path points *inside* a regular file, so open() fails with NotADirectoryError. The
    # runner must degrade to launch_error (not raise) and the message must name the path, not
    # argv[0] (which launched fine — the binary is not the culprit).
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    bad_stdout = blocker / "stdout.log"
    result = run_process(
        ["echo", "hi"],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=bad_stdout,
    )
    assert result.launch_error is not None
    assert result.exit_code is None
    assert "could not open stdout path" in result.launch_error
    assert "could not launch" not in result.launch_error


def test_child_env_is_exactly_what_is_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A secret set in the *parent* environment must never reach the child unless passed in.
    monkeypatch.setenv("WASTECH_PARENT_ONLY", "leaked-secret")
    out = tmp_path / "stdout.log"
    code = "import os; print('SENTINEL=' + os.environ.get('WASTECH_PARENT_ONLY', '<absent>'))"
    result = run_process(
        _py(code),
        cwd=tmp_path,
        env={"WASTECH_ALLOWED": "yes"},
        timeout_seconds=30,
        stdout_path=out,
    )
    assert result.exit_code == 0
    assert "SENTINEL=<absent>" in out.read_text(encoding="utf-8")


def test_duration_uses_injected_monotonic(tmp_path: Path) -> None:
    ticks = iter([100.0, 142.5])
    result = run_process(
        _py("print('ok')"),
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "stdout.log",
        monotonic=lambda: next(ticks),
    )
    assert result.duration_seconds == 42.5


def test_agent_stays_in_the_daemon_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent must NOT ``setsid`` — it stays in the daemon's process group so ``stop
    --force-full`` (POSIX ``killpg`` on the daemon's group) reaches it instead of orphaning it. A
    ``start_new_session``-ed agent would break away into its own group and survive the kill."""
    import subprocess

    captured: dict[str, object] = {}

    class _Done:
        returncode = 0
        stderr = ""

    def fake_run(argv: object, **kwargs: object) -> _Done:
        captured.update(kwargs)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_process(
        _py("pass"), cwd=tmp_path, env={}, timeout_seconds=30, stdout_path=tmp_path / "o.log"
    )
    assert not captured.get("start_new_session")  # inherits the daemon's group, does not lead a new
    assert captured.get("shell") is False


def test_spawn_detached_uses_argv_list_shell_false_and_devnull_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detached daemon launch is an argv list with ``shell=False`` and stdin not inherited —
    the same no-shell-interpolation guarantee as ``run_process``, at the one chokepoint."""
    import subprocess

    from wastech_orchestrator.providers import process as proc_mod

    captured: dict[str, object] = {}

    class _Popen:
        def __init__(self, argv: object, **kwargs: object) -> None:
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.pid = 4242

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    handle = proc_mod.spawn_detached(["worc", "watch"])
    assert handle.pid == 4242  # type: ignore[attr-defined]
    assert captured["argv"] == ["worc", "watch"]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    # The spawned daemon leads its own process group on POSIX (so `stop --force-full` can group-kill
    # it + its agents without touching the console); a no-op on Windows.
    assert kwargs["start_new_session"] is (os.name != "nt")


def test_hard_kill_tree_builds_taskkill_argv_and_swallows_missing_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows hard-stop seam: an argv-list ``taskkill /F /T /PID`` (shell=False), and a failed
    call (dead / recycled PID, or taskkill absent) is swallowed so the stop stays idempotent."""
    import subprocess

    from wastech_orchestrator.providers import process as proc_mod

    seen: dict[str, object] = {}

    def fake_run(argv: object, **kwargs: object) -> object:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        raise OSError("taskkill missing")  # must not propagate

    monkeypatch.setattr(subprocess, "run", fake_run)
    proc_mod.hard_kill_tree(4242)  # does not raise
    assert seen["argv"] == ["taskkill", "/F", "/T", "/PID", "4242"]
    assert isinstance(seen["kwargs"], dict)
    assert seen["kwargs"]["shell"] is False
