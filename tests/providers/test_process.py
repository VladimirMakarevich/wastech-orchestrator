"""Tests for the safe process runner (coding-style.md).

A portable Python one-liner stands in for any external CLI, so these tests run identically on
Windows and POSIX with no real Codex/Claude binary.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import wastech_orchestrator.providers.process as process_mod
from wastech_orchestrator.providers.process import AgentHandleRecorder, run_process

# Every test here is a slow integration test (real git / subprocess / process tree).
pytestmark = pytest.mark.slow


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class _FakeContainment:
    """A no-op :class:`ProcessContainment` for tests that fake ``Popen``.

    Records the lifecycle calls and issues **no real signal**, so a fabricated pid is never
    ``killpg``'d. ``proven``/``detail`` drive the quiescence result the barrier acts on.
    """

    def __init__(
        self,
        *,
        popen_kwargs: dict[str, object] | None = None,
        proven: bool = True,
        detail: str = "fake",
    ) -> None:
        self._kwargs = popen_kwargs or {}
        self._proven = proven
        self._detail = detail
        self.adopted: object = None
        self.terminate_calls = 0
        self.prove_calls = 0

    def popen_kwargs(self) -> dict[str, object]:
        return dict(self._kwargs)

    def adopt(self, proc: object) -> None:
        self.adopted = proc

    def terminate(self) -> None:
        self.terminate_calls += 1

    def terminate_and_prove(self) -> process_mod.QuiescenceResult:
        self.prove_calls += 1
        return process_mod.QuiescenceResult(proven=self._proven, detail=self._detail)


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


def test_run_process_merges_containment_popen_kwargs_and_shell_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_process places the child into the injected containment — the containment's
    ``popen_kwargs`` are merged into ``Popen`` — and always launches shell-free. A fake containment
    keeps the fabricated pid from being signalled."""
    captured: dict[str, object] = {}

    class _FakeProc:
        pid = 4321
        returncode = 0

        def communicate(self, input: object = None, timeout: object = None) -> tuple[None, str]:
            return (None, "")

    def fake_popen(argv: object, **kwargs: object) -> _FakeProc:
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_process(
        _py("pass"),
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "o.log",
        make_containment=lambda: _FakeContainment(popen_kwargs={"sentinel_kw": True}),
    )
    assert captured.get("sentinel_kw") is True  # the containment's launch kwargs reached Popen
    assert captured.get("shell") is False


def test_posix_containment_leads_its_own_session_group() -> None:
    """The POSIX containment launches the child as its own session/group leader
    (``start_new_session``), so its whole subtree is reachable by one ``killpg``; the Windows Job
    Object leaves ``start_new_session`` off (a no-op there — the job does the owning)."""
    assert process_mod.PosixProcessContainment().popen_kwargs() == {"start_new_session": True}
    assert process_mod.WindowsJobObjectContainment(win32=object()).popen_kwargs() == {  # type: ignore[arg-type]
        "start_new_session": False
    }


def test_trusted_run_contains_and_still_proves_quiescence(tmp_path: Path) -> None:
    """``trusted=True`` (the orchestrator-git fast path) still launches inside a real containment
    and proves quiescence on return — it only skips the POSIX ``ps`` descendant sweep, not the
    group isolation or the emptiness proof."""
    result = run_process(
        _py("pass"),
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "o.log",
        trusted=True,
    )
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.quiescence is not None and result.quiescence.proven is True


def test_explicit_make_containment_overrides_trusted(tmp_path: Path) -> None:
    """An explicitly injected ``make_containment`` wins over ``trusted`` — the test seam is
    preserved, so ``trusted`` only chooses the default factory when none is injected."""
    fake = _FakeContainment()
    run_process(
        _py("pass"),
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "o.log",
        trusted=True,
        make_containment=lambda: fake,
    )
    assert fake.prove_calls == 1  # the injected fake ran, not the trusted factory


def test_recorder_records_the_child_then_clears_on_reap(tmp_path: Path) -> None:
    """A supplied recorder sees the child's ``(pid, pgid)`` on spawn (pgid == pid, a group leader)
    then a reap on return — the handle a hard stop uses to find the subtree."""
    events: list[tuple[object, ...]] = []
    recorder = AgentHandleRecorder(
        on_spawn=lambda pid, pgid: events.append(("spawn", pid, pgid)),
        on_reap=lambda: events.append(("reap",)),
    )
    run_process(
        _py("pass"),
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "o.log",
        recorder=recorder,
    )
    assert [e[0] for e in events] == ["spawn", "reap"]
    _, pid, pgid = events[0]
    assert isinstance(pid, int) and pid > 0
    assert pgid == pid  # recorded as its own group leader


def test_recorder_untouched_when_launch_fails(tmp_path: Path) -> None:
    """A failed launch spawns no child, so neither callback fires — there is no handle to record
    or clear (the recorder only tracks a process that actually launched)."""
    events: list[str] = []
    recorder = AgentHandleRecorder(
        on_spawn=lambda pid, pgid: events.append("spawn"),
        on_reap=lambda: events.append("reap"),
    )
    result = run_process(
        [str(tmp_path / "definitely-not-a-real-binary")],
        cwd=tmp_path,
        env={},
        timeout_seconds=30,
        stdout_path=tmp_path / "o.log",
        recorder=recorder,
    )
    assert result.launch_error is not None
    assert events == []  # no child launched → neither spawn nor reap fired


def test_timeout_reaps_the_subtree_and_proves_quiescence(tmp_path: Path) -> None:
    """On timeout the real containment kills the child's subtree (so the drain returns) and proves
    the group empty; classification stays ``timed_out``. Uses the real containment against a real
    child, so the sleeping process is genuinely reaped."""
    result = run_process(
        _py("import time; time.sleep(10)"),
        cwd=tmp_path,
        env={},
        timeout_seconds=1,
        stdout_path=tmp_path / "o.log",
    )
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.quiescence is not None
    assert result.quiescence.proven is True  # the reaped subtree was proven gone before returning


def test_keyboard_interrupt_terminates_containment_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A propagating interrupt terminates the containment before re-raising (so a foreground
    ``worc run`` never orphans the agent), and the quiescence proof still runs in ``finally``."""
    fake = _FakeContainment()

    class _FakeProc:
        pid = 5555
        returncode = None

        def __init__(self) -> None:
            self._calls = 0

        def communicate(self, input: object = None, timeout: object = None) -> tuple[None, str]:
            self._calls += 1
            if self._calls == 1:
                raise KeyboardInterrupt
            return (None, "")  # the post-kill drain returns cleanly

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: _FakeProc())
    with pytest.raises(KeyboardInterrupt):
        run_process(
            _py("pass"),
            cwd=tmp_path,
            env={},
            timeout_seconds=30,
            stdout_path=tmp_path / "o.log",
            make_containment=lambda: fake,
        )
    assert fake.terminate_calls >= 1  # subtree killed before re-raising
    assert fake.prove_calls == 1  # `finally` still ran the bounded quiescence proof


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
    # No capture_path → stdout/stderr are discarded (the daemon is observed via its --log-file).
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    # The spawned daemon leads its own process group on POSIX (so `stop --force-full` can group-kill
    # it + its agents without touching the console); a no-op on Windows.
    assert kwargs["start_new_session"] is (os.name != "nt")


def test_spawn_detached_capture_path_redirects_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """``capture_path`` redirects the child's stdout/stderr to a startup log (crash recovery), with
    stderr merged into stdout; stdin stays ``DEVNULL`` and the file is created in a fresh dir."""
    import subprocess
    from pathlib import Path

    from wastech_orchestrator.providers import process as proc_mod

    captured: dict[str, object] = {}

    class _Popen:
        def __init__(self, argv: object, **kwargs: object) -> None:
            captured["kwargs"] = kwargs
            self.pid = 7

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    log = Path(tmp_path) / "logs" / "daemon-startup.log"  # type: ignore[arg-type]
    proc_mod.spawn_detached(["worc", "watch"], capture_path=log)
    assert log.is_file()  # parent dir created + truncated open
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT  # stderr merged into the captured stdout stream
    assert kwargs["stdout"] is not subprocess.DEVNULL  # a real file handle


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
