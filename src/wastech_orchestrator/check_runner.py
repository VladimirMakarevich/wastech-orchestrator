"""Check Runner (spec §4.8 / the ``testing`` stage).

Runs the configured ``checks.commands`` (config, not hardcoded) through the P2 safe process runner
— an **argv list** (``shlex.split``), never a shell string — with an allowlisted environment and the
per-command ``checks.timeout_seconds``. Each run is written to ``checks/<run-id>.log`` (§10).

A check failure is a **quality** error: the caller routes it to ``fixing`` with **no provider
fallback** (§4.8). The Check Runner itself never transitions state nor touches git; it returns a
:class:`CheckOutcome` and the orchestrator records the ``check_runs`` rows and drives the loop.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.process import ProcessResult, run_process
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.security.env import build_child_env

RunProcess = Callable[..., ProcessResult]


@dataclass(frozen=True)
class CheckRunResult:
    """The outcome of one configured check command."""

    command: str
    exit_code: int | None
    timed_out: bool
    passed: bool
    log_path: str


@dataclass(frozen=True)
class CheckOutcome:
    """The aggregate result of a Check Runner invocation."""

    passed: bool
    runs: tuple[CheckRunResult, ...]
    first_failure_log: str | None = None


class CheckRunner:
    """Runs ``checks.commands`` for a task (or subtask) and reports pass/fail."""

    def __init__(
        self, config: OrchestratorConfig, *, run_process: RunProcess = run_process
    ) -> None:
        self._config = config
        self._run_process = run_process

    def run(
        self,
        *,
        clone_dir: str | Path,
        artifacts_root: str | Path,
        task_id: str,
        subtask: int | None = None,
    ) -> CheckOutcome:
        """Run each configured command in order, stopping at the first failure (§4.8).

        Returns ``passed=True`` with no runs when no checks are configured.
        """
        commands = self._config.checks.commands
        timeout = self._config.checks.timeout_seconds
        env = build_child_env(self._config.security.allowed_environment)
        checks_dir = task_artifact_dir(artifacts_root, task_id) / "checks"
        checks_dir.mkdir(parents=True, exist_ok=True)

        runs: list[CheckRunResult] = []
        for command in commands:
            argv = shlex.split(command, posix=True)
            if not argv:
                continue  # an empty/blank command is a no-op, not a failure
            log_path = self._next_log_path(checks_dir, subtask)
            result = self._run_process(
                argv,
                cwd=clone_dir,
                env=env,
                timeout_seconds=timeout,
                stdout_path=str(log_path),
            )
            self._append_stderr(log_path, result.stderr_text, result)
            passed = result.exit_code == 0 and not result.timed_out and result.launch_error is None
            runs.append(
                CheckRunResult(
                    command=command,
                    exit_code=result.exit_code,
                    timed_out=result.timed_out,
                    passed=passed,
                    log_path=str(log_path),
                )
            )
            if not passed:
                return CheckOutcome(passed=False, runs=tuple(runs), first_failure_log=str(log_path))

        return CheckOutcome(passed=True, runs=tuple(runs))

    def _next_log_path(self, checks_dir: Path, subtask: int | None) -> Path:
        """A fresh, non-overwriting ``<run-id>.log`` path (logs are never overwritten, §10)."""
        prefix = "" if subtask is None else f"sub-{subtask:02d}-"
        existing = sorted(checks_dir.glob(f"{prefix}*.log"))
        return checks_dir / f"{prefix}{len(existing) + 1:03d}.log"

    def _append_stderr(self, log_path: Path, stderr_text: str, result: ProcessResult) -> None:
        """Append the (redacted) stderr and a status footer to the run log."""
        footer = []
        if result.launch_error:
            footer.append(f"launch_error: {result.launch_error}")
        if result.timed_out:
            footer.append("timed_out: true")
        stderr = redact_text(stderr_text) if stderr_text else ""
        with log_path.open("a", encoding="utf-8") as fh:
            if stderr:
                fh.write("\n----- stderr -----\n")
                fh.write(stderr)
            if footer:
                fh.write("\n----- status -----\n")
                fh.write("\n".join(footer) + "\n")


def split_command(command: str) -> Sequence[str]:
    """Public helper: split a configured check command into an argv list (no shell)."""
    return shlex.split(command, posix=True)
