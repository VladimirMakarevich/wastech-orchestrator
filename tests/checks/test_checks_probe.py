"""Launchability probing (automatic check discovery)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.checks.conftest import make_process_result
from wastech_orchestrator.checks.model import CheckCandidate, CheckSource, ProbeStatus
from wastech_orchestrator.checks.probe import CheckProbeRunner


def _candidate(*argv: str, status: ProbeStatus | None = None) -> CheckCandidate:
    return CheckCandidate(
        name="tests", argv=tuple(argv), source=CheckSource.DETECTED, probe_status=status
    )


def test_path_present_is_launchable(make_repo: Callable[..., Path]) -> None:
    root = make_repo(venv=".venv", venv_tools=("pytest",))
    prober = CheckProbeRunner(repo_root=root, allowed_environment=("PATH",))
    probed = prober.probe(_candidate(".venv/bin/pytest"))
    assert probed.probe_status is ProbeStatus.LAUNCHABLE


def test_missing_path_is_not_launchable(make_repo: Callable[..., Path]) -> None:
    root = make_repo()
    prober = CheckProbeRunner(repo_root=root, allowed_environment=("PATH",))
    probed = prober.probe(_candidate(".venv/bin/pytest"))
    assert probed.probe_status is ProbeStatus.NOT_LAUNCHABLE


def test_bare_command_uses_which(make_repo: Callable[..., Path]) -> None:
    root = make_repo()
    found = CheckProbeRunner(
        repo_root=root, allowed_environment=("PATH",), which=lambda _n: "/usr/bin/x"
    )
    missing = CheckProbeRunner(repo_root=root, allowed_environment=("PATH",), which=lambda _n: None)
    assert found.probe(_candidate("pytest")).probe_status is ProbeStatus.LAUNCHABLE
    assert missing.probe(_candidate("pytest")).probe_status is ProbeStatus.NOT_LAUNCHABLE


def test_python_dash_m_import_check_launchable(make_repo: Callable[..., Path]) -> None:
    root = make_repo(venv=".venv")
    prober = CheckProbeRunner(
        repo_root=root,
        allowed_environment=("PATH",),
        run_process=lambda *a, **k: make_process_result(exit_code=0),
    )
    probed = prober.probe(_candidate(".venv/bin/python", "-m", "pytest"))
    assert probed.probe_status is ProbeStatus.LAUNCHABLE


def test_python_dash_m_import_failure_not_launchable(make_repo: Callable[..., Path]) -> None:
    root = make_repo(venv=".venv")
    prober = CheckProbeRunner(
        repo_root=root,
        allowed_environment=("PATH",),
        run_process=lambda *a, **k: make_process_result(exit_code=1),
    )
    probed = prober.probe(_candidate(".venv/bin/python", "-m", "pytest"))
    assert probed.probe_status is ProbeStatus.NOT_LAUNCHABLE
