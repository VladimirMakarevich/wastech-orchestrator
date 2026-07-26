"""Real-host, no-model canary smoke for the Codex permission profile (WRI-003).

Runs the actual generator output under the actual ``codex sandbox`` (credential-free, no model, no
network) and asserts the deny/read-only boundary is OS-enforced on this host. This is the only test
that proves real enforcement; the deterministic suite proves wiring only. Hosted GitHub runners ship
no Codex CLI, so this is a local/manual gate; the deterministic 3-OS pytest matrix proves the
cross-platform wiring. Records the Codex version and platform in the assertion context.

Skipped only when the host *cannot enforce at all* — no ``codex`` on PATH, or a sandbox backend that
refuses to start (native Windows requires an **elevated** backend: "Restricted read-only access
requires the elevated Windows sandbox backend"). Never skipped merely for being Windows: WRI-006
requires the native-Windows sandbox be proven wherever it can run, so an elevated Windows host runs
all of this in full. See :func:`_cannot_enforce` for why that gate cannot mask a leak.

IMPORTANT: the workspace must NOT live under a temp root — Codex's sandbox always grants the system
temp root (``/tmp`` / ``:slash_tmp``) as writable/readable, which would mask every carve-out. So the
fixture is built under the home directory (a real, non-tmp path), mirroring a clone, and removed
afterwards.
"""

from __future__ import annotations

import functools
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
    build_canary_command,
    build_canary_probes,
    default_canary_runner,
    run_codex_canary,
    run_codex_capability_smoke,
)
from wastech_orchestrator.providers.codex_profile import (
    build_codex_permission_profile,
    render_permission_profile_arg,
    toml_basic_string,
)
from wastech_orchestrator.runtime_layout import InternalDenyPolicy, ProviderWriteGuardPolicy


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


@functools.cache
def _cannot_enforce() -> str:
    """Why ``codex sandbox`` cannot enforce a restricted profile on this host (``""`` when it can).

    Runs one **positive control**: a read the profile explicitly grants. If even that fails the host
    cannot demonstrate any policy, every probe below would fail for a host reason, and the canary
    correctly reports ``CAPABILITY_UNAVAILABLE`` — a host limit, not a profile defect (production
    surfaces it through ``worc preflight``). Native Windows needs an *elevated* sandbox backend
    ("Restricted read-only access requires the elevated Windows sandbox backend"), so an unelevated
    host lands here.

    The gate is deliberately keyed on the probed capability and never on "is Windows": WRI-006
    requires the native-Windows sandbox be proven where it *can* run, so an elevated Windows host
    still executes these tests in full. It also cannot mask a leak — a host that enforces at all
    passes this control and then runs every deny probe for real.
    """
    if shutil.which("codex") is None:
        return "needs the real codex CLI (local/manual gate; hosted CI ships none)"
    root = Path(tempfile.mkdtemp(prefix="worc-canaryprobe-", dir=str(Path.home())))
    try:
        repo = root / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "main.py").write_text("print(1)", encoding="utf-8")
        granted = str(repo / "src" / "main.py")
        # Probe the same profile shape the tests below use, so the skip reason carries the backend's
        # real diagnostic (a bare profile fails with an unhelpful "Access is denied.").
        profile = _profile_arg(repo, "read-only")
        control = next(
            p
            for p in build_canary_probes(
                private_probe=granted, exchange_probe=granted, system=platform.system()
            )
            if p.label == "exchange-read-allowed"
        )
        argv = build_canary_command(_codex(), profile, str(repo), control)
        with tempfile.TemporaryDirectory(prefix="worc-codexhome-") as codex_home:
            rc, output = default_canary_runner(
                argv, str(repo), {**_env(), "CODEX_HOME": codex_home}
            )
        if rc != 0:
            return f"host cannot enforce a codex sandbox profile: {output.strip()[:200]}"
        return ""
    finally:
        shutil.rmtree(root, ignore_errors=True)


pytestmark = [
    pytest.mark.skipif(bool(_cannot_enforce()), reason=_cannot_enforce() or "host can enforce"),
    pytest.mark.slow,  # invokes the real codex CLI
]


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
    # `toml_basic_string` (the production renderer's own quoting) escapes the backslashes in a
    # native Windows path; interpolating the path raw would emit invalid TOML escapes (`\U...`)
    # and the profile would be rejected before enforcement was ever observed.
    bad = (
        'permissions.worc={ "extends" = ":workspace", "filesystem" = '
        f'{{ ":minimal" = "read", {toml_basic_string(str(clone))} = "write" }} }}'
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
