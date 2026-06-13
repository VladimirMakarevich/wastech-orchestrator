"""Loader: fail-closed structural parsing and the legacy-config migration (spec §11)."""

from __future__ import annotations

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import FootprintLocation, FootprintTracking
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


def test_poll_interval_defaults_to_300() -> None:
    result = loads_config(_LEGACY)
    assert result.config.orchestrator.poll_interval_seconds == 300


def test_footprint_defaults_to_in_repo_commit() -> None:
    # The operating default keeps tasks + artifacts in the modified repo, audit-committed (§21).
    result = loads_config(_LEGACY)
    assert result.config.git.footprint.location is FootprintLocation.IN_REPO
    assert result.config.git.footprint.tracking is FootprintTracking.COMMIT


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
    assert result.config.checks.timeout_seconds == 7200


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


_WITH_CLAUDE = _LEGACY + """
agents:
  allowed:
    - claude
  providers:
    claude:
      command: "claude"
"""

_PROVIDER_BASE = """
agents:
  allowed:
    - claude
    - codex
  providers:
    claude:
      command: "claude"
    codex:
      command: "codex"
"""


def test_reasoning_absent_defaults_to_none() -> None:
    result = loads_config(_PROVIDER_BASE)
    from wastech_orchestrator.providers.base import ProviderId
    assert result.config.agents.providers[ProviderId.CLAUDE].reasoning is None
    assert result.config.agents.providers[ProviderId.CODEX].reasoning is None


def test_reasoning_valid_levels_parse() -> None:
    for level in ("low", "medium", "high", "xhigh", "max"):
        text2 = _PROVIDER_BASE.replace(
            "    claude:\n      command: \"claude\"",
            f"    claude:\n      command: \"claude\"\n      reasoning: {level}",
        )
        result = loads_config(text2)
        from wastech_orchestrator.providers.base import ProviderId
        assert result.config.agents.providers[ProviderId.CLAUDE].reasoning == level


def test_reasoning_invalid_value_is_rejected() -> None:
    text = _PROVIDER_BASE.replace(
        "    claude:\n      command: \"claude\"",
        "    claude:\n      command: \"claude\"\n      reasoning: ultra",
    )
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("reasoning" in issue for issue in exc.value.issues)
    assert any("ultra" in issue for issue in exc.value.issues)


def test_reasoning_null_parses_to_none() -> None:
    text = _PROVIDER_BASE.replace(
        "    claude:\n      command: \"claude\"",
        "    claude:\n      command: \"claude\"\n      reasoning: null",
    )
    result = loads_config(text)
    from wastech_orchestrator.providers.base import ProviderId
    assert result.config.agents.providers[ProviderId.CLAUDE].reasoning is None
