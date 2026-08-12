"""logging.level: load_config_for applies the persisted level unless --log-level was passed.

Also covers the startup-capture handoff: a child whose raw stream is captured into an unrotated
startup log must not also log there through its terminal handler.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.observability import logging as obslog
from wastech_orchestrator.providers import process as agent_process


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


@pytest.fixture
def _reset_configured_logger() -> Iterator[None]:
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved, saved_level = pkg.handlers[:], pkg.level
    pkg.handlers.clear()
    obslog._configured = False
    yield
    for handler in pkg.handlers:
        handler.close()
    pkg.handlers.clear()
    pkg.handlers.extend(saved)
    pkg.setLevel(saved_level)
    obslog._configured = False


def _log_args(log_file: Path) -> argparse.Namespace:
    return argparse.Namespace(log_level=None, log_format="logfmt", log_file=str(log_file))


def test_startup_captured_child_logs_only_to_its_rotating_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _reset_configured_logger: None,
) -> None:
    # The whole chain that stops `daemon-startup.log` from growing: `spawn_detached` marks the
    # child, the child keeps its trace out of the captured stream, and the rotating file gets it.
    log_file = tmp_path / "daemon.log"
    monkeypatch.setenv(agent_process.STARTUP_CAPTURE_ENV, "1")
    cli._configure_runtime_logging(_log_args(log_file))
    handlers = logging.getLogger(obslog.LOGGER_NAME).handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], RotatingFileHandler)

    obslog.bind(logging.getLogger(obslog.LOGGER_NAME), task_id="t").info("serving the queue")
    handlers[0].flush()
    captured = capsys.readouterr()
    assert captured.err == ""  # nothing lands in the captured stream (the startup log)
    assert captured.out == ""
    assert "serving the queue" in log_file.read_text(encoding="utf-8")


def test_ordinary_child_keeps_both_sinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _reset_configured_logger: None
) -> None:
    monkeypatch.delenv(agent_process.STARTUP_CAPTURE_ENV, raising=False)
    cli._configure_runtime_logging(_log_args(tmp_path / "daemon.log"))
    assert len(logging.getLogger(obslog.LOGGER_NAME).handlers) == 2


@pytest.mark.slow
def test_pre_configuration_crash_still_reaches_the_startup_log(tmp_path: Path) -> None:
    """A crash before ``configure_logging`` runs is exactly what the startup log exists for.

    Dropping the terminal handler must not close that window: the child's raw stderr is still
    redirected there, so an argparse error / import failure / preflight abort is still recoverable.
    """
    startup_log = tmp_path / "logs" / "daemon-startup.log"
    proc = agent_process.spawn_detached(
        [sys.executable, "-c", "import sys; sys.stderr.write('argv is wrong\\n'); sys.exit(2)"],
        capture_path=startup_log,
    )
    assert proc.wait(timeout=30) == 2
    assert "argv is wrong" in startup_log.read_text(encoding="utf-8", errors="replace")
