"""No-model effective-policy canary for the Codex permission profile.

Before ``codex exec`` starts, prove the generated profile is actually enforced on THIS host/CLI by
running the *same* profile under ``codex sandbox -P`` (a no-model sandbox runner) and
checking that internal paths are denied and the exchange is read-only. ``codex sandbox`` applies
exactly the profile ``codex exec`` selects via ``default_permissions`` — one definition, two
selectors — so a pass here is real OS-enforcement evidence, not prompt hygiene.

Classification (mirrors the provider error-class split):

* a **denied path that turned out readable/writable** (a real leak) → ``CONFIGURATION_ERROR``, a
  non-fallback security result: the profile is not enforcing and no other provider should be tried;
* a **profile that refuses to execute the provider's own binary** (the exec probe) →
  ``CONFIGURATION_ERROR`` too: the capability provably exists — the denied binary launched the
  probe — so only our profile (or a policy over it) can be taking it away, and falling back would
  mask a break that is ours to fix;
* the **sandbox could not run / could not demonstrate the requested policy** on this host (Codex
  itself refuses to run unsandboxed on native Windows when it cannot enforce a split policy; a Linux
  host missing its sandbox helper; an allowed path wrongly blocked) → ``CAPABILITY_UNAVAILABLE``, a
  deterministic pre-model infrastructure result the Router may only fall over to a same-or-stricter,
  self-isolating provider for.

The read/write probes touch only files that already exist (the attempt's own ``request.json`` under
the private home; the frozen task packet in the exchange), so the canary never mutates the curated
exchange; the exec probe runs the provider CLI's own ``--version``, the same re-exec shape
``apply_patch``'s fs sandbox helper uses.
"""

from __future__ import annotations

import ntpath
import platform
import shlex
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from wastech_orchestrator.providers._adapter_base import (
    CAPABILITY_PASSED,
    CAPABILITY_POLICY_FAILED,
    CAPABILITY_UNSUPPORTED,
)
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
    "restricted read-only access requires the elevated windows sandbox backend",
    "helper copy failed",
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
    """One sandbox probe: run ``command`` under the profile; ``expect_denied`` is the verdict.

    ``cleanup_path`` is set for a probe that writes to a path that did not exist before it: if the
    deny did not hold, the file the probe created is the orchestrator's litter, so it removes it
    before failing the attempt closed. Probes that append to a pre-existing file leave it ``None`` —
    there is nothing to delete and the file is evidence.

    ``denied_error_class`` is what a denial of an ``expect_denied=False`` probe proves. The default
    keeps today's reading — a required read was blocked, a host-capability gap. The exec probe sets
    ``CONFIGURATION_ERROR``: the capability provably exists (the denied binary is the very
    executable that launched the probe), so a refusal means the generated profile — or a policy
    layered over it — took it away, and that is a non-fallback configuration error, not a host gap.
    """

    label: str
    command: list[str]
    expect_denied: bool
    cleanup_path: str | None = None
    denied_error_class: ErrorClass = ErrorClass.CAPABILITY_UNAVAILABLE


@dataclass(frozen=True)
class ExtraProbes:
    """Optional probes added beyond the per-attempt private/exchange set.

    Grouped into one value so :func:`run_codex_canary` stays within the argument-count ratchet: a
    workspace repo read (the mandatory positive control), a workspace symlink alias resolving to the
    private file (must stay denied), a repo write (allowed for ``workspace-write``, denied for
    ``read-only``), and the write-guard roots.

    ``write_guard_probes`` is ``(label, sentinel path)`` per Git-control / lifecycle root the
    profile declares write-denied — the product's central claim ("the agent cannot change
    ``.git``"), which until now no probe on any provider tested. Built by
    :func:`write_guard_probe_paths`, which decides *which* roots are worth a probe launch. Both the
    capability smoke and the per-attempt canary pass them; the other three fields are smoke-only.
    """

    repo_probe: str | None = None
    alias_probe: str | None = None
    repo_write_probe: str | None = None
    repo_writable: bool = False
    write_guard_probes: tuple[tuple[str, str], ...] = ()


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


# Every Windows probe keeps the path as its **own argv member** rather than pre-formatting it into a
# single `cmd /c "<command line>"` string. The safe runner quotes an individual argument correctly,
# but `cmd /c` re-parses a one-string command line and splits it on spaces — so a workspace under,
# say, `C:\Users\First Last\...` made the probe fail for a *malformed command*, not because the
# sandbox denied it. On an `expect_denied=True` probe that reads as enforcement and masks
# non-enforcement, which is why this must stay argv-shaped (inner `"..."` quoting does not help:
# `cmd` then chokes on the nested quotes added around the whole string).


def _native(path: str, system: str) -> str:
    """Normalize a probe path to the target OS's separators.

    Probe paths reach the canary in mixed form: the private probe is a native path, while the
    exchange probe (``AgentRunRequest.task_path``) is POSIX-shaped even on Windows. ``cmd`` resolves
    neither ``type C:/a/b`` nor ``echo x >> C:/a/b`` — it reports the file as missing — so an
    unnormalized path fails the probe for a *malformed command* rather than a policy verdict: the
    required read reads as a capability gap, and the paired ``expect_denied=True`` write reads as
    enforcement, masking a real leak. Keyed on the injected ``system``, not the host, so the
    deterministic suite exercises the Windows shape from any platform.
    """
    return ntpath.normpath(path) if system == "Windows" else path


def _read_cmd(path: str, system: str) -> list[str]:
    if system == "Windows":
        return ["cmd", "/c", "type", _native(path, system)]
    return ["/bin/cat", path]


def _shell_read_cmd(path: str, system: str) -> list[str]:
    """A shell-mediated read — proves the OS layer blocks *any* program, not one tool.

    On Windows this coincides with :func:`_read_cmd`: ``cmd`` *is* the shell there and ``type`` is
    one of its builtins, so the read is already shell-mediated and there is no separate direct-exec
    form to contrast it with (the POSIX pair contrasts ``/bin/cat`` with ``/bin/sh -c cat``).
    """
    if system == "Windows":
        return ["cmd", "/c", "type", _native(path, system)]
    return ["/bin/sh", "-c", f"cat {shlex.quote(path)}"]


def _write_cmd(path: str, system: str) -> list[str]:
    if system == "Windows":
        # `>>` carries no space, so it survives `cmd`'s re-parse as a redirection operator while the
        # path stays a separately quoted argv member.
        return ["cmd", "/c", "echo", "x", ">>", _native(path, system)]
    return ["/bin/sh", "-c", f"printf x >> {shlex.quote(path)}"]


#: The file a write-guard probe tries to create inside a write-denied root. A name nothing else
#: uses, and a *new* file rather than an append to an existing one: appending "x" to ``.git/HEAD``
#: would corrupt the repository on the very host where the deny failed to hold.
WRITE_GUARD_SENTINEL = "worc-write-guard-probe"


@dataclass(frozen=True)
class WriteGuardTargets:
    """Which declared write-deny roots a probe can actually speak to, and why the rest cannot.

    ``probes`` is ``(label, sentinel path)`` per root that gets its own probe launch. ``covered``
    names roots skipped because a probed ancestor already proves the deny (``.git/hooks`` under
    ``.git`` in a normal clone) — paired as ``(root, ancestor)`` so the reason is inspectable rather
    than implied. ``missing`` names roots whose directory is absent: writing into a directory that
    does not exist fails for want of a parent, and counting that as an enforced deny is exactly how
    a probe suite certifies a policy nobody applied.
    """

    probes: tuple[tuple[str, str], ...]
    covered: tuple[tuple[str, str], ...]
    missing: tuple[str, ...]


def write_guard_probe_paths(denied_write_paths: Sequence[Path]) -> WriteGuardTargets:
    """Pick the probe target inside each declared write-deny root.

    Every root either gets a probe of its own or is accounted for: collapsed into a probed ancestor,
    or reported missing. Nothing is dropped silently, because "no probe ran" and "the deny held" are
    the two answers a floor claim must never confuse. The one host question — does this root exist
    as a directory — is asked directly; the deterministic suite creates real directories, so an
    injection seam for it would have no caller.

    Labels are derived from the root's own name, so a linked worktree — where the per-worktree
    gitdir and the shared common dir are different directories — yields two distinguishable probes
    rather than one that could pass while the other root stays wide open.
    """
    probes: list[tuple[str, str]] = []
    covered: list[tuple[str, str]] = []
    missing: list[str] = []
    kept: list[Path] = []
    for root in denied_write_paths:
        ancestor = next((k for k in kept if _is_within(root, k)), None)
        if ancestor is not None:
            covered.append((root.as_posix(), ancestor.as_posix()))
            continue
        if not root.is_dir():
            missing.append(root.as_posix())
            continue
        kept.append(root)
        probes.append((f"write-guard-{_root_label(root)}-denied", str(root / WRITE_GUARD_SENTINEL)))
    return WriteGuardTargets(tuple(probes), tuple(covered), tuple(missing))


def _is_within(path: Path, ancestor: Path) -> bool:
    """Whether *path* sits inside *ancestor* (lexical, both already absolute and resolved)."""
    return path != ancestor and ancestor in path.parents


def _root_label(root: Path) -> str:
    """A stable, human-readable probe label for a deny root — its last two path segments.

    Two segments rather than one because the interesting pair is ``.git`` versus
    ``.git/worktrees/<name>``: bare directory names would collide or read as the same root.
    """
    parts = [part for part in root.parts if part not in ("/", "\\")][-2:]
    return "-".join(part.strip(":").replace(".", "").lower() or "root" for part in parts) or "root"


def build_canary_probes(
    *,
    private_probe: str,
    exchange_probe: str | None,
    system: str,
    repo_probe: str | None = None,
    alias_probe: str | None = None,
    repo_write_probe: str | None = None,
    repo_writable: bool = False,
    write_guard_probes: Sequence[tuple[str, str]] = (),
    exec_probe: str | None = None,
) -> list[CanaryProbe]:
    """The probe set for the profile under test.

    Private reads (direct + shell-mediated, and — when *alias_probe* is a workspace symlink/hard
    link that resolves to the private file — through that alias) must be denied. *repo_probe*, when
    given, is the **positive control**: a workspace read that MUST succeed, so a broken probe
    harness (every command failing) can no longer masquerade as "everything denied → enforcing".
    *repo_write_probe* proves the profile's write level (allowed for ``workspace-write``, denied for
    ``read-only``). The exchange, when a file is available, must be readable but not writable — and
    also serves as a positive control on the per-attempt path where no *repo_probe* is supplied.

    The private-read expectation does not depend on read-isolation: the profile denies that set at
    every setting, so the probes assert a denial unconditionally.

    ``write_guard_probes`` adds one write-deny probe per Git-control / lifecycle root the profile
    carves out (see :func:`write_guard_probe_paths`). These are the probes behind the product's
    central claim — the agent cannot change ``.git`` — which no probe tested before: each writes a
    sentinel file into the root and expects to be refused.

    ``exec_probe`` (the provider CLI's own launch path) adds an exec probe: the binary must
    *execute* under the profile, because ``apply_patch`` re-execs it inside the sandbox as its fs
    helper — the one capability every read/append probe above misses, and the one a deny over
    ``$CODEX_HOME`` silently broke on hosts where the standalone package keeps the binary inside
    that home. Placed last in the base set so a generically broken harness/host is classified first
    by the weaker probes; its denial escalates to ``CONFIGURATION_ERROR`` only on the selective
    signature — reads work, exec of the provider's own binary does not.
    """
    probes = [
        CanaryProbe("private-read-denied", _read_cmd(private_probe, system), expect_denied=True),
        CanaryProbe(
            "private-shell-read-denied",
            _shell_read_cmd(private_probe, system),
            expect_denied=True,
        ),
    ]
    if alias_probe is not None:
        probes.append(
            CanaryProbe(
                "private-alias-read-denied", _read_cmd(alias_probe, system), expect_denied=True
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
    if exec_probe is not None:
        probes.append(
            CanaryProbe(
                "cli-exec-allowed",
                [exec_probe, "--version"],
                expect_denied=False,
                denied_error_class=ErrorClass.CONFIGURATION_ERROR,
            )
        )
    for label, sentinel in write_guard_probes:
        probes.append(
            CanaryProbe(
                label, _write_cmd(sentinel, system), expect_denied=True, cleanup_path=sentinel
            )
        )
    return probes


def _remove_probe_litter(probe: CanaryProbe) -> str:
    """Delete the file a write probe created when the deny did not hold; describe what happened.

    The attempt is about to fail closed, so nothing will consume the file — but it is the
    orchestrator's litter inside the operator's repository (or its Git directory), and leaving it
    there is both untidy and, in ``.git``, actively confusing. Best-effort by construction: a
    failure to remove it is reported in the same message rather than raised, because the security
    verdict must not depend on cleanup succeeding.
    """
    if probe.cleanup_path is None:
        return ""
    target = Path(probe.cleanup_path)
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        return f"; the file the probe created could not be removed ({exc})"
    return f"; removed the file the probe created ({target.name})"


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


@contextmanager
def _sandbox_probe_env(env: Mapping[str, str], system: str) -> Iterator[dict[str, str]]:
    """Yield the environment in which ``codex sandbox`` can exercise the generated profile.

    Native Windows stores the sandbox accounts, helper, credentials, and capability grants in
    ``CODEX_HOME``. Replacing that home would remove the grant substrate the canary is meant to
    test. The generated ``permissions.worc`` table is still supplied as an inline ``-c`` override
    and selected explicitly with ``-P``, so operator configuration cannot replace the tested
    profile. POSIX sandbox backends keep no such state in ``CODEX_HOME`` and retain the stronger
    empty-home isolation from operator configuration.
    """
    if system == "Windows":
        yield dict(env)
        return
    with tempfile.TemporaryDirectory(prefix="worc-codexhome-") as codex_home:
        yield {**dict(env), "CODEX_HOME": codex_home}


def _classify_probe(
    probe: CanaryProbe,
    *,
    denied: bool,
    lowered: str,
    evidence: list[dict[str, object]],
) -> CanaryOutcome | None:
    """One probe's failing verdict, or ``None`` when the probe holds and the loop continues.

    Order matters and is part of the contract. A probe whose denial is itself the verdict
    (``denied_error_class`` = ``CONFIGURATION_ERROR``, the exec probe) is judged BEFORE the
    capability-marker scan: enforcement prose in the refusal output must not reroute it to a
    capability gap — on macOS the refused exec's own stderr carries seatbelt/sandbox wording, which
    is exactly how this break was masked as "Codex is unavailable today" while the router burned
    five runs falling back. Only the runner's own marker (``codex sandbox`` itself failed to launch
    or timed out) stays a host verdict for that probe.
    """
    if (
        denied
        and not probe.expect_denied
        and probe.denied_error_class is ErrorClass.CONFIGURATION_ERROR
        and _CANARY_UNRUNNABLE not in lowered
    ):
        return CanaryOutcome(
            ok=False,
            error_class=ErrorClass.CONFIGURATION_ERROR,
            message=(
                f"permission-profile canary FAILED: {probe.label!r} — the sandbox refused "
                f"to execute the provider's own binary ({probe.command[0]}), which "
                "provably runs on this host: it is the same executable that launched this "
                "probe. The generated profile (or a policy layered over it) is taking the "
                "capability away — Codex's apply_patch re-execs this binary as its fs "
                "sandbox helper and would fail every patch the same way"
            ),
            evidence=tuple(evidence),
        )
    capability_marker = next((marker for marker in _CAPABILITY_MARKERS if marker in lowered), None)
    if capability_marker is not None:
        return CanaryOutcome(
            ok=False,
            error_class=ErrorClass.CAPABILITY_UNAVAILABLE,
            message=(
                f"codex sandbox could not enforce the permission profile on this host "
                f"(probe {probe.label!r}); the requested isolation cannot be demonstrated "
                f"({capability_marker})"
            ),
            evidence=tuple(evidence),
        )
    if probe.expect_denied and not denied:
        removed = _remove_probe_litter(probe)
        return CanaryOutcome(
            ok=False,
            error_class=ErrorClass.CONFIGURATION_ERROR,
            message=(
                f"permission-profile canary FAILED: {probe.label!r} was expected to be "
                "denied but succeeded — the profile is not enforcing (security "
                f"violation){removed}"
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
    return None


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
) -> CanaryOutcome:
    """Prove the profile's deny/read-only boundary via ``codex sandbox`` before ``codex exec``.

    Returns a :class:`CanaryOutcome`; the adapter raises the mapped ``ProviderError``. A leak (a
    denied path readable/writable) is a non-fallback ``CONFIGURATION_ERROR``; an unrunnable /
    undemonstrable sandbox is a ``CAPABILITY_UNAVAILABLE``. Records each probe's verdict as
    redaction-safe evidence (paths only, never file contents).

    Two hardening guarantees: (1) on POSIX the probes run under a throwaway
    ``CODEX_HOME`` so the operator's ``~/.codex/config.toml`` cannot alter profile resolution; on
    native Windows they retain the caller's home because it contains the sandbox grant substrate,
    while the inline ``-c permissions.worc={...}`` override and explicit ``-P worc`` selection keep
    the generated profile authoritative; (2) the canary refuses to return ``ok`` unless at least
    one **positive control** — an ``expect_denied=False`` read — actually succeeded, so a broken
    probe harness (every command failing) can never be mistaken for a fully-enforcing sandbox.
    """
    evidence: list[dict[str, object]] = []
    probes = build_canary_probes(
        private_probe=private_probe,
        exchange_probe=exchange_probe,
        system=system,
        repo_probe=extra.repo_probe,
        alias_probe=extra.alias_probe,
        repo_write_probe=extra.repo_write_probe,
        repo_writable=extra.repo_writable,
        write_guard_probes=extra.write_guard_probes,
        exec_probe=command,
    )
    saw_positive_control = False
    with _sandbox_probe_env(env, system) as probe_env:
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
            verdict = _classify_probe(probe, denied=denied, lowered=lowered, evidence=evidence)
            if verdict is not None:
                return verdict
            # The positive control screens the *read* harness: the deny probes are reads, and the
            # documented failure class it guards against (a read that looks like enforcement) is
            # screened only by an allowed probe of the same shape. A successful exec proves the
            # exec capability, not read-selectivity, so the exec probe does not certify the deny
            # verdicts.
            saw_positive_control = saw_positive_control or (
                not probe.expect_denied
                and probe.denied_error_class is not ErrorClass.CONFIGURATION_ERROR
            )
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


# --- No-model capability smoke (worc preflight / host gate) -------------------------------------

# Smoke verdicts. Re-exported from :mod:`_adapter_base`, where both adapters' probes read them, so
# preflight cannot end up with two vocabularies for one question: ``unsupported`` maps to the
# pre-model ``CAPABILITY_UNAVAILABLE`` classification, ``policy-failed`` to the non-fallback
# ``CONFIGURATION_ERROR`` security result.

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
    servers; this records the effective inventory as evidence for tool-surface inspection.
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
    strict_isolation: bool = True,
    system: str | None = None,
    runner: CanaryRunner = default_canary_runner,
    inventory_probe: InventoryProbe | None = None,
) -> CapabilitySmokeReport:
    """No-model, real-``codex sandbox`` capability smoke for the generated ``worc`` profile.

    Stands up a throwaway fixture under *home_dir* — which MUST be a real, non-``/tmp`` path, since
    ``codex sandbox`` always grants the system temp root and that would mask every carve-out — with
    a workspace repo file, a private ``.worc`` file, an exchange ``.worc-io`` file, and (POSIX,
    best-effort) a workspace symlink resolving to the private file. It generates the real profile
    for *permission_profile* and runs the full probe battery through :func:`run_codex_canary`
    (private denied direct+shell+alias, repo-read positive control, repo write per profile, exchange
    read/write, an exec of the CLI binary itself, and a write into every declared Git-control /
    lifecycle root), then records a
    no-model tool-surface inventory (``codex mcp list``). Returns a
    :class:`CapabilitySmokeReport` whose ``status`` distinguishes ``passed`` / ``unsupported``
    (``CAPABILITY_UNAVAILABLE``) / ``policy-failed`` (``CONFIGURATION_ERROR``) — never silently
    downgrading. Reusable by ``worc preflight`` and the local/manual host smoke; the
    deterministic suite injects a scripted *runner* + *inventory_probe* so no real sandbox spawns.

    ``strict_isolation`` is the operator's own setting and is passed to the profile generator, so
    what gets proven here is the profile that will actually launch. With it ``false`` (the advanced
    mode) that profile grants ``write`` on the whole volume, which is exactly the configuration
    whose carve-outs are worth demonstrating: a smoke that quietly proved the stricter profile
    instead would report a floor nobody runs under.
    """
    sys_name = system if system is not None else platform.system()
    root = Path(tempfile.mkdtemp(prefix="worc-cap-smoke-", dir=str(home_dir)))
    try:
        repo = root / "repo"
        # Runtime-home dirnames come from runtime_layout (an AST guard forbids hand-joined
        # ``.worc`` / ``.worc-io`` literal), so the fixture mirrors the real layout by construction.
        control = repo / CONTROL_HOME_DIRNAME
        exchange = repo / EXCHANGE_HOME_DIRNAME
        (control / "logs").mkdir(parents=True)
        (exchange / "t").mkdir(parents=True)
        (repo / "src").mkdir(parents=True)
        # Real targets for the write-guard probes, created before anything runs. A probe that writes
        # into a directory that does not exist fails for want of a parent, and that failure is
        # indistinguishable from an enforced deny — so a fixture missing these would certify a floor
        # nobody applied. The gitdir doubles as the common dir here (a normal clone collapses them);
        # the linked-worktree shape, where they differ, is covered by the deterministic suite.
        git_dir = repo / ".git"
        (git_dir / "hooks").mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (repo / "tasks").mkdir(parents=True)
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
        )
        write_guard = ProviderWriteGuardPolicy(
            exchange_root=exchange,
            git_dir=git_dir,
            git_common_dir=git_dir,
            hooks_dir=git_dir / "hooks",
            tasks_dir=repo / "tasks",
        )
        profile = build_codex_permission_profile(
            permission_profile=permission_profile,
            working_directory=str(repo),
            deny_policy=deny,
            write_guard=write_guard if writable else None,
            denied_read_paths=(),
            # Same profile the attempt would launch under, network included: the advanced mode is
            # online, and proving a profile that differs from the real one in any key is what this
            # check exists to stop. The probes are local commands either way.
            network_access=not strict_isolation,
            strict_isolation=strict_isolation,
        )
        # Assert the fixture before trusting its verdict: a root with no directory yields no probe,
        # and a smoke that quietly probed fewer roots than the profile declares would certify a
        # floor it never tested. Undemonstrable, therefore — never ``passed``.
        targets = write_guard_probe_paths(write_guard.denied_write_paths)
        if targets.missing:
            return CapabilitySmokeReport(
                CAPABILITY_UNSUPPORTED,
                f"codex {permission_profile} sandbox: the smoke fixture is incomplete — no "
                f"directory for the write-deny root(s) {', '.join(targets.missing)}, so their deny "
                "cannot be demonstrated (a write there would fail for want of a parent)",
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
            extra=ExtraProbes(
                repo_probe=str(repo_file),
                alias_probe=alias_probe,
                repo_write_probe=str(repo_file),
                repo_writable=writable,
                write_guard_probes=targets.probes,
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
