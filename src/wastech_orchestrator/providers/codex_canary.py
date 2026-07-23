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

import shlex
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from wastech_orchestrator.providers.base import ErrorClass
from wastech_orchestrator.providers.codex_profile import PROFILE_NAME
from wastech_orchestrator.providers.process import run_process

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
    *, private_probe: str, exchange_probe: str | None, system: str
) -> list[CanaryProbe]:
    """The probe set: private reads (direct + shell-mediated) must be denied; the exchange, if a
    file is available, must be readable but not writable."""
    probes = [
        CanaryProbe("private-read-denied", _read_cmd(private_probe, system), expect_denied=True),
        CanaryProbe(
            "private-shell-read-denied", _shell_read_cmd(private_probe, system), expect_denied=True
        ),
    ]
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
) -> CanaryOutcome:
    """Prove the profile's deny/read-only boundary via ``codex sandbox`` before ``codex exec``.

    Returns a :class:`CanaryOutcome`; the adapter raises the mapped ``ProviderError``. A leak (a
    denied path readable/writable) is a non-fallback ``CONFIGURATION_ERROR``; an unrunnable /
    undemonstrable sandbox is a ``CAPABILITY_UNAVAILABLE``. Records each probe's verdict as
    redaction-safe evidence (paths only, never file contents).
    """
    evidence: list[dict[str, object]] = []
    probes = build_canary_probes(
        private_probe=private_probe, exchange_probe=exchange_probe, system=system
    )
    for probe in probes:
        argv = build_canary_command(command, profile_arg, working_directory, probe)
        try:
            rc, output = runner(argv, working_directory, env)
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
                    f"permission-profile canary FAILED: {probe.label!r} was expected to be denied "
                    "but succeeded — the profile is not enforcing (security violation)"
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
    return CanaryOutcome(ok=True, evidence=tuple(evidence))
