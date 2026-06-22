"""Operator-facing check diagnostics (automatic check discovery)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from wastech_orchestrator.checks import diagnostics
from wastech_orchestrator.checks.model import CheckSource, ResolvedCheck
from wastech_orchestrator.checks.profile import PROFILE_SCHEMA_VERSION, ResolvedCheckProfile
from wastech_orchestrator.checks.store import ResolvedCheckProfileStore
from wastech_orchestrator.config.schema import OrchestratorConfig


def test_check_preflight_ready_lists_resolved_commands(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    root = make_repo(
        {"pyproject.toml": "[project]\ndependencies=['pytest']\n"},
        venv=".venv",
        venv_tools=("pytest",),
    )
    config = make_checks_config(local_path=str(root), mode="deterministic")
    ready, lines = diagnostics.check_preflight(config, tmp_path / "art")
    assert ready is True
    assert any(line.startswith("checks: OK") for line in lines)
    assert any(".venv/bin/pytest" in line for line in lines)


def test_check_preflight_not_ready_reports_fail(
    make_repo: Callable[..., Path],
    make_checks_config: Callable[..., OrchestratorConfig],
    tmp_path: Path,
) -> None:
    # An empty repo under deterministic mode yields zero candidates -> not ready (PATH-independent).
    root = make_repo()
    config = make_checks_config(local_path=str(root), mode="deterministic")
    ready, lines = diagnostics.check_preflight(config, tmp_path / "art")
    assert ready is False
    assert any(line.startswith("checks: FAIL") for line in lines)


def test_load_and_summarize_profile(tmp_path: Path) -> None:
    profile = ResolvedCheckProfile(
        schema_version=PROFILE_SCHEMA_VERSION,
        ready=True,
        source=CheckSource.DETECTED,
        checks=(ResolvedCheck(name="tests", argv=(".venv/bin/pytest",)),),
        candidates=(),
        platform="linux",
        fingerprint="abcdef0123456789",
        created_at="t",
        last_validated_at="t",
    )
    ResolvedCheckProfileStore(tmp_path / "checks").save(profile)

    loaded = diagnostics.load_profile(tmp_path)
    assert loaded is not None
    summary = diagnostics.summarize_profile(loaded)
    assert any("checks_profile: source=detected" in line for line in summary)
    assert any("tests: .venv/bin/pytest" in line for line in summary)


def test_load_profile_missing_returns_none(tmp_path: Path) -> None:
    assert diagnostics.load_profile(tmp_path) is None
