"""Candidate validation rules (automatic check discovery §7, §12)."""

from __future__ import annotations

from wastech_orchestrator.checks.model import CheckCandidate, CheckSource
from wastech_orchestrator.checks.validate import CheckCandidateValidator


def _candidate(*argv: str) -> CheckCandidate:
    return CheckCandidate(name="x", argv=tuple(argv), source=CheckSource.AGENT)


def test_accepts_a_plain_argv() -> None:
    result = CheckCandidateValidator().validate(_candidate("pytest", "-q"))
    assert result.rejection is None
    assert result.candidate is not None


def test_rejects_shell_metacharacters() -> None:
    result = CheckCandidateValidator().validate(_candidate("pytest", ";", "rm", "-rf", "/"))
    assert result.candidate is None
    assert "metacharacter" in (result.rejection or "")


def test_rejects_sandbox_weakening_flag() -> None:
    result = CheckCandidateValidator().validate(
        _candidate("claude", "--dangerously-skip-permissions")
    )
    assert result.candidate is None


def test_rejects_denied_command() -> None:
    validator = CheckCandidateValidator(denied_commands=("git push",))
    result = validator.validate(_candidate("git", "push", "origin", "main"))
    assert result.candidate is None
    assert "denied" in (result.rejection or "")


def test_rejects_install_command_as_check() -> None:
    result = CheckCandidateValidator().validate(_candidate("uv", "sync"))
    assert result.candidate is None
    assert "not a check" in (result.rejection or "")


def test_rejects_npm_ci() -> None:
    result = CheckCandidateValidator().validate(_candidate("npm", "ci"))
    assert result.candidate is None
