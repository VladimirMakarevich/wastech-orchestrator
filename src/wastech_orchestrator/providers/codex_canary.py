"""No-model effective-policy canary for the Codex permission profile (WRI-003).

Before ``codex exec`` starts, prove the generated profile is actually enforced on THIS host/CLI by
running the *same* profile under ``codex sandbox -P`` (a no-model sandbox runner) and
checking that internal paths are denied and the exchange is read-only. ``codex sandbox`` applies
exactly the profile ``codex exec`` selects via ``default_permissions`` — one definition, two
selectors — so a pass here is real OS-enforcement evidence, not prompt hygiene.

Classification (mirrors WRI-002's error-class split):

* a **denied path that turned out readable/writable** (a real leak) → ``CONFIGURATION_ERROR``, a
  non-fallback security result: the profile is not enforcing and no other provider should be tried;
* the **sandbox could not run / could not demonstrate the requested policy** on this host (Codex
  itself refuses to run unsandboxed on native Windows when it cannot enforce a split policy; a Linux
  host missing its sandbox helper; an allowed path wrongly blocked) → ``CAPABILITY_UNAVAILABLE``, a
  deterministic pre-model infrastructure result the Router may only fall over to a same-or-stricter,
  self-isolating provider for.

The probes read files that already exist (the attempt's own ``request.json`` under the private home;
the frozen task packet in the exchange), so the canary never writes into — and never mutates — the
curated exchange.
"""

from __future__ import annotations

import platform
import shlex
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from wastech_orchestrator.providers.base import ErrorClass
from wastech_orchestrator.providers.codex_profile import (
    PROFILE_NAME,
    build_codex_permission_profile,
    render_permission_profile_arg,
)
from wastech_orchestrator.providers.process import run_process
from wastech_orchestrator.runtime_layout import (
    CONTROL_HOME_DIRNAME,
    EXCHANGE_HOME_DIRNAME,
    InternalDenyPolicy,
    ProviderWriteGuardPolicy,
)

# A probe runner: ``(argv, cwd, env) -> (returncode, output)``. Injectable so the classifier is
# unit-testable with a fake CLI; the default runs the real ``codex sandbox`` through the shared safe
# process runner (argv list, no shell, allowlisted env, mandatory timeout) — never a raw launcher.
CanaryRunner = Callable[[list[str], str, Mapping[str, str]], tuple[int, str]]

CANARY_TIMEOUT_S = 60

# Emitted by the default runner when the sandbox process could not launch or timed out (an infra
# gap, classified as a capability failure, not a policy leak).
_CANARY_UNRUNNABLE = "canary-sandbox-unrunnable"

# Substrings proving the SANDBOX itself could not run / enforce here (a host-capability gap, not a
# policy leak). Includes Codex's fail-closed messages on Windows and the missing Linux helper.
_CAPABILITY_MARKERS: tuple[str, ...] = (
    _CANARY_UNRUNNABLE,
    "refusing to run unsandboxed",
    "missing codex-linux-sandbox",
    "sandbox helper",
    "orchestrator_helper_launch_failed",
    "createprocesswithlogonw failed",
    "failed to initialize sandbox",
    "failed to start sandbox",
    "seatbelt",
    "landlock",
)


@dataclass(frozen=True)
class CanaryProbe:
    """One sandbox probe: run ``command`` under the profile; ``expect_denied`` is the verdict."""

    label: str
    command: list[str]
    expect_denied: bool


@dataclass(frozen=True)
class ExtraProbes:
    """Optional probes the capability smoke adds beyond the per-attempt private/exchange set.

    Grouped into one value so :func:`run_codex_canary` stays within the argument-count ratchet: a
    workspace repo read (the mandatory positive control), a workspace symlink alias resolving to the
    private file (must stay denied), and a repo write (allowed for ``workspace-write``, denied for
    ``read-only``). All default off, so the per-attempt canary passes none.
    """

    repo_probe: str | None = None
    alias_probe: str | None = None
    repo_write_probe: str | None = None
    repo_writable: bool = False


_NO_EXTRA_PROBES = ExtraProbes()


@dataclass(frozen=True)
class CanaryOutcome:
    """The canary verdict. ``error_class`` is ``None`` on success; else the failure class."""

    ok: bool
    error_class: ErrorClass | None = None
    message: str = ""
    evidence: tuple[dict[str, object], ...] = field(default_factory=tuple)


def default_canary_runner(argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
    """Run one probe through the shared safe process runner and return ``(rc, combined output)``.

    A binary that could not launch or a timeout is reported (not raised) as a nonzero rc tagged with
    the ``_CANARY_UNRUNNABLE`` marker, so the classifier maps it to a capability failure.
    """
    with tempfile.TemporaryDirectory() as scratch:
        stdout_path = Path(scratch) / "canary.out"
        result = run_process(
            argv, cwd=cwd, env=env, timeout_seconds=CANARY_TIMEOUT_S, stdout_path=stdout_path
        )
        try:
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stdout = ""
    if result.launch_error is not None:
        return 127, f"{result.launch_error} {_CANARY_UNRUNNABLE}"
    if result.timed_out:
        return 124, f"canary probe timed out {_CANARY_UNRUNNABLE}"
    rc = result.exit_code if result.exit_code is not None else 1
    return rc, stdout + result.stderr_text


def _read_cmd(path: str, system: str) -> list[str]:
    if system == "Windows":
        return ["cmd", "/c", "type", path]
    return ["/bin/cat", path]


def _shell_read_cmd(path: str, system: str) -> list[str]:
    """A shell-mediated read — proves the OS layer blocks *any* program, not one tool."""
    if system == "Windows":
        return ["cmd", "/c", f"type {path}"]
    return ["/bin/sh", "-c", f"cat {shlex.quote(path)}"]


def _write_cmd(path: str, system: str) -> list[str]:
    if system == "Windows":
        return ["cmd", "/c", f"echo x>> {path}"]
    return ["/bin/sh", "-c", f"printf x >> {shlex.quote(path)}"]


def build_canary_probes(
    *,
    private_probe: str,
    exchange_probe: str | None,
    system: str,
    private_readable: bool = False,
    repo_probe: str | None = None,
    alias_probe: str | None = None,
    repo_write_probe: str | None = None,
    repo_writable: bool = False,
) -> list[CanaryProbe]:
    """The probe set for the profile under test.

    Private reads (direct + shell-mediated, and — when *alias_probe* is a workspace symlink/hard
    link that resolves to the private file — through that alias) must be denied. *repo_probe*, when
    given, is the **positive control**: a workspace read that MUST succeed, so a broken probe
    harness (every command failing) can no longer masquerade as "everything denied → enforcing".
    *repo_write_probe* proves the profile's write level (allowed for ``workspace-write``, denied for
    ``read-only``). The exchange, when a file is available, must be readable but not writable — and
    also serves as a positive control on the per-attempt path where no *repo_probe* is supplied.

    ``private_readable`` (VF-6, read-isolation OFF) flips the private-read expectation: the private
    set is now READABLE (the reads become positive controls) but a private WRITE must still be
    denied, so a ``private-write-denied`` probe is added to prove the profile keeps the control
    plane immutable.
    """
    if private_readable:
        probes = [
            CanaryProbe(
                "private-read-allowed", _read_cmd(private_probe, system), expect_denied=False
            ),
            CanaryProbe(
                "private-shell-read-allowed",
                _shell_read_cmd(private_probe, system),
                expect_denied=False,
            ),
            CanaryProbe(
                "private-write-denied", _write_cmd(private_probe, system), expect_denied=True
            ),
        ]
    else:
        probes = [
            CanaryProbe(
                "private-read-denied", _read_cmd(private_probe, system), expect_denied=True
            ),
            CanaryProbe(
                "private-shell-read-denied",
                _shell_read_cmd(private_probe, system),
                expect_denied=True,
            ),
        ]
    if alias_probe is not None:
        probes.append(
            CanaryProbe(
                "private-alias-read-allowed" if private_readable else "private-alias-read-denied",
                _read_cmd(alias_probe, system),
                expect_denied=not private_readable,
            )
        )
    if repo_probe is not None:
        probes.append(
            CanaryProbe("repo-read-allowed", _read_cmd(repo_probe, system), expect_denied=False)
        )
    if repo_write_probe is not None:
        probes.append(
            CanaryProbe(
                f"repo-write-{'allowed' if repo_writable else 'denied'}",
                _write_cmd(repo_write_probe, system),
                expect_denied=not repo_writable,
            )
        )
    if exchange_probe is not None:
        probes.append(
            CanaryProbe(
                "exchange-read-allowed", _read_cmd(exchange_probe, system), expect_denied=False
            )
        )
        probes.append(
            CanaryProbe(
                "exchange-write-denied", _write_cmd(exchange_probe, system), expect_denied=True
            )
        )
    return probes


def build_canary_command(
    command: str, profile_arg: str, working_directory: str, probe: CanaryProbe
) -> list[str]:
    """``codex sandbox -c <inline profile> -P <name> --include-managed-config -C <wd> -- <cmd>``.

    ``--include-managed-config`` resolves the profile with any managed (MDM/system) layer
    applied, so the canary proves the *effective* policy ``codex exec`` will run under — not the
    generated profile in isolation. No model, no network.
    """
    return [
        command,
        "sandbox",
        "-c",
        profile_arg,
        "-P",
        PROFILE_NAME,
        "--include-managed-config",
        "-C",
        working_directory,
        "--",
        *probe.command,
    ]


def run_codex_canary(
    *,
    command: str,
    profile_arg: str,
    working_directory: str,
    private_probe: str,
    exchange_probe: str | None,
    env: Mapping[str, str],
    system: str,
    runner: CanaryRunner = default_canary_runner,
    extra: ExtraProbes = _NO_EXTRA_PROBES,
    private_readable: bool = False,
) -> CanaryOutcome:
    """Prove the profile's deny/read-only boundary via ``codex sandbox`` before ``codex exec``.

    Returns a :class:`CanaryOutcome`; the adapter raises the mapped ``ProviderError``. A leak (a
    denied path readable/writable) is a non-fallback ``CONFIGURATION_ERROR``; an unrunnable /
    undemonstrable sandbox is a ``CAPABILITY_UNAVAILABLE``. Records each probe's verdict as
    redaction-safe evidence (paths only, never file contents).

    Two hardening guarantees (final-review H4): (1) the probes run under a throwaway ``CODEX_HOME``
    so the operator's ``~/.codex/config.toml`` cannot alter profile resolution — ``codex sandbox``
    has no ``--ignore-user-config`` flag, so an empty home reproduces the ``exec`` config layering
    (no model / no network → no credentials needed); (2) the canary refuses to return ``ok`` unless
    at least one **positive control** — an ``expect_denied=False`` read — actually succeeded, so a
    broken probe harness (every command failing) can never be mistaken for a fully-enforcing
    sandbox.
    """
    evidence: list[dict[str, object]] = []
    probes = build_canary_probes(
        private_probe=private_probe,
        exchange_probe=exchange_probe,
        system=system,
        private_readable=private_readable,
        repo_probe=extra.repo_probe,
        alias_probe=extra.alias_probe,
        repo_write_probe=extra.repo_write_probe,
        repo_writable=extra.repo_writable,
    )
    saw_positive_control = False
    with tempfile.TemporaryDirectory(prefix="worc-codexhome-") as codex_home:
        probe_env = {**dict(env), "CODEX_HOME": codex_home}
        for probe in probes:
            argv = build_canary_command(command, profile_arg, working_directory, probe)
            try:
                rc, output = runner(argv, working_directory, probe_env)
            except Exception as exc:  # any runner failure is treated as a host-capability gap
                return CanaryOutcome(
                    ok=False,
                    error_class=ErrorClass.CAPABILITY_UNAVAILABLE,
                    message=f"codex sandbox canary probe {probe.label!r} could not run: {exc}",
                    evidence=tuple(evidence),
                )
            lowered = output.lower()
            denied = rc != 0
            evidence.append(
                {"probe": probe.label, "expect_denied": probe.expect_denied, "denied": denied}
            )
            if any(marker in lowered for marker in _CAPABILITY_MARKERS):
                return CanaryOutcome(
                    ok=False,
                    error_class=ErrorClass.CAPABILITY_UNAVAILABLE,
                    message=(
                        f"codex sandbox could not enforce the permission profile on this host "
                        f"(probe {probe.label!r}); the requested isolation cannot be demonstrated"
                    ),
                    evidence=tuple(evidence),
                )
            if probe.expect_denied and not denied:
                return CanaryOutcome(
                    ok=False,
                    error_class=ErrorClass.CONFIGURATION_ERROR,
                    message=(
                        f"permission-profile canary FAILED: {probe.label!r} was expected to be "
                        "denied but succeeded — the profile is not enforcing (security violation)"
                    ),
                    evidence=tuple(evidence),
                )
            if not probe.expect_denied and denied:
                return CanaryOutcome(
                    ok=False,
                    error_class=ErrorClass.CAPABILITY_UNAVAILABLE,
                    message=(
                        f"permission-profile canary could not demonstrate the requested policy: "
                        f"{probe.label!r} (a required read) was blocked on this host"
                    ),
                    evidence=tuple(evidence),
                )
            saw_positive_control = saw_positive_control or not probe.expect_denied
    if not saw_positive_control:
        return CanaryOutcome(
            ok=False,
            error_class=ErrorClass.CAPABILITY_UNAVAILABLE,
            message=(
                "permission-profile canary could not prove SELECTIVE enforcement: no positive "
                "control (an allowed read) was available to run, so a broken probe harness would "
                "make every denial look enforced — refusing to certify isolation"
            ),
            evidence=tuple(evidence),
        )
    return CanaryOutcome(ok=True, evidence=tuple(evidence))


# --- No-model capability smoke (worc preflight / host gate; final-review H4/H7, WRI-006) ---------

#: Smoke verdicts. ``passed`` = the profile is OS-enforced here; ``unsupported`` = the sandbox could
#: not run / demonstrate the policy on this host (maps to the pre-model ``CAPABILITY_UNAVAILABLE``
#: classification); ``policy-failed`` = a denied path was actually read/written (maps to the
#: non-fallback ``CONFIGURATION_ERROR`` security result). Kept distinct so preflight never silently
#: downgrades strict isolation (WRI-006 acceptance criterion).
CAPABILITY_PASSED = "passed"
CAPABILITY_UNSUPPORTED = "unsupported"
CAPABILITY_POLICY_FAILED = "policy-failed"

#: A no-model tool-surface inventory probe: returns ``(clean_exit, combined_output)`` for a command
#: such as ``codex mcp list``. Injectable so the deterministic suite records a fake inventory;
#: defaults to routing ``codex mcp list`` through the same sandbox *runner* seam under a clean home.
InventoryProbe = Callable[[str, Mapping[str, str]], tuple[bool, str]]


@dataclass(frozen=True)
class CapabilitySmokeReport:
    """The neutral capability-smoke verdict surfaced by ``worc preflight`` and the host gate."""

    status: str  # one of the CAPABILITY_* constants
    detail: str
    evidence: tuple[dict[str, object], ...] = ()

    @property
    def ok(self) -> bool:
        """True only for a fully-demonstrated profile (``passed``)."""
        return self.status == CAPABILITY_PASSED


def _inventory_via_runner(
    runner: CanaryRunner, command: str, env: Mapping[str, str]
) -> tuple[bool, str]:
    """Run ``codex mcp list`` through the sandbox *runner* under a throwaway ``CODEX_HOME``.

    With the user config isolated (empty home), a strict-isolation Codex run must resolve **no** MCP
    servers; this records the effective inventory as evidence (WRI-006 tool-surface inspection).
    Reusing the *runner* seam keeps the whole smoke deterministic when a fake runner is injected.
    """
    with tempfile.TemporaryDirectory(prefix="worc-mcp-home-") as home:
        try:
            rc, output = runner([command, "mcp", "list"], home, {**dict(env), "CODEX_HOME": home})
        except Exception as exc:
            return False, f"{exc} {_CANARY_UNRUNNABLE}"
    return rc == 0, output


# A tool-surface inventory that reports an empty MCP server list (the expected strict-isolation
# state). Substrings Codex prints when nothing is configured.
_EMPTY_INVENTORY_MARKERS: tuple[str, ...] = ("no mcp servers", "no servers", "[]")


def run_codex_capability_smoke(
    *,
    command: str,
    home_dir: Path,
    env: Mapping[str, str],
    permission_profile: str = "workspace-write",
    system: str | None = None,
    runner: CanaryRunner = default_canary_runner,
    inventory_probe: InventoryProbe | None = None,
    read_isolation_off: bool = False,
) -> CapabilitySmokeReport:
    """No-model, real-``codex sandbox`` capability smoke for the generated ``worc`` profile.

    Stands up a throwaway fixture under *home_dir* — which MUST be a real, non-``/tmp`` path, since
    ``codex sandbox`` always grants the system temp root and that would mask every carve-out — with
    a workspace repo file, a private ``.worc`` file, an exchange ``.worc-io`` file, and (POSIX,
    best-effort) a workspace symlink resolving to the private file. It generates the real profile
    for *permission_profile* and runs the full probe battery through :func:`run_codex_canary`
    (private denied direct+shell+alias, repo-read positive control, repo write per profile, exchange
    read/write), then records a no-model tool-surface inventory (``codex mcp list``). Returns a
    :class:`CapabilitySmokeReport` whose ``status`` distinguishes ``passed`` / ``unsupported``
    (``CAPABILITY_UNAVAILABLE``) / ``policy-failed`` (``CONFIGURATION_ERROR``) — never silently
    downgrading. Reusable by ``worc preflight`` (H7) and the local/manual host smoke; the
    deterministic suite injects a scripted *runner* + *inventory_probe* so no real sandbox spawns.
    """
    sys_name = system if system is not None else platform.system()
    root = Path(tempfile.mkdtemp(prefix="worc-cap-smoke-", dir=str(home_dir)))
    try:
        repo = root / "repo"
        # Runtime-home dirnames come from runtime_layout (WRI-004 AST guard: no hand-joined
        # ``.worc`` / ``.worc-io`` literal), so the fixture mirrors the real layout by construction.
        control = repo / CONTROL_HOME_DIRNAME
        exchange = repo / EXCHANGE_HOME_DIRNAME
        (control / "logs").mkdir(parents=True)
        (exchange / "t").mkdir(parents=True)
        (repo / "src").mkdir(parents=True)
        private_file = control / "logs" / "req.json"
        private_file.write_text("PRIVATE_SECRET", encoding="utf-8")
        exchange_file = exchange / "t" / "task.md"
        exchange_file.write_text("EXCHANGE_TASK", encoding="utf-8")
        repo_file = repo / "src" / "main.py"
        repo_file.write_text("print(1)", encoding="utf-8")
        # A workspace alias resolving to the private file — the deny must hold through it (README
        # "canaries include workspace aliases"). Best-effort: skipped + reported if the host cannot
        # create the link (e.g. unprivileged Windows), never a silent pass.
        alias_probe: str | None = None
        alias_note = ""
        alias = repo / "src" / "alias_to_private"
        try:
            alias.symlink_to(private_file)
            alias_probe = str(alias)
        except (OSError, NotImplementedError):
            alias_note = " (alias probe skipped: host could not create a symlink fixture)"

        writable = permission_profile == "workspace-write"
        deny = InternalDenyPolicy(
            control_home=control,
            private_home=control,
            env_file=None,
            provider_homes=(),
        )
        write_guard = ProviderWriteGuardPolicy(
            exchange_root=exchange,
            git_dir=repo / ".git",
            git_common_dir=repo / ".git",
            hooks_dir=repo / ".git" / "hooks",
            tasks_dir=repo / "tasks",
        )
        profile = build_codex_permission_profile(
            permission_profile=permission_profile,
            working_directory=str(repo),
            deny_policy=deny,
            write_guard=write_guard if writable else None,
            denied_read_paths=(),
            read_isolation_off=read_isolation_off,
        )
        outcome = run_codex_canary(
            command=command,
            profile_arg=render_permission_profile_arg(profile),
            working_directory=str(repo),
            private_probe=str(private_file),
            exchange_probe=str(exchange_file),
            env=env,
            system=sys_name,
            runner=runner,
            private_readable=read_isolation_off,
            extra=ExtraProbes(
                repo_probe=str(repo_file),
                alias_probe=alias_probe,
                repo_write_probe=str(repo_file),
                repo_writable=writable,
            ),
        )
        evidence = list(outcome.evidence)
        inv_ok, inv_out = (
            inventory_probe(command, env)
            if inventory_probe is not None
            else _inventory_via_runner(runner, command, env)
        )
        inv_empty = inv_ok and any(m in inv_out.lower() for m in _EMPTY_INVENTORY_MARKERS)
        evidence.append({"probe": "mcp-inventory", "clean_exit": inv_ok, "empty": inv_empty})
    finally:
        shutil.rmtree(root, ignore_errors=True)

    prefix = f"codex {permission_profile} sandbox"
    if outcome.ok:
        inv = "empty MCP inventory" if inv_empty else "MCP inventory NOT confirmed empty"
        return CapabilitySmokeReport(
            CAPABILITY_PASSED, f"{prefix}: OS-enforced ({inv}){alias_note}", tuple(evidence)
        )
    if outcome.error_class is ErrorClass.CONFIGURATION_ERROR:
        return CapabilitySmokeReport(
            CAPABILITY_POLICY_FAILED, f"{prefix}: {outcome.message}", tuple(evidence)
        )
    return CapabilitySmokeReport(
        CAPABILITY_UNSUPPORTED, f"{prefix}: {outcome.message}{alias_note}", tuple(evidence)
    )
