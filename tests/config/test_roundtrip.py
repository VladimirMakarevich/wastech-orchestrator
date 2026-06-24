"""Round-trip: the shipped config.example.yaml loads and validates clean, and is in sync."""

from __future__ import annotations

from wastech_orchestrator.config.loader import (
    _DEFAULT_ALLOWED_ENV,
    _DEFAULT_DENIED_COMMANDS,
    loads_config,
)
from wastech_orchestrator.config.validation import validate_config


def test_example_denied_commands_match_loader_default(packaged_config_text: str) -> None:
    # denied_commands REPLACES (does not extend) the default, so an operator copying the example
    # must not silently lose a default denial (e.g. ``gh pr merge``). Guards drift in either copy.
    cfg = loads_config(packaged_config_text).config
    assert cfg.security.denied_commands == _DEFAULT_DENIED_COMMANDS


def test_example_allowed_environment_matches_loader_default(packaged_config_text: str) -> None:
    # allowed_environment REPLACES the default too, so an operator copying the example must not
    # lose a default key (e.g. ``USER``, needed for macOS auth). Guards drift in either copy.
    cfg = loads_config(packaged_config_text).config
    assert cfg.security.allowed_environment == _DEFAULT_ALLOWED_ENV


def test_packaged_example_loads_and_validates_clean(packaged_config_text: str) -> None:
    result = loads_config(packaged_config_text)
    assert result.warnings == ()  # the example has an explicit routing block — no migration
    assert validate_config(result.config) == []


def test_repo_root_example_in_sync_with_packaged(
    packaged_config_text: str, repo_root_config_text: str
) -> None:
    # The two copies carry slightly different header comments (one notes it is the packaged copy),
    # but must describe the same configuration. Compare the parsed configs, not raw bytes.
    assert loads_config(repo_root_config_text).config == loads_config(packaged_config_text).config
