"""Config: the structured-command union and the checks.discovery block (check discovery)."""

from __future__ import annotations

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import (
    CheckCommandSpec,
    CheckDiscoveryMode,
    CheckRefreshPolicy,
)
from wastech_orchestrator.config.validation import validate_config

# codex is the lone allowed provider and the global primary, so validate_config passes on routing
# (leaving only the checks issues we assert on).
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


def _load(checks_block: str):
    return loads_config(_BASE + checks_block).config


def test_legacy_string_commands_still_load() -> None:
    config = _load("checks:\n  commands:\n    - 'pytest'\n    - 'ruff check .'\n")
    assert config.checks.commands == ("pytest", "ruff check .")


def test_structured_commands_load() -> None:
    config = _load(
        "checks:\n  commands:\n    - {name: tests, argv: ['.venv/bin/python', '-m', 'pytest']}\n"
    )
    spec = config.checks.commands[0]
    assert isinstance(spec, CheckCommandSpec)
    assert spec.name == "tests"
    assert spec.argv == (".venv/bin/python", "-m", "pytest")


def test_mixed_union_commands_load() -> None:
    config = _load("checks:\n  commands:\n    - 'pytest'\n    - {argv: ['ruff', 'check', '.']}\n")
    assert config.checks.commands[0] == "pytest"
    assert isinstance(config.checks.commands[1], CheckCommandSpec)


def test_structured_command_requires_argv() -> None:
    with pytest.raises(ConfigError) as exc:
        _load("checks:\n  commands:\n    - {name: tests}\n")
    assert any("argv" in issue for issue in exc.value.issues)


def test_discovery_defaults() -> None:
    config = _load("checks:\n  commands: []\n")
    discovery = config.checks.discovery
    assert discovery.mode is CheckDiscoveryMode.CONFIGURED
    assert discovery.agent_fallback is True
    assert discovery.refresh is CheckRefreshPolicy.ON_CHANGE
    assert discovery.reasoning == "low"
    assert discovery.timeout_seconds == 120


def test_discovery_block_parses() -> None:
    config = _load(
        "checks:\n"
        "  discovery:\n"
        "    mode: auto\n"
        "    agent_fallback: false\n"
        "    refresh: always\n"
        "    provider: claude\n"
        "    model: claude-haiku-4-5-20251001\n"
        "    reasoning: medium\n"
        "    timeout_seconds: 90\n"
    )
    discovery = config.checks.discovery
    assert discovery.mode is CheckDiscoveryMode.AUTO
    assert discovery.agent_fallback is False
    assert discovery.refresh is CheckRefreshPolicy.ALWAYS
    assert discovery.model == "claude-haiku-4-5-20251001"
    assert discovery.reasoning == "medium"
    assert discovery.timeout_seconds == 90


def test_invalid_discovery_mode_rejected() -> None:
    with pytest.raises(ConfigError):
        _load("checks:\n  discovery:\n    mode: bogus\n")


def test_shell_metacharacter_command_rejected_by_validator() -> None:
    config = _load("checks:\n  commands:\n    - 'pytest; rm -rf /'\n")
    with pytest.raises(ConfigError) as exc:
        validate_config(config)
    assert any("metacharacter" in issue for issue in exc.value.issues)


def test_denied_command_rejected_by_validator() -> None:
    config = _load("checks:\n  commands:\n    - {argv: ['git', 'push', 'origin']}\n")
    with pytest.raises(ConfigError) as exc:
        validate_config(config)
    assert any("denied" in issue for issue in exc.value.issues)


def test_discovery_provider_must_be_allowed() -> None:
    # claude is not in agents.allowed (only codex is), so it cannot run discovery.
    config = _load("checks:\n  discovery:\n    provider: claude\n")
    with pytest.raises(ConfigError) as exc:
        validate_config(config)
    assert any("discovery.provider" in issue for issue in exc.value.issues)


def test_disabled_mode_warns_not_errors() -> None:
    config = _load("checks:\n  discovery:\n    mode: disabled\n")
    warnings = validate_config(config)
    assert any("disabled" in w for w in warnings)
