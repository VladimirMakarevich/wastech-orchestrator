"""The loader's non-fatal warnings channel reaches the operator on every command.

``ConfigLoadResult.warnings`` existed but had no producer and no consumer, so the memory quench —
whose whole mitigation is that the operator SEES it — would have been invisible. These tests pin the
two seams that make it visible: ``_load_config`` (which every command reaches) and
``upgrade-config`` (the command that rewrites the file).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from wastech_orchestrator import cli
from wastech_orchestrator.config.upgrade import packaged_template_mapping

_QUENCH_TEXT = "turns memory off for this run"


def _config_with(tmp_path: Path, *, supervisor_enabled: bool, memory_enabled: bool) -> Path:
    mapping = packaged_template_mapping()
    mapping["repo"]["local_path"] = str(tmp_path / "repo")
    mapping["supervisor"]["enabled"] = supervisor_enabled
    mapping["memory"]["enabled"] = memory_enabled
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
    return path


def _warnings_from_load(path: Path) -> list[str]:
    """Every WARNING ``_load_config`` emits.

    Collected with an explicit handler rather than ``caplog``: the package logger's propagation is
    reconfigured by other tests, so a fixture that depends on it passes alone and fails in a full
    run.
    """
    messages: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger = logging.getLogger("wastech_orchestrator")
    handler = _Collect(level=logging.WARNING)
    logger.addHandler(handler)
    prior = logger.level
    logger.setLevel(logging.WARNING)
    try:
        cli._load_config(str(path))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior)
    return messages


def test_load_config_logs_the_loader_warning(tmp_path: Path) -> None:
    path = _config_with(tmp_path, supervisor_enabled=False, memory_enabled=True)
    messages = _warnings_from_load(path)
    assert any(_QUENCH_TEXT in message for message in messages)
    # And the resolved config really is quenched, not merely warned about.
    assert cli._load_config(str(path)).memory.enabled is False


def test_load_config_is_quiet_on_a_config_without_the_conflict(tmp_path: Path) -> None:
    path = _config_with(tmp_path, supervisor_enabled=True, memory_enabled=True)
    assert not [m for m in _warnings_from_load(path) if _QUENCH_TEXT in m]


def test_upgrade_config_prints_the_warning_on_a_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _config_with(tmp_path, supervisor_enabled=False, memory_enabled=True)
    assert cli.main(["--config", str(path), "upgrade-config", "--dry-run"]) == 0
    assert _QUENCH_TEXT in capsys.readouterr().out


def test_upgrade_config_prints_the_warning_even_when_already_up_to_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The sharp case: this command early-returns "already up to date" for any config whose merge
    # is a no-op — the steady state of a long-lived config — so a warning printed after that
    # return would never reach the operators who most need it. It comes from the probe instead.
    path = _config_with(tmp_path, supervisor_enabled=False, memory_enabled=True)
    assert cli.main(["--config", str(path), "upgrade-config"]) == 0
    first = capsys.readouterr().out
    assert _QUENCH_TEXT in first

    assert cli.main(["--config", str(path), "upgrade-config"]) == 0
    second = capsys.readouterr().out
    assert "already up to date" in second
    assert _QUENCH_TEXT in second
