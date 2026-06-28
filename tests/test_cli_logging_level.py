"""logging.level: load_config_for applies the persisted level unless --log-level was passed."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.observability import logging as obslog


def _config(level: str) -> OrchestratorConfig:
    text = (
        'repo:\n  url: "git@example.com:o/r.git"\n'
        "agents:\n  allowed: [codex]\n  providers:\n    codex:\n      command: codex\n"
        f"logging:\n  level: {level}\n"
    )
    return loads_config(text).config


@pytest.fixture
def _restore_logger_level() -> Iterator[None]:
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved = pkg.level
    yield
    pkg.setLevel(saved)


def test_persisted_level_applied_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch, _restore_logger_level: None
) -> None:
    monkeypatch.setattr(cli, "_load_config", lambda _path: _config("warning"))
    logging.getLogger(obslog.LOGGER_NAME).setLevel(logging.INFO)
    args = argparse.Namespace(config="/dummy/config.yaml", log_level=None)
    assert cli.load_config_for(args) is not None
    assert logging.getLogger(obslog.LOGGER_NAME).level == logging.WARNING


def test_flag_wins_over_persisted_level(
    monkeypatch: pytest.MonkeyPatch, _restore_logger_level: None
) -> None:
    monkeypatch.setattr(cli, "_load_config", lambda _path: _config("warning"))
    # Simulate what _configure_runtime_logging already applied from the explicit flag.
    logging.getLogger(obslog.LOGGER_NAME).setLevel(logging.DEBUG)
    args = argparse.Namespace(config="/dummy/config.yaml", log_level="debug")
    cli.load_config_for(args)
    # The flag wins: load_config_for must not override it with the config's warning.
    assert logging.getLogger(obslog.LOGGER_NAME).level == logging.DEBUG
