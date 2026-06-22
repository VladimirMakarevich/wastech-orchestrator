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


def has_gh() -> bool:
    """Whether the GitHub CLI (``gh``) is on ``PATH`` (gates the PR-creation default).

    The raising runtime gate built on this lives in :mod:`wastech_orchestrator.preflight`.
    """
    return find_executable("gh") is not None
