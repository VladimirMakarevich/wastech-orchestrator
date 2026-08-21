"""Environment detection for the installer (read-only).

Probes the operator's machine to seed the wizard's defaults: the current Git repository's root,
``origin`` URL, branches and cleanliness; which agent CLIs (``codex`` / ``claude``) and ``gh`` are
on ``PATH``; and a sensible default ``checks`` list inferred from the repo's ecosystem markers.

Everything here is **read-only** and operator-side. Git is launched through the shared safe process
runner (:func:`~wastech_orchestrator.providers.process.run_process`) — an **argv list, never a shell
string**, with a mandatory timeout — so the no-shell-interpolation invariant holds at every call
site. These probes are trusted and network-free, but they are not exempt from the git environment
policy: they used to run on the operator's full environment, which meant a shell ``GIT_DIR`` pointed
them at another repository and a shell ``GH_REPO`` at another remote — on **every** CLI command, via
``resolve_config_path``. They now use the same allowlist-plus-scrub environment as every other
orchestrator-owned git process (:func:`~wastech_orchestrator.git_manager.build_helper_git_env`),
with ``gh auth status`` alone widened by the two token names it exists to account for.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.config.schema import SecurityConfig
from wastech_orchestrator.git_manager import build_helper_git_env
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.providers.process import run_process
from wastech_orchestrator.security.launchers import resolve_launcher

#: ``git``/``gh`` resolved once at import, the same rule the Git Manager and the adapters follow: a
#: bare name lets a directory earlier on ``PATH`` decide what answers. It matters here more than the
#: name suggests — ``git_info`` resolves the repository root on **every** CLI command, so a shim
#: chooses which ``config.yaml`` the orchestrator reads, and the ``gh`` probe decides the
#: install-time default for opening pull requests. Falls back to the bare name when the executable
#: is not on ``PATH`` at all, so a launch failure stays the caller's ordinary "not found" answer.
_GIT = resolve_launcher("git") or "git"
_GH = resolve_launcher("gh") or "gh"

# Read-only git probes are quick; bound them so a hung git can never wedge the installer.
_GIT_TIMEOUT_SECONDS = 30

#: ``gh auth status`` reports an environment token as authenticated, which is the whole reason it is
#: the right probe — so these two names are forwarded on purpose. Nothing else is.
_GH_TOKEN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")


@dataclass(frozen=True)
class GitInfo:
    """What ``install`` needs to know about the bound repository (repo/footprint)."""

    root: Path
    origin_url: str | None
    current_branch: str | None
    default_branch: str | None
    is_clean: bool


def _run_git(args: list[str], cwd: Path, security: SecurityConfig | None = None) -> tuple[int, str]:
    """Run a read-only git command through the safe runner; return ``(returncode, stdout)``.

    A launch failure (``git`` missing or an invalid ``cwd``) maps to returncode 127 and a timeout to
    124, so callers treat "git unavailable" the same as any other probe failure; the wizard checks
    ``find_executable('git')`` up front to give a precise message.
    """
    with tempfile.TemporaryDirectory() as tmp:
        stdout_path = Path(tmp) / "stdout"
        result = run_process(
            [_GIT, *args],
            cwd=cwd,
            env=build_helper_git_env(security=security),
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


def git_version() -> tuple[int, int] | None:
    """The installed git's ``(major, minor)``, or ``None`` when git is absent/unparseable.

    Runs ``git --version`` through the safe runner (it needs no repo). Feeds the git-control
    preflight gate that fails fast when git is too old to honor ``-c core.hooksPath`` (< 2.9), which
    would
    otherwise leave the hook-neutralization silently ineffective.
    """
    rc, out = _run_git(["--version"], Path.cwd())
    if rc != 0:
        return None
    match = re.search(r"git version (\d+)\.(\d+)", out)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def gh_auth_ok(security: SecurityConfig | None = None) -> bool | None:
    """Whether ``gh`` is authenticated, via a read-only ``gh auth status`` probe.

    Returns ``True`` on exit 0 (authenticated), ``False`` on a non-zero exit (logged out), and
    ``None`` on a launch failure / timeout (unknown — a missing ``gh`` binary is
    :func:`has_gh`/``require_gh``'s concern, not this one). Mirrors :func:`_run_git`: the probe runs
    through the safe runner with the policy-built git/gh environment, so an env
    ``GH_TOKEN``/``GITHUB_TOKEN`` is honored (``gh auth status`` already accounts for env tokens,
    which is why it is the right probe).

    ``security`` is the operator's policy when the caller has one — ``worc preflight`` and the run
    entry points do, the installer does not. Passing it matters for more than tidiness: this probe
    decides the reported auth verdict, and without the operator's ``allowed_environment`` an
    ``HTTPS_PROXY`` that every other ``gh`` call gets never reached it, so a proxied host reported
    "not logged in" while publishing worked.

    Its stdout (streamed to a throwaway temp file) and stderr are discarded, never surfaced —
    ``gh auth status`` prints the account login and token scopes, which must stay out of
    logs/artifacts per the no-secrets invariant.
    """
    with tempfile.TemporaryDirectory() as tmp:
        result = run_process(
            [_GH, "auth", "status"],
            cwd=tmp,
            # Built per call, not once at import: the CLI loads `<repo>/.worc/.env` into the
            # environment AFTER this module is imported, and a `GH_TOKEN` kept there (the
            # shipped `.env.example` offers exactly that line) has to reach this probe.
            env=build_helper_git_env(*_GH_TOKEN_NAMES, security=security),
            timeout_seconds=_GIT_TIMEOUT_SECONDS,
            stdout_path=Path(tmp) / "stdout",
        )
    if result.launch_error is not None or result.timed_out or result.exit_code is None:
        return None
    return result.exit_code == 0
