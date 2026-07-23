"""Real-host, no-model canary smoke for the Codex permission profile (WRI-003).

Runs the actual generator output under the actual ``codex sandbox`` (credential-free, no model, no
network) and asserts the deny/read-only boundary is OS-enforced on this host. This is the only test
that proves real enforcement; the deterministic suite proves wiring only. Skipped **only** when
``codex`` is absent — so it runs natively on Windows too when the CLI is present (WRI-006 requires
the native-Windows sandbox be *proven*, not skipped merely for being Windows). Hosted GitHub runners
ship no Codex CLI, so this is a local/manual gate; the deterministic 3-OS pytest matrix proves the
cross-platform wiring. Records the Codex version and platform in the assertion context.

IMPORTANT: the workspace must NOT live under a temp root — Codex's sandbox always grants the system
temp root (``/tmp`` / ``:slash_tmp``) as writable/readable, which would mask every carve-out. So the
fixture is built under the home directory (a real, non-tmp path), mirroring a clone, and removed
afterwards.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from wastech_orchestrator.providers.base import ErrorClass
from wastech_orchestrator.providers.codex_canary import (
    CAPABILITY_PASSED,
    run_codex_canary,
    run_codex_capability_smoke,
)
from wastech_orchestrator.providers.codex_profile import (
    build_codex_permission_profile,
    render_permission_profile_arg,
)
from wastech_orchestrator.runtime_layout import InternalDenyPolicy, ProviderWriteGuardPolicy

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="needs the real codex CLI (local/manual gate; hosted CI ships none)",
)


@pytest.fixture
def clone() -> Iterator[Path]:
    # A non-tmp workspace so Codex's tmp grant does not mask the carve-outs (see module docstring).
    root = Path(tempfile.mkdtemp(prefix="worc-canary-", dir=str(Path.home())))
    try:
        repo = root / "repo"
        (repo / ".worc" / "logs").mkdir(parents=True)
        (repo / ".worc-io" / "t").mkdir(parents=True)
        (repo / "src").mkdir(parents=True)
        (repo / ".worc" / "logs" / "req.json").write_text("PRIVATE_SECRET", encoding="utf-8")
        (repo / ".worc-io" / "t" / "task.md").write_text("EXCHANGE_TASK", encoding="utf-8")
        (repo / "src" / "main.py").write_text("print(1)", encoding="utf-8")
        yield repo
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _profile_arg(repo: Path, profile: str) -> str:
    deny = InternalDenyPolicy(
        control_home=repo / ".worc",
        private_home=repo / ".worc",
        env_file=None,
        provider_homes=(),
    )
    wg = ProviderWriteGuardPolicy(
        exchange_root=repo / ".worc-io",
        git_dir=repo / ".git",
        git_common_dir=repo / ".git",
        hooks_dir=repo / ".git" / "hooks",
        tasks_dir=repo / "tasks",
    )
    generated = build_codex_permission_profile(
        permission_profile=profile,
        working_directory=str(repo),
        deny_policy=deny,
        write_guard=wg if profile == "workspace-write" else None,
        denied_read_paths=(".env", "secrets/**"),
    )
    return render_permission_profile_arg(generated)


def _codex() -> str:
    resolved = shutil.which("codex")
    assert resolved is not None  # guarded by the module skipif
    return resolved


def _env() -> dict[str, str]:
    # Include codex's own dir on PATH so the CLI resolves. On POSIX add /usr/bin + /bin for the
    # /bin/cat|/bin/sh probes and HOME so codex locates its sandbox resources; on Windows keep the
    # inherited PATH (System32 for cmd.exe + the sandbox helper) and the essentials cmd/codex need.
    codex_dir = str(Path(_codex()).parent)
    if platform.system() == "Windows":
        env = {"PATH": os.pathsep.join([codex_dir, os.environ.get("PATH", "")])}
        for key in ("USERPROFILE", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "LOCALAPPDATA"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env
    return {"PATH": f"{codex_dir}:/usr/bin:/bin", "HOME": str(Path.home())}


def _version() -> str:
    out = subprocess.run([_codex(), "--version"], capture_output=True, text=True, check=False)
    return (out.stdout or out.stderr).strip()


@pytest.mark.parametrize("profile", ["read-only", "workspace-write"])
def test_generated_profile_is_os_enforced(clone: Path, profile: str) -> None:
    outcome = run_codex_canary(
        command=_codex(),
        profile_arg=_profile_arg(clone, profile),
        working_directory=str(clone),
        private_probe=str(clone / ".worc" / "logs" / "req.json"),
        exchange_probe=str(clone / ".worc-io" / "t" / "task.md"),
        env=_env(),
        system=platform.system(),
    )
    assert outcome.ok, (
        f"canary failed on {platform.system()} / codex {_version()} for {profile}: "
        f"{outcome.message} :: {outcome.evidence}"
    )
    # the private read (direct + shell) must be denied; the exchange readable but not writable
    verdicts = {e["probe"]: e["denied"] for e in outcome.evidence}
    assert verdicts["private-read-denied"] is True
    assert verdicts["private-shell-read-denied"] is True
    assert verdicts["exchange-read-allowed"] is False
    assert verdicts["exchange-write-denied"] is True


@pytest.mark.parametrize("profile", ["read-only", "workspace-write"])
def test_capability_smoke_passes_on_real_host(profile: str) -> None:
    # WRI-006 / H7: the self-contained no-model capability smoke `worc preflight` runs — it builds
    # its own throwaway fixture (private denied incl. a workspace symlink alias, repo-read positive
    # control, repo-write per profile, exchange read-only) and records the MCP inventory — must be
    # OS-enforced (`passed`) on a host with the real codex CLI.
    report = run_codex_capability_smoke(
        command=_codex(),
        home_dir=Path.home(),
        env=_env(),
        permission_profile=profile,
        system=platform.system(),
    )
    assert report.status == CAPABILITY_PASSED, (
        f"capability smoke {report.status} on {platform.system()} / codex {_version()} "
        f"for {profile}: {report.detail} :: {report.evidence}"
    )
    labels = {e["probe"] for e in report.evidence}
    assert {"private-read-denied", "repo-read-allowed", "mcp-inventory"} <= labels


def test_canary_detects_a_non_enforcing_profile(clone: Path) -> None:
    # A profile that (wrongly) grants the private home read must be caught as a security leak —
    # proving the canary observes enforcement rather than rubber-stamping.
    bad = (
        'permissions.worc={ "extends" = ":workspace", "filesystem" = '
        f'{{ ":minimal" = "read", "{clone}" = "write" }} }}'
    )
    outcome = run_codex_canary(
        command=_codex(),
        profile_arg=bad,
        working_directory=str(clone),
        private_probe=str(clone / ".worc" / "logs" / "req.json"),
        exchange_probe=None,
        env=_env(),
        system=platform.system(),
    )
    assert not outcome.ok
    assert outcome.error_class is ErrorClass.CONFIGURATION_ERROR
