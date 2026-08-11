"""Shared fixtures for the test suite."""

from __future__ import annotations

import os
import shlex
import shutil
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
from wastech_orchestrator.providers import claude as _claude

_FAKE_AGENT = Path(__file__).resolve().parent / "fakes" / "fake_agent.py"


@pytest.fixture(autouse=True)
def _assume_bash_sandbox_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the deterministic suite host-independent by assuming a sandbox-capable host.

    The Claude Bash-sandbox capability depends on the real host (macOS Seatbelt / a Linux+WSL2 with
    bubblewrap+socat), so a bwrap-less CI would otherwise flag every workspace-write
    ``isolation_reasons``
    / ``check_isolation`` / provider run. The deterministic suite cannot prove the real host
    boundary
    anyway (no real Claude) — the real proof is the native-Windows CI gate — so we pin the default
    probe
    to "available"; the platform-branch tests inject a concrete ``SandboxCapability`` to
    exercise the native-Windows / missing-deps branches.
    """
    monkeypatch.setattr(
        _claude,
        "default_sandbox_probe",
        lambda *a, **k: _claude.SandboxCapability.LINUX_AVAILABLE,
    )


@pytest.fixture
def no_provider_auth_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disarm the startup credential gate for command tests that are not about credentials.

    ``run``/``watch``/``rerun`` probe every allowed provider's CLI before starting, and the shared
    test config names the real ``claude``/``codex`` commands — so without this a test about PID
    files or a confirmation prompt would spawn the operator's actual CLIs and its result would
    depend on whether this host happens to be logged in. The gate's own behavior is covered directly
    by the tests asserting on it, so switching it off here removes a host dependency, not coverage.
    """
    monkeypatch.setattr("wastech_orchestrator.cli.require_provider_auth", lambda _config: None)


# The packaged built-in flows tree (source-tree/wheel path). ``worc install`` copies this into an
# operator's ``.worc/flows/``; since the registry no longer falls back to the packaged tree at run
# time, a test that resolves a built-in flow points a FlowRegistry's ``operator_flows_dir`` here, or
# seeds a clone's ``.worc/flows/`` from it via ``seed_builtin_flows`` — mirroring real install.
BUILTIN_FLOWS_DIR: Path = Path(str(resources.files("wastech_orchestrator"))) / "packaged" / "flows"


def seed_builtin_flows(clone: Path) -> None:
    """Deliver the packaged built-in flows into ``clone/.worc/flows/``, mirroring ``worc install``.

    The registry reads flows only from ``.worc/flows/`` (no packaged fallback), so any test
    that runs the orchestrator against a clone needs them physically present — call this in the
    harness that builds a task-running orchestrator (not in the pure config builder, so
    flow-content tests like
    ``validate-flow`` keep full control of ``.worc/flows/``). ``.worc/``/``.worc-io/`` are excluded
    via the clone-local ``.git/info/exclude`` (as ``worc install`` does), so seeding never dirties
    git and even a merge's ``git add -A`` skips them (the commit gate refuses a staged
    ``.worc``). Idempotent.
    """
    worc_flows = clone / ".worc" / "flows"
    if not worc_flows.exists():
        shutil.copytree(BUILTIN_FLOWS_DIR, worc_flows)
    # Mirror install's runtime-exclude so `git add -A` (the merge path) never stages the seeded
    # `.worc/`. `.git/info/exclude` is clone-local (not a working-tree file), so it dirties nothing.
    exclude = clone / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    present = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    missing = [ln for ln in (".worc/", ".worc-io/") if ln not in present]
    if missing:
        exclude.write_text("\n".join([*present, *missing]) + "\n", encoding="utf-8")


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
        .joinpath("packaged", "config.example.yaml")
        .read_text(encoding="utf-8")
    )


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
    """Run a git command for test setup/inspection (real git, not the manager); return stdout.

    Decodes explicitly as UTF-8 rather than the platform locale encoding: Git always writes path
    and ref data as UTF-8 on every OS, but Windows' default locale codec is not reliably UTF-8, so
    a test asserting a literal non-ASCII path (e.g. via ``-z`` output) would be flaky there without
    it.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class GitRepo:
    clone: Path
    remote: Path


def build_git_config(
    clone: Path,
    *,
    create_pr: bool = True,
    audit_on_branch: str = "task",
    checks: Sequence[str] = (),
    decomposition: bool = False,
    max_subtasks: int = 8,
    max_fix_cycles: int = 3,
    max_total_fix_iterations: int = 5,
    quarantine: str | None = None,
    auto_mode: bool = False,
    auto_merge: bool = False,
    auto_merge_strategy: str = "squash",
    auto_merge_wait_for_checks: bool = False,
    prompt_audit: bool = False,
    tasks_dir: str = "tasks",
    telegram_trace: bool = False,
    memory_enabled: bool = False,
    allow_git_evidence: bool = False,
    allow_native_memory: bool = False,
    trust_level: str | None = None,
    checkout_base_on_cleanup: bool | None = None,
    clean_runs_on_success: bool = True,
    supervisor_observe: str | None = None,
    supervisor_include_nodes: Sequence[str] = (),
    supervisor_enabled: bool | None = None,
    skills_dynamic: bool | None = None,
) -> OrchestratorConfig:
    """Build a config pointing ``repo.local_path`` at the clone, with the given footprint/checks.

    ``clean_runs_on_success`` mirrors the shipped default (a successful task evicts its own
    ``.worc/runs/`` subtree). Pass ``False`` in a test that inspects a finished task's frozen
    bundles or sealed exchange — the same switch an operator flips to analyze runs.

    ``supervisor_observe`` sets the global observation cadence (absent → the schema default,
    ``events``). Note that it only reaches a run whose flow declares no cadence of its own: a
    flow's own ``supervisor.observe.mode`` narrows this, so a test driving a specific mode end to
    end should use a flow with no ``supervisor:`` block.

    ``supervisor_enabled=False`` removes the whole layer. Two consequences a test must expect: a
    flow's own cadence is no longer checked against the global one (there is nothing to widen), and
    ``memory_enabled=True`` is quenched by the loader to ``False`` for the run — so the config this
    returns has ``memory.enabled is False``. The warning that says so is on
    ``loads_config(...).warnings``, which this helper drops; assert it through ``loads_config``.
    """
    env_lines = "\n".join(f"    - {e}" for e in _TEST_ALLOWED_ENV)
    cleanup_line = (
        f"  checkout_base_on_cleanup: {str(checkout_base_on_cleanup).lower()}\n"
        if checkout_base_on_cleanup is not None
        else ""
    )
    paths_block = f"paths:\n  tasks_dir: {tasks_dir!r}\n" if tasks_dir != "tasks" else ""
    # Absent => the shipped default `auto`; pass "strict" in a test that drives the diff-shape gate.
    trust_level_line = f"  trust_level: {trust_level}\n" if trust_level is not None else ""
    # ``checks`` (shell-string commands) map to one always-on ``default`` command set (v15).
    if checks:
        cmd_lines = "\n".join(f"        - {{ argv: {shlex.split(c)!r} }}" for c in checks)
        checks_block = "  command_sets:\n    default:\n      commands:\n" + cmd_lines + "\n"
    else:
        checks_block = "  command_sets: {}\n"
    validation_block = f"validation:\n  quarantine_folder: {quarantine!r}\n" if quarantine else ""
    telegram_block = "telegram:\n  trace: true\n" if telegram_trace else ""
    memory_block = "memory:\n  enabled: true\n" if memory_enabled else ""
    logging_block = (
        "logging:\n  clean_runs_on_success: false\n" if not clean_runs_on_success else ""
    )
    skills_block = (
        f"skills:\n  dynamic: {str(skills_dynamic).lower()}\n" if skills_dynamic is not None else ""
    )
    supervisor_lines = []
    if supervisor_enabled is not None:
        supervisor_lines.append(f"  enabled: {str(supervisor_enabled).lower()}\n")
    if supervisor_observe is not None:
        supervisor_lines.append(f"  observe:\n    mode: {supervisor_observe}\n")
        if supervisor_include_nodes:
            supervisor_lines.append(f"    include_nodes: {list(supervisor_include_nodes)!r}\n")
    supervisor_block = "supervisor:\n" + "".join(supervisor_lines) if supervisor_lines else ""
    text = f"""
orchestrator:
  auto_mode:
    enabled: {str(auto_mode).lower()}
repo:
  url: "git@example.com:o/r.git"
  local_path: {str(clone)!r}
  base_branch: "main"
  branch_prefix: "worc"
{cleanup_line}{paths_block}agents:
  allowed: [claude, codex]
  max_fix_cycles: {max_fix_cycles}
  max_total_fix_iterations: {max_total_fix_iterations}
  decomposition:
    enabled: {str(decomposition).lower()}
    max_subtasks: {max_subtasks}
  providers:
    claude:
      command: "claude"
      primary: true
      allow_native_memory: {str(allow_native_memory).lower()}
    codex:
      command: "codex"
security:
  allow_git_evidence: {str(allow_git_evidence).lower()}
{trust_level_line}  allowed_environment:
{env_lines}
{validation_block}{telegram_block}checks:
{checks_block}  timeout_seconds: 30
git:
  create_pull_request: {str(create_pr).lower()}
  pr_base: "main"
  auto_merge: {str(auto_merge).lower()}
  auto_merge_strategy: {auto_merge_strategy}
  auto_merge_wait_for_checks: {str(auto_merge_wait_for_checks).lower()}
  footprint:
    audit_commit_message: "chore(orchestrator): audit trail for {{task_id}}"
    audit_on_branch: {audit_on_branch}
prompt_audit: {str(prompt_audit).lower()}
{memory_block}{logging_block}{skills_block}{supervisor_block}"""
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
