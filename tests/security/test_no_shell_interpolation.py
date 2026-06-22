"""No-shell-interpolation across all call sites (spec §12.5, §6.1).

Every external process is launched as an argv list, never through a shell, and no user string is
ever spliced into a command. We prove this structurally: the *only* module that touches
``subprocess`` is the single safe runner (``providers/process.py``), it passes ``shell=False``, and
the Check Runner splits commands into argv tokens rather than handing them to a shell.

The per-adapter "prompt is never interpolated into argv" proofs live in
tests/providers/test_codex_command.py and test_claude_command.py.
"""

from __future__ import annotations

from pathlib import Path

import wastech_orchestrator
from wastech_orchestrator.checks.model import normalize_check_command

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


def test_safe_runner_launches_without_a_shell() -> None:
    source = _PROCESS_RUNNER.read_text(encoding="utf-8")
    assert "shell=False" in source


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
