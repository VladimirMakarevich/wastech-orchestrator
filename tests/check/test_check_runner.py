"""Unit tests for the Check Runner (§4.8 / testing stage)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.check_runner import CheckRunner
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.process import ProcessResult


def _config(commands: list[str], *, timeout: int = 1800) -> OrchestratorConfig:
    cmds = "\n".join(f"    - {c!r}" for c in commands)
    text = f"""
repo:
  url: "git@example.com:o/r.git"
agents:
  allowed: [codex]
  providers:
    codex:
      command: "codex"
checks:
  commands:
{cmds if commands else "    []"}
  timeout_seconds: {timeout}
"""
    return loads_config(text).config


class _FakeProc:
    """Records each invocation and returns a scripted ProcessResult per call."""

    def __init__(self, results: Sequence[ProcessResult]) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: dict,
        timeout_seconds: int,
        stdout_path: str,
        stdin_text: str | None = None,
    ) -> ProcessResult:
        self.calls.append(
            {"argv": list(argv), "cwd": cwd, "timeout": timeout_seconds, "stdout": stdout_path}
        )
        # Simulate the child writing to its stdout log.
        Path(stdout_path).write_text("check output\n", encoding="utf-8")
        return self._results[len(self.calls) - 1]


def _ok() -> ProcessResult:
    return ProcessResult(
        exit_code=0,
        timed_out=False,
        launch_error=None,
        duration_seconds=0.1,
        stdout_path="x",
        stderr_text="",
    )


def _fail(exit_code: int = 1, stderr: str = "boom") -> ProcessResult:
    return ProcessResult(
        exit_code=exit_code,
        timed_out=False,
        launch_error=None,
        duration_seconds=0.1,
        stdout_path="x",
        stderr_text=stderr,
    )


def test_no_commands_passes(tmp_path: Path) -> None:
    runner = CheckRunner(_config([]), run_process=_FakeProc([]))
    outcome = runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    assert outcome.passed is True
    assert outcome.runs == ()


def test_all_commands_pass(tmp_path: Path) -> None:
    fake = _FakeProc([_ok(), _ok()])
    runner = CheckRunner(_config(["pytest", "ruff check ."]), run_process=fake)
    outcome = runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    assert outcome.passed is True
    assert len(outcome.runs) == 2
    assert all(r.passed for r in outcome.runs)
    # Both log files exist under checks/.
    logs = list((tmp_path / "logs" / "t1" / "checks").glob("*.log"))
    assert len(logs) == 2


def test_argv_split_no_shell(tmp_path: Path) -> None:
    fake = _FakeProc([_ok()])
    runner = CheckRunner(_config(["npm run lint"]), run_process=fake)
    runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    assert fake.calls[0]["argv"] == ["npm", "run", "lint"]


def test_stops_at_first_failure(tmp_path: Path) -> None:
    fake = _FakeProc([_fail(), _ok()])
    runner = CheckRunner(_config(["pytest", "ruff check ."]), run_process=fake)
    outcome = runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    assert outcome.passed is False
    assert len(outcome.runs) == 1  # second command never ran
    assert outcome.first_failure_log is not None
    assert outcome.first_failure_log.endswith(".log")


def test_timeout_is_failure(tmp_path: Path) -> None:
    timed_out = ProcessResult(
        exit_code=None,
        timed_out=True,
        launch_error=None,
        duration_seconds=1.0,
        stdout_path="x",
        stderr_text="",
    )
    fake = _FakeProc([timed_out])
    runner = CheckRunner(_config(["pytest"]), run_process=fake)
    outcome = runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    assert outcome.passed is False
    assert outcome.runs[0].timed_out is True


def test_launch_error_is_failure(tmp_path: Path) -> None:
    launch_err = ProcessResult(
        exit_code=None,
        timed_out=False,
        launch_error="could not launch 'pytest'",
        duration_seconds=0.0,
        stdout_path="x",
        stderr_text="",
    )
    fake = _FakeProc([launch_err])
    runner = CheckRunner(_config(["pytest"]), run_process=fake)
    outcome = runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    assert outcome.passed is False


def test_timeout_value_passed_through(tmp_path: Path) -> None:
    fake = _FakeProc([_ok()])
    runner = CheckRunner(_config(["pytest"], timeout=42), run_process=fake)
    runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    assert fake.calls[0]["timeout"] == 42


def test_logs_not_overwritten_across_runs(tmp_path: Path) -> None:
    runner = CheckRunner(_config(["pytest"]), run_process=_FakeProc([_ok(), _ok()]))
    runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    logs = list((tmp_path / "logs" / "t1" / "checks").glob("*.log"))
    assert len(logs) == 2  # the fix-loop re-run did not clobber the first log


def test_subtask_logs_are_prefixed(tmp_path: Path) -> None:
    runner = CheckRunner(_config(["pytest"]), run_process=_FakeProc([_ok()]))
    runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1", subtask=2)
    logs = [p.name for p in (tmp_path / "logs" / "t1" / "checks").glob("*.log")]
    assert logs == ["sub-02-001.log"]


def test_stderr_redacted_in_log(tmp_path: Path) -> None:
    secret = "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    fake = _FakeProc([_fail(stderr=secret)])
    runner = CheckRunner(_config(["pytest"]), run_process=fake)
    outcome = runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    log_text = Path(outcome.first_failure_log).read_text(encoding="utf-8")  # type: ignore[arg-type]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in log_text


@pytest.mark.parametrize("command", ["", "   "])
def test_blank_command_is_skipped(tmp_path: Path, command: str) -> None:
    fake = _FakeProc([])
    runner = CheckRunner(_config([command]), run_process=fake)
    outcome = runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")
    assert outcome.passed is True
    assert fake.calls == []
