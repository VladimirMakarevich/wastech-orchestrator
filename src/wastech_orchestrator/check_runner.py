"""Check Runner (the ``testing`` stage).

Runs the operator's selected ``checks.command_sets`` — normalized to ``checks.model.ResolvedCheck``
argv lists, never shell strings — through the shared safe process runner, each in its set's ``cwd``
with
an allowlisted environment and the set's (or global) timeout. Each run is written to
``checks/<run-id>.log``.

Every selected check runs (no fail-fast): the runner aggregates the results so the human sees the
full picture and ``fixing`` can address all quality failures in one cycle. A check failure is a
**quality** error → the caller routes it to ``fixing`` with no provider fallback. A **launch**
failure (a missing executable on a *required* set) is an infrastructure event, not a quality error,
reported via ``CheckOutcome.any_launch_failed`` so the caller hands an incomplete gate to a human
rather than spending a fix iteration on a problem no code change can fix. A set marked
``skip_if_unavailable`` whose toolchain binary is absent is **skipped** — loudly, never "passed" —
reported via ``CheckOutcome.any_skipped``. The Check Runner never transitions state nor touches git.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from wastech_orchestrator.checks.model import (
    ResolvedCheck,
    ResolvedCheckSet,
    normalize_command_sets,
)
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.observability.progress import run_with_heartbeat
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.process import ProcessResult, run_process
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.security.env import build_child_env

RunProcess = Callable[..., ProcessResult]
Which = Callable[[str], str | None]
_LOG = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Wall-clock UTC ISO timestamp for a check's ``check_runs`` interval.

    A per-check wall-clock read bracketing the launched check, distinct from the monotonic clock
    used for the duration log line — mirrors how the provider adapter stamps its attempt timestamps.
    """
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class CheckRunResult:
    """The outcome of one check command."""

    command: str
    exit_code: int | None
    timed_out: bool
    passed: bool
    log_path: str
    # The check's real wall-clock interval, captured around the launched check, so the
    # ``check_runs`` row carries a measured duration instead of two identical row-write stamps.
    started_at: str
    finished_at: str
    name: str = ""
    # A launch failure (binary/module not found) is an infrastructure event, not a quality failure.
    launch_failed: bool = False
    launch_error: str | None = None
    # Set when the command's toolchain binary was absent and its set is ``skip_if_unavailable``: the
    # check did not run (never ``passed``); the gate is incomplete for it.
    skipped: bool = False


@dataclass(frozen=True)
class CheckOutcome:
    """The aggregate result of a Check Runner invocation (every selected check is run)."""

    passed: bool
    runs: tuple[CheckRunResult, ...]
    # Aggregates over ``runs`` (see :meth:`CheckRunner._aggregate`):
    any_quality_failed: bool = False  # ≥1 executed check failed on its exit code → ``fixing``
    any_launch_failed: bool = False  # ≥1 required check could not be launched → incomplete gate
    any_skipped: bool = False  # ≥1 check skipped (opted-in set, toolchain absent)
    nothing_ran: bool = False  # got ≥1 check but every one was skipped → incomplete gate
    first_failure_log: str | None = None  # first quality-failure log, for the fixing loop


class CheckRunner:
    """Runs the selected ``checks.command_sets`` for a task/subtask and aggregates pass/fail."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        run_process: RunProcess = run_process,
        which: Which = shutil.which,
        heartbeat_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._run_process = run_process
        self._which = which
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
        selected: Sequence[ResolvedCheckSet] | None = None,
        clock: Callable[[], str] = _utc_now_iso,
    ) -> CheckOutcome:
        """Run every check in the ``selected`` sets and aggregate the results.

        ``selected`` is the diff-selected command sets; when ``None`` all of the configured
        ``checks.command_sets`` are normalized and run. Returns a vacuous ``passed=True`` with no
        runs when there are no checks. A check whose set is ``skip_if_unavailable`` and whose binary
        is absent is recorded as ``skipped``; a missing binary on a *required* set surfaces as a
        launch failure via the process runner.
        """
        if selected is not None:
            sets = list(selected)
        else:
            sets = list(normalize_command_sets(self._config.checks.command_sets))
        global_timeout = self._config.checks.timeout_seconds
        env = build_child_env(self._config.security)
        checks_dir = task_artifact_dir(artifacts_root, task_id) / "checks"
        checks_dir.mkdir(parents=True, exist_ok=True)

        runs: list[CheckRunResult] = []
        log = bind(_LOG, task_id=task_id, stage="testing")
        index = 0
        for cset in sets:
            timeout = cset.timeout_seconds or global_timeout
            for check in cset.checks:
                index += 1
                argv = list(check.argv)
                log_path = self._next_log_path(checks_dir, subtask)
                fields: dict[str, object] = {
                    "check_index": index,
                    "check": check.name,
                    "command": argv[0],
                    "command_set": cset.name,
                    "timeout_seconds": timeout,
                }
                if subtask is not None:
                    fields["subtask"] = subtask
                if cset.skip_if_unavailable and self._which(argv[0]) is None:
                    # Opted-in set, toolchain binary absent on host → skip (loud), never "passed".
                    log.warning("check skipped: toolchain absent", extra=fields)
                    runs.append(self._skipped_result(argv, check, log_path, clock()))
                    continue
                cwd = Path(clone_dir) / check.cwd if check.cwd else Path(clone_dir)
                started = self._monotonic()
                started_at = clock()  # wall-clock bracket for the check_runs interval
                log.info("check started", extra=fields)
                result = run_with_heartbeat(
                    partial(
                        self._run_process,
                        argv,
                        cwd=cwd,
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
                finished_at = clock()  # end of the wall-clock bracket
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
                        started_at=started_at,
                        finished_at=finished_at,
                        launch_failed=launch_failed,
                        launch_error=result.launch_error,
                    )
                )

        return self._aggregate(tuple(runs))

    def _aggregate(self, runs: tuple[CheckRunResult, ...]) -> CheckOutcome:
        """Fold per-check results into the node-level outcome (run-all precedence is the node's)."""
        executed = [r for r in runs if not r.skipped]
        any_skipped = any(r.skipped for r in runs)
        any_launch_failed = any(r.launch_failed for r in runs)
        any_quality_failed = any(not r.passed and not r.launch_failed for r in executed)
        nothing_ran = bool(runs) and not executed  # got checks, but every one was skipped
        first_failure_log = next(
            (r.log_path for r in executed if not r.passed and not r.launch_failed), None
        )
        passed = not (any_quality_failed or any_launch_failed or nothing_ran)
        return CheckOutcome(
            passed=passed,
            runs=runs,
            any_quality_failed=any_quality_failed,
            any_launch_failed=any_launch_failed,
            any_skipped=any_skipped,
            nothing_ran=nothing_ran,
            first_failure_log=first_failure_log,
        )

    def _skipped_result(
        self, argv: list[str], check: ResolvedCheck, log_path: Path, now: str
    ) -> CheckRunResult:
        """Record a skipped check with a loud, distinct log line (never overwritten).

        A skip launches nothing, so ``started_at`` == ``finished_at``: an honest zero-length
        interval, distinct from the old row-write double-stamp of a check that really ran.
        """
        log_path.write_text(
            f"skipped (toolchain absent): {argv[0]!r} not found on host\n", encoding="utf-8"
        )
        return CheckRunResult(
            command=" ".join(argv),
            name=check.name,
            exit_code=None,
            timed_out=False,
            passed=False,
            log_path=str(log_path),
            started_at=now,
            finished_at=now,
            skipped=True,
        )

    def _next_log_path(self, checks_dir: Path, subtask: int | None) -> Path:
        """A fresh, non-overwriting ``<run-id>.log`` path (logs are never overwritten)."""
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
