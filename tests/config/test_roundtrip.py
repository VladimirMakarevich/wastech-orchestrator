"""Round-trip: the shipped config.example.yaml loads and validates clean."""

from __future__ import annotations

from wastech_orchestrator.config.loader import (
    _DEFAULT_DENIED_COMMANDS,
    loads_config,
)
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.security.env import default_allowed_environment


def test_example_denied_commands_match_loader_default(packaged_config_text: str) -> None:
    # The shipped list mirrors the mandatory baseline; merging deduplicates it exactly.
    cfg = loads_config(packaged_config_text).config
    assert cfg.security.denied_commands == _DEFAULT_DENIED_COMMANDS


def test_example_allowed_environment_covers_every_os_default(packaged_config_text: str) -> None:
    # allowed_environment REPLACES the default too, and the default is OS-aware, so an operator
    # copying the cross-platform example must not lose ANY OS's launch essentials (e.g. SystemRoot
    # on Windows, USER for macOS auth). The example is the union of every OS default; guards drift.
    cfg = loads_config(packaged_config_text).config
    every_os_default = (
        set(default_allowed_environment("Windows"))
        | set(default_allowed_environment("Linux"))
        | set(default_allowed_environment("Darwin"))
    )
    assert set(cfg.security.allowed_environment) == every_os_default


def test_packaged_example_loads_and_validates_clean(packaged_config_text: str) -> None:
    result = loads_config(packaged_config_text)
    assert result.warnings == ()  # the example has an explicit routing block — no migration
    assert validate_config(result.config) == []
