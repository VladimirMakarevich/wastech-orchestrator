"""Config `schema_version` gate: a newer version is refused; current/absent/packaged load clean."""

from __future__ import annotations

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import CONFIG_SCHEMA_VERSION

# Minimal structurally-valid config body (routing absent → legacy migration: a warning, not error).
_BODY = (
    "repo:\n  url: x\nagents:\n  allowed: [codex]\n  providers:\n    codex:\n      command: codex\n"
)


def test_newer_schema_version_is_refused() -> None:
    text = f"schema_version: {CONFIG_SCHEMA_VERSION + 1}\n{_BODY}"
    with pytest.raises(ConfigError, match="newer than this orchestrator supports"):
        loads_config(text)


def test_current_and_absent_schema_version_load() -> None:
    loads_config(f"schema_version: {CONFIG_SCHEMA_VERSION}\n{_BODY}")  # explicit current
    loads_config(_BODY)  # absent is accepted


def test_non_integer_schema_version_is_rejected() -> None:
    with pytest.raises(ConfigError, match="schema_version: expected an integer"):
        loads_config(f"schema_version: 'x'\n{_BODY}")


def test_packaged_example_declares_and_loads_schema_version(packaged_config_text: str) -> None:
    assert (
        f"schema_version: {CONFIG_SCHEMA_VERSION}" in packaged_config_text
    )  # declared == constant
    loads_config(packaged_config_text)  # loads clean with the version present
