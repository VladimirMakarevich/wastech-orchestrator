"""Shared fixtures for task-layer tests."""

from __future__ import annotations

import pytest

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig


@pytest.fixture
def config(packaged_config_text: str) -> OrchestratorConfig:
    """The packaged example config parsed into the typed schema (thresholds, routing)."""
    return loads_config(packaged_config_text).config
