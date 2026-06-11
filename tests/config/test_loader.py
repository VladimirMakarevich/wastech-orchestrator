"""Loader: fail-closed structural parsing and the legacy-config migration (spec §11)."""

from __future__ import annotations

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.providers.base import ProviderId, Stage

_LEGACY = """
repo:
  url: "git@example.com:o/r.git"
agents:
  allowed:
    - codex
  providers:
    codex:
      command: "codex"
"""


def test_non_mapping_root_is_rejected() -> None:
    with pytest.raises(ConfigError):
        loads_config("- a\n- b\n")


def test_empty_root_is_rejected() -> None:
    with pytest.raises(ConfigError):
        loads_config("")


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config("nonsense: 1\n")
    assert any("nonsense" in issue for issue in exc.value.issues)


def test_auto_mode_defaults_to_false() -> None:
    result = loads_config(_LEGACY)
    assert result.config.orchestrator.auto_mode.enabled is False


def test_auto_mode_enabled_loads_true() -> None:
    text = """
orchestrator:
  auto_mode:
    enabled: true
repo:
  url: "git@example.com:o/r.git"
agents:
  allowed:
    - codex
  providers:
    codex:
      command: "codex"
"""
    result = loads_config(text)
    assert result.config.orchestrator.auto_mode.enabled is True


def test_auto_mode_enabled_must_be_boolean() -> None:
    text = """
orchestrator:
  auto_mode:
    enabled: "yes"
"""
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("orchestrator.auto_mode.enabled" in issue for issue in exc.value.issues)


def test_unknown_orchestrator_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config("orchestrator:\n  unexpected: true\n")
    assert any("unexpected" in issue for issue in exc.value.issues)


def test_checks_timeout_defaults_to_1800() -> None:
    result = loads_config(_LEGACY)
    assert result.config.checks.timeout_seconds == 1800


def test_checks_timeout_loads() -> None:
    text = _LEGACY + "checks:\n  commands: ['pytest']\n  timeout_seconds: 60\n"
    result = loads_config(text)
    assert result.config.checks.commands == ("pytest",)
    assert result.config.checks.timeout_seconds == 60


def test_checks_timeout_must_be_integer() -> None:
    text = _LEGACY + "checks:\n  timeout_seconds: fast\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("checks.timeout_seconds" in issue for issue in exc.value.issues)


def test_unknown_checks_key_is_rejected() -> None:
    text = _LEGACY + "checks:\n  retries: 3\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("retries" in issue for issue in exc.value.issues)


def test_unknown_route_key_is_rejected() -> None:
    text = """
agents:
  routing:
    deployment:
      primary: codex
"""
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("deployment" in issue for issue in exc.value.issues)


def test_bad_enum_value_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config("git:\n  footprint:\n    location: weird\n")
    assert any("location" in issue for issue in exc.value.issues)


def test_unknown_provider_in_routing_is_rejected() -> None:
    text = """
agents:
  routing:
    planning:
      primary: gpt
"""
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("gpt" in issue for issue in exc.value.issues)


def test_all_issues_collected_not_just_first() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config("foo: 1\nbar: 2\n")
    assert len(exc.value.issues) >= 2


def test_legacy_codex_only_config_migrates_with_warning() -> None:
    result = loads_config(_LEGACY)
    assert any("migrat" in w.lower() for w in result.warnings)
    # Every agent-routed stage now has a Codex primary; the migrated config validates clean.
    assert result.config.agents.routing[Stage.PLANNING].primary is ProviderId.CODEX
    assert result.config.agents.routing[Stage.REVIEW].primary is ProviderId.CODEX
    assert validate_config(result.config) == []
