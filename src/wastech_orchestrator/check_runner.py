"""Check Runner (spec §4.8 / the ``testing`` stage).

Runs the configured ``checks.commands`` (config, not hardcoded) through the P2 safe process runner
— an **argv list** (split by ``checks.model.normalize_check_command``), never a shell string — with
an allowlisted environment and the per-command ``checks.timeout_seconds``. Each run is written to
``checks/<run-id>.log`` (§10).

A check failure is a **quality** error: the caller routes it to ``fixing`` with **no provider
fallback** (§4.8). The Check Runner itself never transitions state nor touches git; it returns a
:class:`CheckOutcome` and the orchestrator records the ``check_runs`` rows and drives the loop.

A **launch** failure (a missing executable/module) is *not* a quality error: it is reported via
``CheckOutcome.launch_failed`` so the orchestrator treats it as an infrastructure/preflight event,
never spending a fix iteration on a problem no code change can fix (automatic check discovery §3,
§11). Checks run from the canonical ``checks.model.ResolvedCheck`` argv lists supplied by the
resolver; absent those, the configured ``checks.commands`` are normalized.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from wastech_orchestrator.checks.model import ResolvedCheck, normalize_commands
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.observability.progress import run_with_heartbeat
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.process import ProcessResult, run_process
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.security.env import build_child_env

RunProcess = Callable[..., ProcessResult]
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckRunResult:
    """The outcome of one configured check command."""

    command: str
    exit_code: int | None
    timed_out: bool
    passed: bool
    log_path: str
    name: str = ""
    # A launch failure (binary/module not found) is an infrastructure event, not a quality failure.
    launch_failed: bool = False
    launch_error: str | None = None


@dataclass(frozen=True)
class CheckOutcome:
    """The aggregate result of a Check Runner invocation."""

    passed: bool
    runs: tuple[CheckRunResult, ...]
    first_failure_log: str | None = None
    # Set when the first failure was a *launch* failure: the orchestrator treats it as an infra
    # event (terminal/preflight), never routing it to ``fixing`` (automatic check discovery §11).
    launch_failed: bool = False
    first_launch_error: str | None = None


class CheckRunner:
    """Runs ``checks.commands`` for a task (or subtask) and reports pass/fail."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        run_process: RunProcess = run_process,
        heartbeat_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._run_process = run_process
        self._heartbeat_seconds = heartbeat_seconds
        self._monotonic = monotonic

    @property
    def run_process(self) -> RunProcess:
        """The injected safe process runner — shared with the flow's ``dependency_scan`` checker so
        a test's fake runner drives both, and production uses the one real argv launcher."""
        return self._run_process

    def run(
        self,
        *,
        clone_dir: str | Path,
        artifacts_root: str | Path,
        task_id: str,
        subtask: int | None = None,
        checks: Sequence[ResolvedCheck] | None = None,
    ) -> CheckOutcome:
        """Run each resolved check in order, stopping at the first failure (§4.8).

        ``checks`` is the resolved profile's argv list; when ``None`` the configured
        ``checks.commands`` are normalized (backward compatible). Returns ``passed=True`` with no
        runs when no checks are configured. A *launch* failure short-circuits with
        ``launch_failed=True`` so the caller can treat it as infrastructure rather than a quality
        failure.
        """
        if checks is not None:
            resolved = list(checks)
        else:
            resolved = normalize_commands(self._config.checks.commands)
        timeout = self._config.checks.timeout_seconds
        env = build_child_env(self._config.security.allowed_environment)
        checks_dir = task_artifact_dir(artifacts_root, task_id) / "checks"
        checks_dir.mkdir(parents=True, exist_ok=True)

        runs: list[CheckRunResult] = []
        log = bind(_LOG, task_id=task_id, stage="testing")
        for index, check in enumerate(resolved, start=1):
            argv = list(check.argv)
            log_path = self._next_log_path(checks_dir, subtask)
            fields: dict[str, object] = {
                "check_index": index,
                "check": check.name,
                "command": argv[0],
                "timeout_seconds": timeout,
            }
            if subtask is not None:
                fields["subtask"] = subtask
            started = self._monotonic()
            log.info("check started", extra=fields)
            result = run_with_heartbeat(
                partial(
                    self._run_process,
                    argv,
                    cwd=clone_dir,
                    env=env,
                    timeout_seconds=timeout,
                    stdout_path=str(log_path),
                ),
                logger=log,
                message="check heartbeat",
                interval_seconds=self._heartbeat_seconds,
                fields=fields,
                monotonic=self._monotonic,
            )
            self._append_stderr(log_path, result.stderr_text, result)
            launch_failed = result.launch_error is not None
            passed = result.exit_code == 0 and not result.timed_out and not launch_failed
            log.info(
                "check completed",
                extra={
                    **fields,
                    "passed": passed,
                    "launch_failed": launch_failed,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "duration_seconds": round(self._monotonic() - started, 3),
                },
            )
            runs.append(
                CheckRunResult(
                    command=" ".join(argv),
                    name=check.name,
                    exit_code=result.exit_code,
                    timed_out=result.timed_out,
                    passed=passed,
                    log_path=str(log_path),
                    launch_failed=launch_failed,
                    launch_error=result.launch_error,
                )
            )
            if not passed:
                return CheckOutcome(
                    passed=False,
                    runs=tuple(runs),
                    first_failure_log=str(log_path),
                    launch_failed=launch_failed,
                    first_launch_error=result.launch_error,
                )

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
