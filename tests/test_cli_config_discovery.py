"""Config discovery DoD (backlog: interactive installer): resolution order is explicit
``--config`` > ``./config.yaml`` > registry binding (from any subdir) > a "run install" hint."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.install import registry
from wastech_orchestrator.observability import logging as obslog


@pytest.fixture(autouse=True)
def _registry_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(registry.HOME_ENV, str(tmp_path / "registry-home"))


@pytest.fixture
def _reset_package_logger() -> Iterator[None]:
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved = pkg.handlers[:]
    pkg.handlers.clear()
    obslog._configured = False
    yield
    pkg.handlers.clear()
    pkg.handlers.extend(saved)
    obslog._configured = False


def test_explicit_config_wins_over_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("ignored\n", encoding="utf-8")  # would be branch 2
    args = argparse.Namespace(config="/explicit/elsewhere.yaml")
    assert cli.resolve_config_path(args) == "/explicit/elsewhere.yaml"


def test_local_config_yaml_is_the_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("x\n", encoding="utf-8")
    assert cli.resolve_config_path(argparse.Namespace(config=None)) == "config.yaml"


def test_registry_binding_resolved_from_nested_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_repo: Any
) -> None:
    config_path = tmp_path / "workspace" / "config.yaml"
    registry.bind(git_repo.clone, config_path)
    nested = git_repo.clone / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)  # no ./config.yaml here; must fall through to the binding
    assert cli.resolve_config_path(argparse.Namespace(config=None)) == str(config_path.resolve())


def test_unconfigured_returns_none_and_hints_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # not a git repo, no ./config.yaml, no binding
    args = argparse.Namespace(config=None)
    assert cli.resolve_config_path(args) is None
    assert cli.load_config_for(args) is None
    assert "install ." in capsys.readouterr().out


def test_command_exits_2_when_unconfigured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _reset_package_logger: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["status"]) == 2
    assert "install ." in capsys.readouterr().out
