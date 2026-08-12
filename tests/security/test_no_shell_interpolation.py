"""No-shell-interpolation across all call sites.

Every external process is launched as an argv list, never through a shell, and no user string is
ever spliced into a command. We prove this structurally: the *only* module that touches
``subprocess`` is the single safe runner (``providers/process.py``), it passes ``shell=False``, and
the Check Runner splits commands into argv tokens rather than handing them to a shell.

The per-adapter "prompt is never interpolated into argv" proofs live in
tests/providers/test_codex_command.py and test_claude_command.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import wastech_orchestrator
from wastech_orchestrator.checks.model import normalize_check_command
from wastech_orchestrator.providers import process as process_mod

_SRC = Path(wastech_orchestrator.__file__).resolve().parent
_PROCESS_RUNNER = _SRC / "providers" / "process.py"


def _py_files() -> list[Path]:
    return [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_subprocess_is_only_used_in_the_safe_runner() -> None:
    # Every external launch funnels through providers/process.run_process — the single chokepoint
    # that guarantees an argv list and shell=False. No other module may import subprocess.
    offenders = [
        str(p.relative_to(_SRC))
        for p in _py_files()
        if p != _PROCESS_RUNNER and "subprocess" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"subprocess used outside the safe runner: {offenders}"


def test_safe_runner_passes_shell_false_to_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Behavioral proof (not a source-substring grep): intercept the real subprocess.Popen and assert
    # the runner actually hands it shell=False with an argv list. A refactor that moved the literal
    # to a constant would no longer be able to silently defeat this guard.
    captured: dict[str, Any] = {}

    class _FakeProc:
        pid = 4321
        returncode = 0

        def communicate(self, input: object = None, timeout: object = None) -> tuple[None, str]:
            return (None, "")

    def fake_popen(argv: object, **kwargs: object) -> _FakeProc:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc()

    class _NoopContainment:
        # Isolate the launch from the containment's own process bookkeeping (its POSIX
        # descendant snapshot shells out to `ps` via subprocess, which would otherwise be the last
        # Popen captured here). That `ps` call is itself argv-list + shell=False in-source and is
        # covered by test_subprocess_is_only_used_in_the_safe_runner above.
        def popen_kwargs(self) -> dict[str, object]:
            return {"start_new_session": True}

        def adopt(self, proc: object) -> None:
            pass

        def terminate(self) -> None:
            pass

        def terminate_and_prove(self) -> process_mod.QuiescenceResult:
            return process_mod.QuiescenceResult(proven=True, detail="noop")

    monkeypatch.setattr(process_mod.subprocess, "Popen", fake_popen)
    process_mod.run_process(
        ["echo", "hi"],
        cwd=tmp_path,
        env={},
        timeout_seconds=5,
        stdout_path=tmp_path / "stdout.log",
        make_containment=_NoopContainment,
    )
    assert captured["kwargs"]["shell"] is False
    assert captured["argv"] == ["echo", "hi"]


def test_no_module_enables_a_shell() -> None:
    # `shell=True` may appear only in the safe runner's docstring ("never `shell=True`"); it must
    # never appear as a real keyword argument in any other module.
    offenders = [
        str(p.relative_to(_SRC))
        for p in _py_files()
        if p != _PROCESS_RUNNER and "shell=True" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_check_commands_are_split_into_argv_not_shell_interpreted() -> None:
    # A shell metacharacter stays a literal argv token — it is never expanded by a shell. This pins
    # the real resolver path (normalize_check_command), the only command-splitting seam.
    assert normalize_check_command("npm test").argv == ("npm", "test")
    assert normalize_check_command("echo $(whoami)").argv == ("echo", "$(whoami)")
