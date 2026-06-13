"""Shared fixtures for the test suite."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import pytest

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
_FAKE_AGENT = Path(__file__).resolve().parent / "fakes" / "fake_agent.py"

# A broad-but-explicit env allowlist so git runs under the orchestrator's allowlisted environment on
# both POSIX and Windows CI (git may need SYSTEMROOT/TEMP on Windows). Production tunes this per-OS.
_TEST_ALLOWED_ENV = [
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "GIT_EXEC_PATH",
]


@pytest.fixture
def packaged_config_text() -> str:
    """The packaged config.example.yaml shipped as package data (used by ``init``)."""
    return (
        resources.files("wastech_orchestrator")
        .joinpath("templates", "config.example.yaml")
        .read_text(encoding="utf-8")
    )


@pytest.fixture
def repo_root_config_text() -> str:
    """The repo-root config.example.yaml (must stay in sync with the packaged copy)."""
    return (REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8")


@pytest.fixture
def fake_cli(tmp_path: Path) -> Callable[..., str]:
    """Build a runnable fake CLI launcher (named like the real binary) for a given scenario.

    Returns the launcher path to use as ``ProviderConfig.command``. The scenario is embedded in the
    launcher (not the env, which the adapter's allowlist would strip). Cross-platform: a ``.cmd``
    wrapper on Windows, a shebang shell script on POSIX. Both invoke ``fakes/fake_agent.py`` and
    forward the adapter's real argv. Parametrize ``cli_name`` (``codex``/``claude``) to reuse the
    same scenario matrix across adapters.
    """

    def _make(scenario: str, cli_name: str = "codex") -> str:
        bin_dir = tmp_path / f"fakebin-{cli_name}-{scenario}"
        bin_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            launcher = bin_dir / f"{cli_name}.cmd"
            launcher.write_text(
                f'@"{sys.executable}" "{_FAKE_AGENT}" {cli_name} {scenario} %*\r\n',
                encoding="utf-8",
            )
        else:
            launcher = bin_dir / cli_name
            launcher.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE_AGENT}" {cli_name} {scenario} "$@"\n',
                encoding="utf-8",
            )
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return str(launcher)

    return _make


def run_git(args: Sequence[str], cwd: Path) -> str:
    """Run a git command for test setup/inspection (real git, not the manager); return stdout."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class GitRepo:
    clone: Path
    remote: Path


def build_git_config(
    clone: Path,
    *,
    location: str = "external",
    tracking: str = "none",
    create_pr: bool = True,
    audit_on_branch: str = "task",
    checks: Sequence[str] = (),
    decomposition: bool = False,
    max_subtasks: int = 8,
    max_fix_cycles: int = 3,
    max_total_fix_iterations: int = 5,
    quarantine: str | None = None,
    skip_stages: Sequence[str] = (),
    allow_review_skip: bool = False,
    auto_mode: bool = False,
    auto_merge: bool = False,
    auto_merge_strategy: str = "squash",
    auto_merge_allow_per_task: bool = False,
    auto_merge_wait_for_checks: bool = False,
    prompts_block: str | None = None,
) -> OrchestratorConfig:
    """Build a config pointing ``repo.local_path`` at the clone, with the given footprint/checks.

    ``prompts_block`` is appended verbatim (a full top-level ``prompts:`` YAML block) when given.
    """
    env_lines = "\n".join(f"    - {e}" for e in _TEST_ALLOWED_ENV)
    check_lines = "\n".join(f"    - {c!r}" for c in checks)
    skip_block = "  skip_stages: [" + ", ".join(skip_stages) + "]\n" if skip_stages else ""
    skip_block += f"  allow_review_skip: {str(allow_review_skip).lower()}\n"
    validation_block = f"validation:\n  quarantine_folder: {quarantine!r}\n" if quarantine else ""
    text = f"""
orchestrator:
  auto_mode:
    enabled: {str(auto_mode).lower()}
repo:
  url: "git@example.com:o/r.git"
  local_path: {str(clone)!r}
  base_branch: "main"
  branch_prefix: "agent"
agents:
  allowed: [claude, codex]
  max_fix_cycles: {max_fix_cycles}
  max_total_fix_iterations: {max_total_fix_iterations}
{skip_block}  decomposition:
    enabled: {str(decomposition).lower()}
    max_subtasks: {max_subtasks}
  routing:
    refinement: {{primary: claude, fallback: codex}}
    planning: {{primary: claude, fallback: codex}}
    implementation: {{primary: claude, fallback: codex}}
    review: {{primary: codex, fallback: claude}}
    fixing: {{primary: claude, fallback: codex}}
    summary: {{primary: claude, fallback: codex}}
  providers:
    claude:
      command: "claude"
    codex:
      command: "codex"
security:
  allowed_environment:
{env_lines}
{validation_block}checks:
  commands:
{check_lines if checks else "    []"}
  timeout_seconds: 30
git:
  create_pull_request: {str(create_pr).lower()}
  pr_base: "main"
  auto_merge: {str(auto_merge).lower()}
  auto_merge_strategy: {auto_merge_strategy}
  auto_merge_allow_per_task: {str(auto_merge_allow_per_task).lower()}
  auto_merge_wait_for_checks: {str(auto_merge_wait_for_checks).lower()}
  footprint:
    location: {location}
    tracking: {tracking}
    external_root: "./external"
    audit_commit_message: "chore(orchestrator): audit trail for {{task_id}}"
    audit_on_branch: {audit_on_branch}
"""
    if prompts_block:
        text += "\n" + prompts_block
    return loads_config(text).config


@pytest.fixture
def git_run() -> Callable[[Sequence[str], Path], str]:
    """The raw git runner for test setup/inspection."""
    return run_git


@pytest.fixture
def make_git_config(tmp_path: Path) -> Callable[..., OrchestratorConfig]:
    """Build configs whose rejected-task quarantine is isolated to the current test."""

    def _make(clone: Path, **kwargs: object) -> OrchestratorConfig:
        if kwargs.get("quarantine") is None:
            kwargs["quarantine"] = str(tmp_path / "rejected")
        return build_git_config(clone, **kwargs)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    """A clone on ``main`` with one initial commit, wired to a local bare ``origin`` remote."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    run_git(["init", "--bare", "-b", "main", "."], remote)

    clone = tmp_path / "clone"
    clone.mkdir()
    run_git(["init", "-b", "main", "."], clone)
    run_git(["config", "user.email", "test@example.com"], clone)
    run_git(["config", "user.name", "Test"], clone)
    run_git(["config", "commit.gpgsign", "false"], clone)
    (clone / "README.md").write_text("# project\n", encoding="utf-8")
    run_git(["add", "README.md"], clone)
    run_git(["commit", "-m", "initial commit"], clone)
    run_git(["remote", "add", "origin", str(remote)], clone)
    run_git(["push", "-u", "origin", "main"], clone)
    return GitRepo(clone=clone, remote=remote)
