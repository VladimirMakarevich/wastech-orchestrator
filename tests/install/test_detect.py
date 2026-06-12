"""Detection DoD: git root/origin/branch/cleanliness, CLI discovery on PATH, and ecosystem-based
check auto-detection (backlog: interactive installer)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator.install import detect
from wastech_orchestrator.providers.base import ProviderId

GitRunner = Callable[[list[str], Path], Any]


def test_git_info_reads_root_origin_and_branch(git_repo: Any) -> None:
    info = detect.git_info(git_repo.clone)
    assert info is not None
    assert info.root == git_repo.clone.resolve()
    assert info.origin_url == str(git_repo.remote)
    assert info.current_branch == "main"
    assert info.default_branch == "main"
    assert info.is_clean is True


def test_git_info_detects_dirty_tree(git_repo: Any) -> None:
    (git_repo.clone / "README.md").write_text("# changed\n", encoding="utf-8")
    info = detect.git_info(git_repo.clone)
    assert info is not None
    assert info.is_clean is False


def test_git_info_from_nested_subdir_resolves_root(git_repo: Any) -> None:
    nested = git_repo.clone / "src" / "deep"
    nested.mkdir(parents=True)
    info = detect.git_info(nested)
    assert info is not None
    assert info.root == git_repo.clone.resolve()


def test_git_info_without_origin(tmp_path: Path, git_run: GitRunner) -> None:
    repo = tmp_path / "norigin"
    repo.mkdir()
    git_run(["init", "-b", "main", "."], repo)
    git_run(["config", "user.email", "t@example.com"], repo)
    git_run(["config", "user.name", "T"], repo)
    git_run(["config", "commit.gpgsign", "false"], repo)
    git_run(["commit", "--allow-empty", "-m", "init"], repo)
    info = detect.git_info(repo)
    assert info is not None
    assert info.origin_url is None


def test_git_info_outside_repo_is_none(tmp_path: Path) -> None:
    assert detect.git_info(tmp_path) is None


def test_detect_providers_and_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    present = {"codex": "/usr/bin/codex", "gh": "/usr/bin/gh"}
    monkeypatch.setattr("shutil.which", lambda name: present.get(name))
    providers = detect.detect_providers()
    assert providers[ProviderId.CODEX] == "/usr/bin/codex"
    assert providers[ProviderId.CLAUDE] is None
    assert detect.has_gh() is True


def test_has_gh_false_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert detect.has_gh() is False


@pytest.mark.parametrize(
    ("marker", "content", "expected"),
    [
        ("pyproject.toml", "[project]\n", ["pytest"]),
        ("Cargo.toml", "[package]\n", ["cargo test"]),
        ("go.mod", "module x\n", ["go test ./..."]),
    ],
)
def test_detect_checks_by_marker(
    tmp_path: Path, marker: str, content: str, expected: list[str]
) -> None:
    (tmp_path / marker).write_text(content, encoding="utf-8")
    assert detect.detect_checks(tmp_path) == expected


def test_detect_checks_node_uses_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "jest", "lint": "eslint ."}}', encoding="utf-8"
    )
    assert detect.detect_checks(tmp_path) == ["npm test", "npm run lint"]


def test_detect_checks_node_without_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    assert detect.detect_checks(tmp_path) == []


def test_detect_checks_empty_when_no_markers(tmp_path: Path) -> None:
    assert detect.detect_checks(tmp_path) == []
