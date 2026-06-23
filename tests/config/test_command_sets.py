"""Config tests for ``checks.command_sets`` (v15): loading + semantic validation."""

from __future__ import annotations

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.validation import validate_config

# A minimal but fully valid base config (one primary provider); a checks block is appended per test.
_BASE = """
repo:
  url: "git@example.com:o/r.git"
agents:
  allowed: [codex]
  providers:
    codex:
      command: "codex"
      primary: true
"""


def _issues(checks_yaml: str) -> list[str]:
    cfg = loads_config(_BASE + checks_yaml).config
    try:
        validate_config(cfg)
        return []
    except ConfigError as exc:
        return list(exc.issues)


def test_valid_monorepo_loads_and_validates() -> None:
    checks = """
checks:
  command_sets:
    backend:
      paths: ["backend/**"]
      commands:
        - { name: bt, argv: [dotnet, test], cwd: backend/src }
    ios:
      paths: ["ios/**"]
      timeout_seconds: 2400
      skip_if_unavailable: true
      commands:
        - { name: it, argv: [xcodebuild, test], cwd: ios }
"""
    cfg = loads_config(_BASE + checks).config
    sets = cfg.checks.command_sets
    assert set(sets) == {"backend", "ios"}
    assert sets["backend"].commands[0].cwd == "backend/src"
    assert sets["ios"].timeout_seconds == 2400
    assert sets["ios"].skip_if_unavailable is True
    assert _issues(checks) == []


def test_empty_command_sets_is_no_gate() -> None:
    assert _issues("checks:\n  command_sets: {}\n") == []


def test_cwd_traversal_rejected() -> None:
    issues = _issues(
        "checks:\n  command_sets:\n    a:\n      commands:\n"
        "        - { name: x, argv: [pytest], cwd: ../escape }\n"
    )
    assert any("cwd" in i and "repo-relative" in i for i in issues)


def test_denied_command_rejected() -> None:
    issues = _issues(
        "checks:\n  command_sets:\n    a:\n      commands:\n"
        "        - { name: x, argv: [git, commit] }\n"
    )
    assert any("denied command" in i for i in issues)


def test_shell_metachar_rejected() -> None:
    issues = _issues(
        "checks:\n  command_sets:\n    a:\n      commands:\n"
        '        - { name: x, argv: [sh, "-c", "a; b"] }\n'
    )
    assert any("shell metacharacter" in i for i in issues)


def test_empty_commands_rejected() -> None:
    issues = _issues(
        'checks:\n  command_sets:\n    a:\n      paths: ["a/**"]\n      commands: []\n'
    )
    assert any("at least one command" in i for i in issues)


def test_per_set_timeout_must_be_positive() -> None:
    issues = _issues(
        "checks:\n  command_sets:\n    a:\n      timeout_seconds: 0\n      commands:\n"
        "        - { name: x, argv: [pytest] }\n"
    )
    assert any("timeout_seconds must be > 0" in i for i in issues)


def test_empty_set_name_rejected() -> None:
    issues = _issues(
        'checks:\n  command_sets:\n    "":\n      commands:\n'
        "        - { name: x, argv: [pytest] }\n"
    )
    assert any("non-empty string" in i for i in issues)


def test_stale_discovery_and_commands_keys_are_tolerated() -> None:
    # v15 removed both; an old config still loads fail-open (the keys are ignored, not rejected).
    cfg = loads_config(
        _BASE + 'checks:\n  discovery: { mode: auto }\n  commands: ["pytest"]\n  command_sets: {}\n'
    ).config
    assert cfg.checks.command_sets == {}
