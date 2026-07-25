"""Unit tests for the Check Runner (testing stage) — run-all over selected command sets."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.check_runner import CheckRunner
from wastech_orchestrator.checks.model import ResolvedCheck, ResolvedCheckSet
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.observability import logging as obslog
from wastech_orchestrator.providers.process import ProcessResult


@pytest.fixture(autouse=True)
def _reset_package_logger() -> Iterator[None]:
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved = pkg.handlers[:]
    pkg.handlers.clear()
    obslog._configured = False
    yield
    for handler in pkg.handlers:
        handler.close()
    pkg.handlers.clear()
    pkg.handlers.extend(saved)
    obslog._configured = False


def _config(*, timeout: int = 1800, command_sets: str = "") -> OrchestratorConfig:
    text = f"""
repo:
  url: "git@example.com:o/r.git"
agents:
  allowed: [codex]
  providers:
    codex:
      command: "codex"
checks:
  command_sets:
{command_sets or "    {}"}
  timeout_seconds: {timeout}
"""
    return loads_config(text).config


def _check(name: str, argv: tuple[str, ...], *, cwd: str = "") -> ResolvedCheck:
    return ResolvedCheck(name=name, argv=argv, cwd=cwd)


def _set(
    name: str,
    *checks: ResolvedCheck,
    paths: tuple[str, ...] = (),
    timeout: int | None = None,
    skip: bool = False,
) -> ResolvedCheckSet:
    return ResolvedCheckSet(
        name=name, paths=paths, checks=checks, timeout_seconds=timeout, skip_if_unavailable=skip
    )


class _FakeProc:
    """Records each invocation and returns a scripted ProcessResult per call."""

    def __init__(self, results: Sequence[ProcessResult]) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        env: dict,
        timeout_seconds: int,
        stdout_path: str,
        stdin_text: str | None = None,
    ) -> ProcessResult:
        self.calls.append(
            {"argv": list(argv), "cwd": cwd, "timeout": timeout_seconds, "stdout": stdout_path}
        )
        Path(stdout_path).write_text("check output\n", encoding="utf-8")  # simulate child stdout
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


def _present(cmd: str) -> str | None:
    return f"/usr/bin/{cmd}"


def _absent(cmd: str) -> str | None:
    return None


def test_no_sets_passes_vacuously(tmp_path: Path) -> None:
    runner = CheckRunner(_config(), run_process=_FakeProc([]))
    outcome = runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1", selected=())
    assert outcome.passed is True
    assert outcome.runs == ()
    assert outcome.nothing_ran is False  # no checks at all = no gate, not an incomplete gate


def test_all_checks_pass(tmp_path: Path) -> None:
    fake = _FakeProc([_ok(), _ok()])
    runner = CheckRunner(_config(), run_process=fake)
    selected = (
        _set("a", _check("pytest", ("pytest",))),
        _set("b", _check("lint", ("ruff", "check", "."))),
    )
    outcome = runner.run(
        clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1", selected=selected
    )
    assert outcome.passed is True
    assert len(outcome.runs) == 2
    assert all(r.passed for r in outcome.runs)
    assert len(list((tmp_path / "logs" / "t1" / "checks").glob("*.log"))) == 2


def test_check_logs_start_completion_and_duration(tmp_path: Path) -> None:
    stream = io.StringIO()
    obslog.configure_logging(stream=stream)
    ticks = iter((10.0, 12.5))
    runner = CheckRunner(
        _config(),
        run_process=_FakeProc([_ok()]),
        heartbeat_seconds=0,
        monotonic=lambda: next(ticks),
    )
    runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        selected=(_set("a", _check("lint", ("npm", "run", "lint"))),),
    )
    output = stream.getvalue()
    assert 'msg="check started"' in output
    assert 'msg="check completed"' in output
    assert "command=npm" in output
    assert "passed=true" in output
    assert "duration_seconds=2.5" in output


def test_argv_no_shell(tmp_path: Path) -> None:
    fake = _FakeProc([_ok()])
    runner = CheckRunner(_config(), run_process=fake)
    runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        selected=(_set("a", _check("lint", ("npm", "run", "lint"))),),
    )
    assert fake.calls[0]["argv"] == ["npm", "run", "lint"]


def test_runs_all_no_fail_fast(tmp_path: Path) -> None:
    # Р5: a failing check no longer stops the run — every selected check runs and is aggregated.
    fake = _FakeProc([_fail(), _ok()])
    runner = CheckRunner(_config(), run_process=fake)
    selected = (
        _set("a", _check("pytest", ("pytest",))),
        _set("b", _check("lint", ("ruff", "check", "."))),
    )
    outcome = runner.run(
        clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1", selected=selected
    )
    assert outcome.passed is False
    assert outcome.any_quality_failed is True
    assert len(outcome.runs) == 2  # both ran (no fail-fast)
    assert outcome.first_failure_log is not None and outcome.first_failure_log.endswith(".log")


def test_timeout_is_quality_failure(tmp_path: Path) -> None:
    timed_out = ProcessResult(
        exit_code=None,
        timed_out=True,
        launch_error=None,
        duration_seconds=1.0,
        stdout_path="x",
        stderr_text="",
    )
    runner = CheckRunner(_config(), run_process=_FakeProc([timed_out]))
    outcome = runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        selected=(_set("a", _check("pytest", ("pytest",))),),
    )
    assert outcome.passed is False
    assert outcome.runs[0].timed_out is True
    assert outcome.any_quality_failed is True  # the process launched → quality failure
    assert outcome.any_launch_failed is False


def test_required_launch_failure_is_infra(tmp_path: Path) -> None:
    # A required set (not skip_if_unavailable) whose binary cannot launch → infra, not quality.
    launch_err = ProcessResult(
        exit_code=None,
        timed_out=False,
        launch_error="could not launch 'pytest'",
        duration_seconds=0.0,
        stdout_path="x",
        stderr_text="",
    )
    runner = CheckRunner(_config(), run_process=_FakeProc([launch_err]))
    outcome = runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        selected=(_set("a", _check("pytest", ("pytest",))),),
    )
    assert outcome.passed is False
    assert outcome.any_launch_failed is True
    assert outcome.any_quality_failed is False
    assert outcome.runs[0].launch_failed is True


def test_per_command_cwd(tmp_path: Path) -> None:
    fake = _FakeProc([_ok()])
    runner = CheckRunner(_config(), run_process=fake)
    runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        selected=(_set("be", _check("bt", ("dotnet", "test"), cwd="backend/src")),),
    )
    assert Path(fake.calls[0]["cwd"]) == tmp_path / "backend" / "src"


def test_per_set_timeout_overrides_global(tmp_path: Path) -> None:
    fake = _FakeProc([_ok(), _ok()])
    runner = CheckRunner(_config(timeout=1800), run_process=fake)
    selected = (
        _set("fast", _check("lint", ("ruff",))),  # inherits global 1800
        _set("slow", _check("ios", ("xcodebuild",)), timeout=2400),  # overrides
    )
    runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1", selected=selected)
    assert fake.calls[0]["timeout"] == 1800
    assert fake.calls[1]["timeout"] == 2400


def test_skip_if_unavailable_skips_when_binary_absent(tmp_path: Path) -> None:
    # Р4: opted-in set + absent binary → skipped (loud), never launched, never "passed".
    fake = _FakeProc([])  # no process is launched
    runner = CheckRunner(_config(), run_process=fake, which=_absent)
    outcome = runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        selected=(_set("ios", _check("it", ("xcodebuild", "test")), skip=True),),
    )
    assert fake.calls == []  # never launched
    assert outcome.runs[0].skipped is True
    assert outcome.runs[0].passed is False
    assert outcome.any_skipped is True
    assert outcome.nothing_ran is True  # every selected check was skipped → incomplete gate
    assert outcome.passed is False
    # The skip is recorded loudly in its own log.
    skip_log = Path(outcome.runs[0].log_path).read_text(encoding="utf-8")
    assert "skipped (toolchain absent)" in skip_log


def test_skip_if_unavailable_runs_when_binary_present(tmp_path: Path) -> None:
    fake = _FakeProc([_ok()])
    runner = CheckRunner(_config(), run_process=fake, which=_present)
    outcome = runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        selected=(_set("ios", _check("it", ("xcodebuild", "test")), skip=True),),
    )
    assert len(fake.calls) == 1  # binary present → the set runs
    assert outcome.passed is True
    assert outcome.any_skipped is False


def test_partial_skip_still_passes_but_flags_skip(tmp_path: Path) -> None:
    # backend present + ios skipped: the node can still pass, but any_skipped gates auto-merge.
    fake = _FakeProc([_ok()])
    which = lambda cmd: None if cmd == "xcodebuild" else f"/usr/bin/{cmd}"  # noqa: E731
    runner = CheckRunner(_config(), run_process=fake, which=which)
    selected = (
        _set("be", _check("bt", ("dotnet", "test")), skip=True),
        _set("ios", _check("it", ("xcodebuild", "test")), skip=True),
    )
    outcome = runner.run(
        clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1", selected=selected
    )
    assert len(fake.calls) == 1  # only backend ran
    assert outcome.any_skipped is True
    assert outcome.nothing_ran is False  # at least one check executed
    assert outcome.passed is True


def test_selected_none_uses_config_command_sets(tmp_path: Path) -> None:
    cfg = _config(
        command_sets=("    default:\n      commands:\n        - { name: tests, argv: [pytest] }\n")
    )
    fake = _FakeProc([_ok()])
    runner = CheckRunner(cfg, run_process=fake)
    outcome = runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1")  # selected=None
    assert fake.calls[0]["argv"] == ["pytest"]
    assert outcome.passed is True


def test_logs_not_overwritten_across_runs(tmp_path: Path) -> None:
    runner = CheckRunner(_config(), run_process=_FakeProc([_ok(), _ok()]))
    sel = (_set("a", _check("pytest", ("pytest",))),)
    runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1", selected=sel)
    runner.run(clone_dir=tmp_path, artifacts_root=tmp_path, task_id="t1", selected=sel)
    assert len(list((tmp_path / "logs" / "t1" / "checks").glob("*.log"))) == 2


def test_subtask_logs_are_prefixed(tmp_path: Path) -> None:
    runner = CheckRunner(_config(), run_process=_FakeProc([_ok()]))
    runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        subtask=2,
        selected=(_set("a", _check("pytest", ("pytest",))),),
    )
    logs = [p.name for p in (tmp_path / "logs" / "t1" / "checks").glob("*.log")]
    assert logs == ["sub-02-001.log"]


def test_stderr_redacted_in_log(tmp_path: Path) -> None:
    secret = "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    runner = CheckRunner(_config(), run_process=_FakeProc([_fail(stderr=secret)]))
    outcome = runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        selected=(_set("a", _check("pytest", ("pytest",))),),
    )
    log_text = Path(outcome.first_failure_log).read_text(encoding="utf-8")  # type: ignore[arg-type]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in log_text


def test_check_run_records_real_wall_clock_interval(tmp_path: Path) -> None:
    # VF-12: each check carries the wall-clock bracket around its subprocess, not two identical
    # row-write stamps — so a downstream check_runs row has a measurable duration.
    runner = CheckRunner(_config(), run_process=_FakeProc([_ok()]))
    ticks = iter([f"2026-07-25T00:00:{s:02d}+00:00" for s in range(60)])
    outcome = runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        selected=(_set("a", _check("pytest", ("pytest",))),),
        clock=lambda: next(ticks),
    )
    (run,) = outcome.runs
    assert run.started_at == "2026-07-25T00:00:00+00:00"
    assert run.finished_at == "2026-07-25T00:00:01+00:00"
    assert run.started_at < run.finished_at  # a real interval, never a zero-width stamp


def test_skipped_check_has_instant_interval(tmp_path: Path) -> None:
    # VF-12: a skipped check ran no subprocess, so its interval is a single honest instant.
    runner = CheckRunner(_config(), run_process=_FakeProc([]), which=_absent)
    outcome = runner.run(
        clone_dir=tmp_path,
        artifacts_root=tmp_path,
        task_id="t1",
        selected=(_set("a", _check("pytest", ("pytest",)), skip=True),),
        clock=lambda: "2026-07-25T00:00:00+00:00",
    )
    (run,) = outcome.runs
    assert run.skipped is True
    assert run.started_at == run.finished_at == "2026-07-25T00:00:00+00:00"
