"""Config discovery DoD: resolution order is explicit ``--config`` > the
``.worc/config.yaml`` discovered by walking up to the Git root > a "run install" hint."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.observability import logging as obslog


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
    args = argparse.Namespace(config="/explicit/elsewhere.yaml")
    assert cli.resolve_config_path(args) == "/explicit/elsewhere.yaml"


def test_worc_config_resolved_from_nested_subdir(
    monkeypatch: pytest.MonkeyPatch, git_repo: Any
) -> None:
    config_path = git_repo.clone / ".worc" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("x\n", encoding="utf-8")
    nested = git_repo.clone / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)  # walks up to the Git root, then finds .worc/config.yaml
    expected = git_repo.clone.resolve() / ".worc" / "config.yaml"  # git_info().root is resolved
    assert cli.resolve_config_path(argparse.Namespace(config=None)) == str(expected)


def test_repo_without_worc_config_returns_none(
    monkeypatch: pytest.MonkeyPatch, git_repo: Any
) -> None:
    monkeypatch.chdir(git_repo.clone)  # a Git repo, but no .worc/config.yaml yet
    assert cli.resolve_config_path(argparse.Namespace(config=None)) is None


def test_unconfigured_returns_none_and_hints_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # not a git repo, no .worc/config.yaml
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
