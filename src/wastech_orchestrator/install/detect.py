"""Environment detection for the installer (read-only).

Probes the operator's machine to seed the wizard's defaults: the current Git repository's root,
``origin`` URL, branches and cleanliness; which agent CLIs (``codex`` / ``claude``) and ``gh`` are
on ``PATH``; and a sensible default ``checks`` list inferred from the repo's ecosystem markers.

Everything here is **read-only** and operator-side. Git is launched through the shared safe process
runner (:func:`~wastech_orchestrator.providers.process.run_process`) — an **argv list, never a shell
string**, with a mandatory timeout — so the no-shell-interpolation invariant holds at every call
site (spec §12.5). Unlike sandboxed *agent* runs, these trusted, network-free probes run with the
operator's own environment.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.providers.process import run_process

# Read-only git probes are quick; bound them so a hung git can never wedge the installer.
_GIT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class GitInfo:
    """What ``install`` needs to know about the bound repository (spec §11 repo/footprint)."""

    root: Path
    origin_url: str | None
    current_branch: str | None
    default_branch: str | None
    is_clean: bool


def _run_git(args: list[str], cwd: Path) -> tuple[int, str]:
    """Run a read-only git command through the safe runner; return ``(returncode, stdout)``.

    A launch failure (``git`` missing or an invalid ``cwd``) maps to returncode 127 and a timeout to
    124, so callers treat "git unavailable" the same as any other probe failure; the wizard checks
    ``find_executable('git')`` up front to give a precise message.
    """
    with tempfile.TemporaryDirectory() as tmp:
        stdout_path = Path(tmp) / "stdout"
        result = run_process(
            ["git", *args],
            cwd=cwd,
            env=dict(os.environ),
            timeout_seconds=_GIT_TIMEOUT_SECONDS,
            stdout_path=stdout_path,
        )
        if result.launch_error is not None:
            return 127, ""
        if result.timed_out or result.exit_code is None:
            return 124, ""
        return result.exit_code, stdout_path.read_text(encoding="utf-8", errors="replace")


def git_info(cwd: Path | str) -> GitInfo | None:
    """Inspect the Git repository containing ``cwd``; ``None`` when it is not inside one."""
    start = Path(cwd)
    rc, toplevel = _run_git(["rev-parse", "--show-toplevel"], start)
    if rc != 0:
        return None
    root = Path(toplevel.strip()).resolve()

    rc, origin = _run_git(["remote", "get-url", "origin"], root)
    origin_url = origin.strip() if rc == 0 else ""

    rc, head = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    current = head.strip() if rc == 0 else ""
    current_branch = current if current and current != "HEAD" else None  # "HEAD" => detached

    rc, status = _run_git(["status", "--porcelain"], root)
    is_clean = rc == 0 and status.strip() == ""

    return GitInfo(
        root=root,
        origin_url=origin_url or None,
        current_branch=current_branch,
        default_branch=_default_branch(root, current_branch),
        is_clean=is_clean,
    )


def _default_branch(root: Path, current_branch: str | None) -> str | None:
    """The remote's default branch (``origin/HEAD``), falling back to the current branch."""
    rc, out = _run_git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], root)
    if rc == 0:
        ref = out.strip()  # e.g. "refs/remotes/origin/main"
        if "/" in ref:
            return ref.rsplit("/", 1)[-1]
    return current_branch


def find_executable(name: str) -> str | None:
    """Resolve ``name`` on ``PATH`` (``shutil.which``); ``None`` when it is not installed."""
    return shutil.which(name)


def detect_providers() -> dict[ProviderId, str | None]:
    """Map each agent provider to its resolved CLI path on ``PATH`` (``None`` when absent)."""
    return {pid: find_executable(pid.value) for pid in ProviderId}


class GhNotAvailableError(OSError):
    """The GitHub CLI (``gh``) is required for PR creation but is not on ``PATH`` (§6.7)."""


def has_gh() -> bool:
    """Whether the GitHub CLI (``gh``) is on ``PATH`` (gates the PR-creation default)."""
    return find_executable("gh") is not None


def require_gh() -> None:
    """Raise :class:`GhNotAvailableError` unless ``gh`` is on ``PATH`` (hard pre-flight gate).

    The raising counterpart to :func:`has_gh`, used at ``watch``/``run`` startup when PR creation is
    enabled so a missing GitHub CLI fails fast with an actionable message rather than surfacing as a
    ``GitCommandError`` deep inside the publish stage.
    """
    if not has_gh():
        raise GhNotAvailableError(
            "'gh' (GitHub CLI) is not installed or not on PATH. Install it from "
            "https://cli.github.com/ and run 'gh auth login', or disable PR creation "
            "(git.create_pull_request: false)."
        )


def detect_checks(repo_root: Path | str) -> list[str]:
    """Propose default ``checks.commands`` from the repo's ecosystem markers (first match wins)."""
    root = Path(repo_root)
    if (root / "pyproject.toml").is_file():
        return ["pytest"]
    if (root / "package.json").is_file():
        return _node_checks(root / "package.json")
    if (root / "Cargo.toml").is_file():
        return ["cargo test"]
    if (root / "go.mod").is_file():
        return ["go test ./..."]
    return []


def _node_checks(package_json: Path) -> list[str]:
    """Read ``package.json`` ``scripts`` and propose ``npm`` commands for the ones that exist."""
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return []
    commands: list[str] = []
    if "test" in scripts:
        commands.append("npm test")
    if "lint" in scripts:
        commands.append("npm run lint")
    return commands
