"""Round-trip: the shipped config.example.yaml loads and validates clean, and is in sync."""

from __future__ import annotations

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.validation import validate_config


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
