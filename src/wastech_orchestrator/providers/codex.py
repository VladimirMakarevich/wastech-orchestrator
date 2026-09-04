"""CodexProvider — the Codex CLI adapter.

Implements the :class:`~wastech_orchestrator.providers.base.AgentProvider` contract for
``id = "codex"`` using ``codex exec`` (OpenAI Codex CLI). This is the **only** module that knows
Codex syntax; it composes the provider-agnostic infrastructure (process runner, env allowlist,
redaction, artifacts, error normalization).

Invariants: the adapter performs **no fallback** and **never**
touches the state machine; it never commits/pushes/PRs. It raises
:class:`~wastech_orchestrator.providers.base.ProviderError` (with the right
:class:`~wastech_orchestrator.providers.base.ErrorClass`) for infrastructure failures, and returns
``AgentRunResult(status=failed, error=task_failure)`` for a clean run that did not satisfy the task.
The CLI is launched as an argv list (no shell); the prompt is fed on stdin; context reaches Codex
only as file paths.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.providers._adapter_base import (
    CAPABILITY_POLICY_FAILED,
    BaseCliProvider,
    IsolationCapabilityReport,
    ParsedEvents,
    coerce_usage_int,
    read_text,
)
from wastech_orchestrator.providers.artifacts import ArtifactPaths
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AuthProbe,
    AuthState,
    ErrorClass,
    NormalizedError,
    NormalizedUsage,
    ProviderError,
    ProviderId,
    UsageScope,
    build_context_footer,
    build_effective_prompt,
)
from wastech_orchestrator.providers.capabilities import normalize_codex_reasoning
from wastech_orchestrator.providers.codex_canary import (
    CanaryRunner,
    ExtraProbes,
    default_canary_runner,
    run_codex_canary,
    run_codex_capability_smoke,
    write_guard_probe_paths,
)
from wastech_orchestrator.providers.codex_profile import (
    PROFILE_NAME,
    build_codex_permission_profile,
    render_permission_profile_arg,
    toml_basic_string,
)
from wastech_orchestrator.providers.errors import (
    StderrSignature,
    make_signatures,
    message_for,
)
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.runtime_layout import InternalDenyPolicy
from wastech_orchestrator.security.env import build_child_env
from wastech_orchestrator.security.forbidden_args import find_forbidden_args
from wastech_orchestrator.security.shell_reach import ShellQuery

__all__ = [
    "CodexProvider",
    "ParsedEvents",
    "attempt_has_shell",
    "build_codex_argv",
    "build_context_footer",
    "build_effective_prompt",
    "isolation_reasons",
    "parse_events",
    "resolve_codex_resources_dir",
]

_LOG = logging.getLogger(__name__)

_DEFAULT_PROFILE = "workspace-write"
_LAST_MESSAGE_FILENAME = "last-message.txt"
_OUTPUT_SCHEMA_FILENAME = "output-schema.json"

# Codex feature flags disabled for an autonomous orchestrator attempt: the non-shell tool
# surfaces that could reach the local filesystem or spawn work outside the profiled shell
# sandbox — hooks, custom subagents/multi-agent, computer use, the browser surfaces, apps/plugins,
# and the persistent memory store. Each is emitted as ``--disable <name>``. Emission is conditional
# twice over: ``hooks`` is not disabled when read-isolation is off — that is, on the shipped
# default — and NOTHING is disabled in the advanced mode, where these surfaces are handed back
# deliberately: in a mode whose whole point is full freedom under the operator's responsibility,
# withholding them would be a floor control that buys nothing and a plain loss of function.
#
# The set is grounded in a live no-model inventory, not in guesswork: ``codex features list``
# enumerates the flags and their enabled state, and ``codex sandbox --disable <name>`` validates
# every name here while rejecting an invented one ("Unknown feature flag"). The rule for extending
# it: only where an enabled flag is a distinct surface that really executes something or reaches
# data. That is what puts the two extra browser surfaces here (an EXTERNAL browser and full CDP
# access reach the operator's own browser session, and neither passes through the profiled shell)
# and ``memories`` (a persistent store outside this orchestrator's redaction net and audit).
# Deliberately NOT added, so the next reader does not have to re-derive it: ``unified_exec``
# is the profiled shell itself; ``plugin_sharing``/``remote_plugin`` are sub-surfaces of the
# already-denied ``plugins``; ``enable_mcp_apps`` and ``standalone_web_search`` ship disabled;
# MCP elicitation is neutralized by ``--ignore-user-config`` plus the untrusted project layer;
# ``code_mode_host`` is enabled but its semantics are not established here, so it stays a watch
# item rather than a blind deny. Widening a deny without a proven surface is the functional
# over-restriction the security rules forbid.
#
# The MCP inventory is neutralized separately, by ``--ignore-user-config`` + the untrusted project
# layer (no server loads); the no-model capability smoke
# (:func:`codex_canary.run_codex_capability_smoke`, run by ``worc preflight`` / the host gate)
# records the effective ``codex mcp list`` inventory as evidence. The per-attempt canary proves the
# filesystem deny/read-only boundary only — it makes no MCP-inventory claim.
_DISABLED_FEATURES: tuple[str, ...] = (
    "hooks",
    "multi_agent",
    "computer_use",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "in_app_browser",
    "memories",
    "apps",
    "plugins",
)

# Authority-bearing Codex flags an operator may NOT supply through config/flow ``extra_args``: they
# would select, replace, or weaken the permission profile, config-isolation, workspace, tool, or
# network policy the adapter owns. Distinct from the cross-provider absolute bans in
# ``security.forbidden_args`` (``--dangerously*`` / ``--yolo`` / ``--ignore-rules``, and any
# ``--sandbox`` that is valueless or selects full access) — both clusters are rejected regardless of
# ``strict_isolation``.
_RESERVED_CODEX_FLAGS: frozenset[str] = frozenset(
    {
        "-c",
        "--config",
        "-p",
        "--profile",
        "-P",
        "--permission-profile",
        "-s",
        "--sandbox",
        # Approval/sandbox-mode selectors: ``--full-auto`` turns on ``--sandbox workspace-write``
        # (and an auto approval policy), and any ``-a``/``--ask-for-approval`` overrides the
        # ``never`` policy the adapter owns. Selecting a ``--sandbox`` mode makes Codex stop
        # applying our generated ``default_permissions="worc"`` profile, so the private-file read
        # denials (``.worc``/``.env``/``state.db``) silently vanish — the isolation this cluster
        # exists to enforce. Reserved regardless of ``strict_isolation``.
        "--full-auto",
        "-a",
        "--ask-for-approval",
        "--add-dir",
        "--ignore-user-config",
        "--ignore-rules",
        "--enable",
        "--disable",
        "--oss",
        "--local-provider",
        "--skip-git-repo-check",
        "--ephemeral",
        "--strict-config",
        "-C",
        "--cd",
        "--output-schema",
        "--json",
        "-o",
        "--output-last-message",
        "--color",
    }
)

# The Windows sandbox helper and its package layout. On Windows ``workspace-write``, Codex launches
# this helper BY NAME, so the directory holding it must be discoverable on the child ``PATH``. The
# orchestrator rebuilds a clean allowlisted env that drops it, so
# :func:`resolve_codex_resources_dir` locates the Codex standalone package's ``codex-resources``
# directory and the adapter prepends it.
_SANDBOX_HELPER_EXE = "codex-windows-sandbox-setup.exe"
_PACKAGE_MANIFEST_NAME = "codex-package.json"
_DEFAULT_RESOURCES_DIRNAME = "codex-resources"

# A fatal Windows-sandbox failure on stderr — either the setup helper could not be launched OR the
# sandbox could not spawn a child process at run time (seclogon ``CreateProcessWithLogonW`` failing
# on every command and on the ``apply_patch`` write path). Codex can still print a clean terminal
# SUCCESS event (exit 0) while this error means the run never touched the workspace, so it is
# matched both as an ordinary ``PERMISSION_DENIED`` stderr signature (the nonzero-exit path) and by
# the post-success guard (``_post_success_infra_error``) that flips a false success into an infra
# failure so the Router falls over to the other provider.
_HELPER_LAUNCH_FAILED_PATTERN = (
    r"orchestrator_helper_launch_failed"
    r"|codex-windows-sandbox-setup\.exe"
    r"|setup refresh failed to launch helper"
    r"|CreateProcessWithLogonW failed"  # seclogon could not spawn the sandbox child at run time
    r"|fs sandbox helper failed"  # the same failure on the apply_patch (write) path
    r"|windows sandbox(?: failed)?:"  # the general Codex windows-sandbox error prefix
)
_HELPER_LAUNCH_FAILED_RE = re.compile(_HELPER_LAUNCH_FAILED_PATTERN, re.IGNORECASE)

# Statuses on a terminal Codex ``result`` event that mark the turn as NOT having satisfied the task.
# Any other status (incl. a missing one) is treated as a completed run — task quality is judged
# later by the orchestrator's review/checks, not by the adapter.
_FAILURE_STATUSES = frozenset({"error", "failed", "failure", "incomplete", "aborted"})

# Codex stderr signatures → normalized error classes (most specific first).
_CODEX_SIGNATURES = make_signatures(
    [
        (
            ErrorClass.SESSION_UNAVAILABLE,
            (
                r"session not found|no such session|unknown session|conversation not found"
                r"|no conversation with|thread not found|cannot resume"
            ),
        ),
        (
            ErrorClass.RATE_LIMITED,
            (
                r"rate limit|\b429\b|too many requests|quota exceeded"
                r"|session limit|usage limit|hit your (session|usage) limit|limit .* resets"
            ),
        ),
        (
            ErrorClass.AUTHENTICATION_FAILED,
            r"not logged in|codex login|authentication|unauthorized|\b401\b",
        ),
        (ErrorClass.AUTHORIZATION_FAILED, r"forbidden|not authorized|\b403\b"),
        (
            ErrorClass.NETWORK_UNAVAILABLE,
            r"could not resolve|connection refused|network is unreachable|getaddrinfo|dns",
        ),
        (
            ErrorClass.PROVIDER_UNAVAILABLE,
            r"service unavailable|\b50[023]\b|bad gateway|internal server error",
        ),
        (
            # A model/schema HTTP 400 the provider rejected (a request WE built) — split from a
            # generic PROCESS_CRASHED so it surfaces loudly instead of wastefully falling over to
            # the other provider, which 400s the same request. Disjoint from the 401/403/429/50x
            # word-boundary numeric signatures above.
            ErrorClass.MODEL_REQUEST_INVALID,
            r"\b400\b|bad request|invalid[_ ]?(json[_ ]?)?schema|unsupported parameter",
        ),
        (ErrorClass.UNSUPPORTED_VERSION, r"unsupported version"),
        (
            # argparse/usage rejection of OUR argv (codex exit 2) — a bad-argv bug on our side, not
            # a version gate. A separate class so it surfaces loudly instead of silently failing
            # over (this was masked as unsupported_version). A stale CLI that emits "unknown option"
            # for a newer flag is caught by the preflight version check first.
            ErrorClass.INVALID_INVOCATION,
            r"unknown option|unrecognized option|unexpected argument",
        ),
        (
            ErrorClass.PERMISSION_DENIED,
            r"sandbox denied|permission denied|operation not permitted|blocked by sandbox|"
            + _HELPER_LAUNCH_FAILED_PATTERN,
        ),
    ]
)


def _resources_dir_for_package(package_root: Path) -> Path:
    """The package's resources directory: ``resourcesDir`` from ``codex-package.json`` if present,
    else the default ``codex-resources`` name (the observed layout: a sibling of ``bin``)."""
    manifest = package_root / _PACKAGE_MANIFEST_NAME
    subdir: object = None
    try:
        subdir = json.loads(manifest.read_text(encoding="utf-8")).get("resourcesDir")
    except (OSError, ValueError, AttributeError):
        subdir = None
    name = subdir if isinstance(subdir, str) and subdir else _DEFAULT_RESOURCES_DIRNAME
    return package_root / name


def codex_config_home() -> Path:
    """The Codex config/credential home: ``$CODEX_HOME`` or the ``~/.codex`` default.

    Codex authenticates through the operator's own home (credentials stay outside the orchestrator).
    No deny is built from it: the standalone package keeps the ``codex`` binary itself inside this
    home, and ``apply_patch`` re-execs that binary under the sandbox as its filesystem helper, so a
    deny here stops every patch from landing. Its consumer is the ``worc preflight`` diagnostic that
    reports whether the provider binary lies inside this home.
    """
    raw = os.environ.get("CODEX_HOME")
    config_dir = Path(raw) if raw else Path.home() / ".codex"
    return config_dir.resolve()


def resolve_codex_resources_dir(
    command: str,
    *,
    system: str | None = None,
    which: Callable[[str], str | None] | None = None,
    userprofile: str | None = None,
) -> Path | None:
    """Locate the Codex ``codex-resources`` directory that holds the Windows sandbox helper.

    Returns the directory (the one containing :data:`_SANDBOX_HELPER_EXE`) to prepend onto the child
    ``PATH`` on Windows, or ``None`` when it need not / cannot be resolved. Pure and injectable
    (``system`` / ``which`` / ``userprofile`` seams, resolved at call time) so it is unit-testable
    on any host. Resolution tries, first-match wins: (1) the package the ``command`` executable
    resolves into — ``codex.exe`` sits in ``bin\\`` with ``codex-resources`` as a sibling, reached
    after resolving the AppData junction; (2) the well-known
    ``%USERPROFILE%\\.codex\\packages\\standalone\\current`` package.
    """
    name = system if system is not None else platform.system()
    if name != "Windows":
        return None
    which_fn = which if which is not None else shutil.which
    candidates: list[Path] = []
    exe = which_fn(command)
    if exe:
        # `…\bin\codex.exe` → resolve the junction → package root (…\releases\<ver>\) holding `bin`.
        package_root = Path(exe).resolve().parent.parent
        candidates.append(_resources_dir_for_package(package_root))
    profile = userprofile if userprofile is not None else os.environ.get("USERPROFILE")
    if profile:
        current = (Path(profile) / ".codex" / "packages" / "standalone" / "current").resolve()
        candidates.append(_resources_dir_for_package(current))
    for candidate in candidates:
        if (candidate / _SANDBOX_HELPER_EXE).exists():
            return candidate
    return None


def _find_reserved_codex_args(args: Sequence[str]) -> list[str]:
    """Return a reason per ``extra_args`` token that is a reserved authority-bearing Codex flag.

    Handles both split (``--profile x``) and inline (``--profile=x``) forms. An empty list is safe.
    """
    reasons: list[str] = []
    for token in args:
        flag = token.split("=", 1)[0]
        if flag in _RESERVED_CODEX_FLAGS:
            reasons.append(
                f"flag {flag!r} is reserved by the orchestrator's Codex isolation policy"
            )
    return reasons


def _effective_permission_profile(config: ProviderConfig, request: AgentRunRequest) -> str:
    """The access level for this attempt.

    A flow node's ceiling (``request.permission_profile``) may lower the provider default to
    ``read-only`` but never raise it: ``read-only`` wins whenever either side requests it.
    """
    configured = config.permission_profile or _DEFAULT_PROFILE
    if request.permission_profile == "read-only" or configured == "read-only":
        return "read-only"
    return request.permission_profile or configured


def _extract_profile_arg(argv: Sequence[str]) -> str | None:
    """The generated permission-profile inline ``-c`` value in *argv*, or ``None``.

    The canary re-runs this exact profile under ``codex sandbox -P`` to prove it enforces. Every
    attempt emits a profile, so ``None`` means the argv is not one this adapter built — a structural
    guard, not a policy: there is no configuration that asks for a profile-free launch.
    """
    for index, token in enumerate(argv[:-1]):
        if token in ("-c", "--config") and argv[index + 1].startswith(
            f"permissions.{PROFILE_NAME}="
        ):
            return argv[index + 1]
    return None


def _isolation_argv(
    config: ProviderConfig,
    request: AgentRunRequest,
    *,
    strict_isolation: bool,
    network_access: bool,
    deny_policy: InternalDenyPolicy | None,
    denied_read_paths: Sequence[str],
    read_isolation_off: bool,
) -> list[str]:
    """The exec-level sandbox + config-isolation options for one attempt.

    Default: a generated permission profile (``read-only``/``workspace-write``) selected as the
    active ``default_permissions``, the operator's user ``config.toml`` ignored (auth still uses
    ``CODEX_HOME``), the project marked ``untrusted`` (so ``.codex/config.toml``/hooks/rules are not
    trusted), and the non-shell tool surfaces disabled. The profile itself carries the filesystem
    deny/read-only carve-outs and disables network. There is no opt-out: a profile is emitted for
    every attempt, because it is also what the pre-launch canary re-runs to prove enforcement.

    ``read_isolation_off`` restores Codex's NATIVE config discovery: the operator's user
    ``config.toml`` is loaded (no ``--ignore-user-config``), the project is TRUSTED (so its
    ``.codex`` config/rules/hooks apply), and the ``hooks`` feature is re-enabled — symmetric with
    Claude's ``--setting-sources project``. It does **not** reach the profile: the private/control
    set stays ``deny`` at every setting (:func:`build_codex_permission_profile`), because the CLI
    reads its own config and auth outside the profile, so discovery never needed that grant — while
    granting it handed the sandboxed shell the orchestrator's own env-file. The heavier autonomous
    tool surfaces (multi-agent/computer/browser/apps/plugins) stay disabled (execution surfaces, not
    read-side discovery), and the pre-launch canary still holds.

    ``strict_isolation: false`` — the advanced mode — drops the feature disables entirely, those
    five included. It is the one setting where they are meant to be reachable, and refusing them
    there would leave the removed full-access escape as their only route. The profile it generates
    also changes there, in the two ways that mode is about: ``write`` on the workspace volume's root
    and ``network.enabled`` from *network_access*, which the caller resolved once for the whole
    attempt. What does NOT become optional: the profile itself, its ``default_permissions``
    selection, and the canary that re-proves it before every launch — the wider the grant, the more
    the carve-outs need demonstrating rather than asserting.
    """
    profile = build_codex_permission_profile(
        permission_profile=_effective_permission_profile(config, request),
        working_directory=request.working_directory,
        deny_policy=deny_policy,
        write_guard=request.write_guard,
        denied_read_paths=tuple(denied_read_paths),
        network_access=network_access,
        strict_isolation=strict_isolation,
    )
    argv = [
        "-c",
        render_permission_profile_arg(profile),
        "-c",
        f'default_permissions="{PROFILE_NAME}"',
    ]
    trust = toml_basic_string(request.working_directory)
    if read_isolation_off:
        # Load the operator's user config and TRUST the project → native ``.codex`` config/rules.
        argv += ["-c", f'projects.{trust}.trust_level="trusted"']
        disabled: tuple[str, ...] = tuple(f for f in _DISABLED_FEATURES if f != "hooks")
    else:
        argv += ["--ignore-user-config", "-c", f'projects.{trust}.trust_level="untrusted"']
        disabled = _DISABLED_FEATURES
    if not strict_isolation:
        # Advanced mode: every feature surface is handed back. The profile is still emitted (it is
        # what the pre-launch canary re-runs to prove the local floor), so what is given up here is
        # the tool inventory, not the filesystem boundary.
        disabled = ()
    for feature in disabled:
        argv += ["--disable", feature]
    return argv


def build_codex_argv(
    config: ProviderConfig,
    request: AgentRunRequest,
    *,
    output_schema_path: str | None,
    last_message_path: str,
    deny_policy: InternalDenyPolicy | None = None,
    strict_isolation: bool = True,
    denied_read_paths: Sequence[str] = (),
    read_isolation_off: bool = False,
) -> list[str]:
    """Build the ``codex exec`` argv (a list, never a shell string).

    Raises :class:`ProviderError` (``CONFIGURATION_ERROR``) when ``extra_args`` would weaken or
    replace the owned authority: the absolutely-forbidden ``--dangerously*`` / ``--yolo`` /
    ``--ignore-rules`` flags and any ``--sandbox`` that is valueless or selects full access, plus
    the reserved authority-bearing flags (``-c``/``--config``, ``-p``/``--profile``, ``-P``,
    ``-s``/``--sandbox``, the approval/sandbox selectors ``--full-auto`` /
    ``-a``/``--ask-for-approval``, ``--add-dir``, ``--ignore-user-config``,
    ``--enable``/``--disable``, ...). Isolation is a generated permission profile via
    ``default_permissions`` (:func:`_isolation_argv`), emitted for every attempt with no opt-out.
    The prompt is delivered on stdin (the trailing ``-``), never on the command line.
    """
    combined_extra = tuple(config.extra_args) + tuple(request.extra_args)
    reasons = find_forbidden_args(combined_extra) + _find_reserved_codex_args(combined_extra)
    if reasons:
        raise ProviderError(
            ErrorClass.CONFIGURATION_ERROR, "rejected unsafe extra_args: " + "; ".join(reasons)
        )

    # Approval policy is a global Codex flag. Both Codex CLI 0.57 and current releases reject it
    # when it is placed after the ``exec`` subcommand.
    argv = [
        config.command,
        "--ask-for-approval",
        "never",
        "exec",
    ]
    # Exec-level options belong to parent ``codex exec`` and MUST precede the optional ``resume``
    # subcommand (codex 0.144.x grammar: ``codex exec [OPTIONS] resume [SESSION_ID] [PROMPT]``).
    # --cd / --json / --output-last-message / --output-schema, the permission-profile / config /
    # feature-disable options, and ``--ignore-user-config`` are all exec options; placing any after
    # ``resume`` is rejected (exit 2). Only -m/--model and -c/--config are accepted by ``resume``
    # itself, so those go after it below — this keeps fresh/resume isolation identical.
    argv += [
        "--cd",
        request.working_directory,
        "--json",
        "--output-last-message",
        last_message_path,
    ]
    # The attempt's effective network, resolved ONCE: the flow's grant, or the advanced mode, which
    # is online for every node whatever its flow said. Both surfaces below read this one value —
    # the profile's sandbox network and the backend-side ``web_search`` — because they are separate
    # boundaries, and a run that opened only one would be online in half the ways that matter.
    network_access = request.network_access or not strict_isolation
    argv += _isolation_argv(
        config,
        request,
        strict_isolation=strict_isolation,
        network_access=network_access,
        deny_policy=deny_policy,
        denied_read_paths=denied_read_paths,
        read_isolation_off=read_isolation_off,
    )
    # Codex's native ``AGENTS.md`` project-doc discovery is intentionally left ENABLED — the agent
    # assembles its own instruction context from the repo's root files. Those files are ordinary,
    # editable repository content: a run that changes them is reported to the operator as a
    # notice, not blocked. (The ``.codex`` project trust control in ``_isolation_argv`` is separate
    # and stays: the project is marked untrusted.)
    if not network_access:
        # Offline attempt → also deny the host-side ``web_search`` tool. It runs on the OpenAI
        # backend, OUTSIDE the profile's sandbox network policy, so without this an "offline" node
        # could still reach the web (a network_access=false writer performed 9 web searches). An
        # online attempt keeps web_search, and its profile is online too: one flag, both surfaces.
        # In the advanced mode every attempt is online, which is also why the validator rule
        # forbidding a Codex workspace-write node with network does not apply there.
        argv += ["-c", 'web_search="disabled"']
    if output_schema_path is not None:
        argv += ["--output-schema", output_schema_path]
    # Durable session resume: ``codex exec [exec-options] resume <SESSION_ID>`` continues the
    # prior session. SESSION_ID is positional right after ``resume``; the prompt is read from stdin
    # (-). --model and model_reasoning_effort (-c) are resume-compatible, so they follow the
    # subcommand; on the fresh path (no subcommand) they follow the exec options.
    if request.session_id:
        argv += ["resume", request.session_id]
    model = config.effective_model(request.model)
    if model:
        argv += ["--model", model]
    reasoning = config.effective_reasoning(request.reasoning)
    if reasoning:
        effort = normalize_codex_reasoning(reasoning)
        if effort is None:
            raise ProviderError(
                ErrorClass.CONFIGURATION_ERROR,
                f"unsupported Codex reasoning value {reasoning!r}",
            )
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv += list(combined_extra)
    argv.append("-")  # read the prompt from stdin
    return argv


def isolation_reasons(config: ProviderConfig) -> list[str]:
    """Reasons this Codex *configuration* is not legal — an empty list means OK.

    Pure, offline and host-independent (no CLI launched), so it drives the fatal
    ``strict_isolation`` gate (:mod:`wastech_orchestrator.security.isolation`) and the Router's
    fallback eligibility question. Every ``read-only``/``workspace-write`` node runs a generated
    permission profile, so what is left to reject is ``extra_args``: an absolutely-forbidden flag
    (including the full-access sandbox selector) or a reserved authority-bearing one, either of
    which would weaken or replace the profile/config surface the adapter owns. The real OS-enforced
    proof is the pre-launch canary (:mod:`wastech_orchestrator.providers.codex_canary`); this
    offline check mirrors argv.
    """
    reasons: list[str] = []
    reasons.extend(f"extra_args {r}" for r in find_forbidden_args(config.extra_args))
    reasons.extend(f"extra_args {r}" for r in _find_reserved_codex_args(config.extra_args))
    return reasons


def default_host_system() -> str:
    """The host platform name Codex's offline floor answer keys on (``platform.system()``).

    A module-level seam mirroring :func:`~wastech_orchestrator.providers.claude`'s
    ``default_sandbox_probe``, and it exists for the same reason: ``host_floor_gap`` is bound into
    the composition table at import time and is thereafter reachable only through the one-argument
    :class:`~wastech_orchestrator.security.isolation.HostFloorCheck` protocol, which has nowhere to
    put an injected host. Reading the host through a name the deterministic suite can pin — rather
    than calling ``platform.system()`` inline — is what keeps a suite assertion about the floor from
    becoming a property of the machine running it.
    """
    return platform.system()


def host_floor_gap(*, strict_isolation: bool, system: str | None = None) -> str | None:
    """What this host cannot be shown to enforce for Codex, or ``None`` when it can be.

    The counterpart of :func:`claude.host_floor_gap`, and deliberately shaped differently, because
    the two questions differ: Claude's sandbox availability is classifiable offline (a platform plus
    two executables on ``PATH``), while Codex's is decided by its own CLI and, on native Windows, by
    whether the elevated sandbox backend is installed — something only the CLI can answer. So the
    honest offline verdict there is "not classifiable here", which is still worth printing: the
    alternative was silence, and a Codex-only park on such a host learned nothing from
    ``worc preflight`` and then met the answer inside its first attempt.

    ``strict_isolation`` is accepted and deliberately NOT read, and that is the asymmetry the
    advanced mode has to state rather than imply. Claude's answer changes with this flag: the mode
    raises no OS sandbox there, on any host. Codex's does not — every attempt gets a generated
    permission profile (``read-only`` / ``workspace-write``) with no opt-out, and the one selector
    that would remove it, ``danger-full-access``, is absolutely forbidden on all three enforcement
    layers at every value of every key. So "no restrictions in the advanced mode" is true of Claude
    and false of Codex, and this parameter is in the signature so a future reader cannot mistake
    silence here for the question not having been asked.

    ``system`` is injectable for the deterministic suite; it defaults to the real host through
    :func:`default_host_system`, which callers that cannot reach this parameter pin instead.
    """
    del strict_isolation  # see above: Codex's floor does not move with the mode
    if (system if system is not None else default_host_system()) != "Windows":
        return None
    return (
        "native Windows: whether the Codex sandbox can enforce here is decided by the CLI's "
        "elevated sandbox backend, which cannot be classified offline — run "
        "`worc preflight` (it runs the capability smoke) to get the answer before a task does. An "
        "undemonstrable sandbox is a warning under strict_isolation: false (the run continues, "
        "unproven) and refuses the attempt under strict_isolation: true"
    )


def attempt_has_shell(config: ProviderConfig, query: ShellQuery) -> bool:
    """Whether a Codex attempt can execute commands — always true, on every profile.

    Pure and offline (no CLI launched), so it can drive the core's per-attempt detection bracket
    (:mod:`wastech_orchestrator.security.shell_reach`). Codex has no shell-less mode: the generated
    ``read-only`` profile forbids every mutation but still permits command execution (a
    ``read-only`` node can run ``git log`` today) and ``workspace-write`` adds the write grant. So
    neither the node's ceiling nor the
    git-evidence grant changes the answer — the grant exists for the provider whose read-only
    profile carries no shell, and Codex needs nothing from it.
    """
    return True


def _normalize_codex_usage(usage: Mapping[str, Any] | None) -> NormalizedUsage | None:
    """Map Codex's raw ``usage`` to the provider-neutral cumulative record.

    Codex reports ``input_tokens`` inclusive of the cached subset, so uncached input is derived; it
    has no cache-creation counter, so ``cache_write`` stays ``None``. Its ``token_count`` /
    ``turn.completed`` events carry token counts but **no dollar figure**, so ``cost`` stays
    ``None`` — never a guessed value. Returns ``None`` when no usage was emitted, preserving
    the no-work guard's "absent usage never fires" contract.
    """
    if not usage:
        return None
    input_total = coerce_usage_int(usage.get("input_tokens"))
    cache_read = coerce_usage_int(usage.get("cached_input_tokens"))
    uncached_input = (
        input_total - cache_read
        if input_total is not None and cache_read is not None
        else input_total
    )
    return NormalizedUsage(
        scope=UsageScope.SESSION_CUMULATIVE,
        input_total=input_total,
        cache_read=cache_read,
        cache_write=None,
        uncached_input=uncached_input,
        output_total=coerce_usage_int(usage.get("output_tokens")),
        reasoning_output=coerce_usage_int(usage.get("reasoning_output_tokens")),
    )


def parse_events(
    stdout_text: str, last_message_text: str | None = None, *, schema_requested: bool = False
) -> ParsedEvents:
    """Parse a Codex JSONL event stream into :class:`ParsedEvents`.

    Tolerant of stray non-JSON lines as long as a recognizable terminal ``result`` event is present.
    Raises :class:`ProviderError` (``INVALID_OUTPUT``) when no terminal event can be found.

    Verified by smoke test against codex-cli 0.139.0: the terminal ``turn.completed`` event
    carries only ``{type, usage}`` — no ``output`` field — so a schema-requested run never fills
    ``structured_output`` from the event stream alone; the schema result instead lands in the
    ``--output-last-message`` file (and mirrors it as an ``agent_message`` event's text). When
    ``schema_requested`` and no terminal ``output`` was seen, parse ``last_message_text`` as the
    structured output. Fails **closed**: an unparseable/non-object last message leaves
    ``structured_output`` at ``None`` rather than guessing — the evaluator runner then routes
    the verdict to manual instead of a silent accept. ``usage`` is also read directly off the
    terminal event, mirroring ``claude.py``'s ``parse_stream_json``.
    """
    final_message: str | None = None
    structured_output: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    session_id: str | None = None
    terminal_seen = False
    succeeded = False

    for line in stdout_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue  # tolerated only if a terminal event is still found below
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        if event_type in ("session", "session.created") or "session_id" in event:
            session_id = event.get("session_id", session_id)
        # ``codex exec`` emits ``{"type":"thread.started","thread_id":"..."}`` — the thread id is
        # the resumable session id passed back to ``codex exec resume <id>`` (durable sessions).
        if event_type == "thread.started":
            session_id = event.get("thread_id", session_id)
        if event_type in ("message", "assistant", "agent_message"):
            text = event.get("text") or event.get("message")
            if isinstance(text, str):
                final_message = text
        if event_type in ("usage", "token_count"):
            usage = {k: v for k, v in event.items() if k != "type"}
        if event_type in ("result", "task_complete", "turn.completed"):
            terminal_seen = True
            status = str(event.get("status", "success")).lower()
            succeeded = status not in _FAILURE_STATUSES
            output = event.get("output")
            if isinstance(output, dict):
                structured_output = output
            run_usage = event.get("usage")
            if isinstance(run_usage, dict):
                usage = run_usage

    if not terminal_seen:
        raise ProviderError(ErrorClass.INVALID_OUTPUT, message_for(ErrorClass.INVALID_OUTPUT))

    if last_message_text:
        final_message = last_message_text.strip()
    if schema_requested and structured_output is None and last_message_text:
        try:
            candidate = json.loads(last_message_text.strip())
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict):
            structured_output = candidate
    return ParsedEvents(
        final_message=final_message,
        structured_output=structured_output,
        usage=usage,
        normalized_usage=_normalize_codex_usage(usage),
        session_id=session_id,
        succeeded=succeeded,
    )


class CodexProvider(BaseCliProvider):
    """Codex CLI adapter implementing the ``AgentProvider`` protocol.

    Carries only the Codex-specific syntax (argv, stderr signatures, JSONL parsing, the
    ``--output-last-message`` file); the run/preflight/redaction spine lives in
    :class:`BaseCliProvider`.
    """

    id: str = ProviderId.CODEX.value

    def __init__(
        self, config: ProviderConfig, *, canary_runner: CanaryRunner | None = None, **kwargs: Any
    ) -> None:
        """Like the base, plus an injectable ``canary_runner``.

        Defaults to real ``codex sandbox``; tests inject a fake so the deterministic suite
        never spawns the real sandbox. Everything else is forwarded to :class:`BaseCliProvider`.
        """
        super().__init__(config, **kwargs)
        self._canary_runner = canary_runner or default_canary_runner

    def _executable_label(self) -> str:
        return "codex"

    def _pre_launch_check(
        self,
        request: AgentRunRequest,
        argv: list[str],
        env: Mapping[str, str],
        paths: ArtifactPaths,
    ) -> None:
        """Prove the generated permission profile is OS-enforced before ``codex exec``.

        Runs the *same* profile under ``codex sandbox -P`` (no model, no network) and checks the
        private home is denied (direct and shell-mediated), the exchange is read-only, the CLI
        binary itself executes under the profile (the path ``apply_patch``'s fs sandbox helper
        takes — a deny that covers the binary broke every patch while every read probe stayed
        green), and every
        Git-control / lifecycle root the profile write-denies actually refuses a write — the
        product's central claim. On real launch env. Skipped only when there is no internal deny set
        to prove (a unit harness with no ``deny_policy``). A leak
        — and a refused exec of the CLI binary — fails closed as a non-fallback security error; an
        undemonstrable sandbox as
        ``CAPABILITY_UNAVAILABLE``.

        Every attempt emits a profile, so a missing one in the argv means the argv is not one this
        adapter built. Returning quietly there would be fail-open on the run's central proof, so it
        is a configuration error instead.
        """
        if self._deny_policy is None:
            return
        profile_arg = _extract_profile_arg(argv)
        if profile_arg is None:
            raise ProviderError(
                ErrorClass.CONFIGURATION_ERROR,
                "no generated permission profile in the launch argv, so the pre-launch canary "
                "cannot prove the sandbox — refusing the attempt rather than running unproven",
            )
        task_path = request.task_path
        exchange_probe = task_path if task_path and Path(task_path).exists() else None
        outcome = run_codex_canary(
            command=self._command_path,
            profile_arg=profile_arg,
            working_directory=request.working_directory,
            private_probe=paths.request_path,
            exchange_probe=exchange_probe,
            env=env,
            system=platform.system(),
            runner=self._canary_runner,
            extra=ExtraProbes(write_guard_probes=self._write_guard_probes(request)),
        )
        Path(paths.attempt_dir, "canary.json").write_text(
            json.dumps(
                {"ok": outcome.ok, "message": outcome.message, "probes": list(outcome.evidence)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if not outcome.ok:
            assert outcome.error_class is not None  # set whenever ok is False
            # The same rule `worc preflight` and the router's fallback apply: in the ADVANCED mode
            # a probe that could not demonstrate the sandbox is a warning and the attempt proceeds —
            # that host class (native Windows without the elevated backend) is exactly the one the
            # mode exists to keep working, and refusing it would make the mode unavailable there
            # while proving nothing. A *proven*
            # leak (`CONFIGURATION_ERROR`) stays fatal at either setting: that is not an
            # unclassifiable host, it is an enforcement failure. Under strict isolation nothing
            # changes — an undemonstrable sandbox still refuses the attempt, fallback-eligible.
            undemonstrable = outcome.error_class is ErrorClass.CAPABILITY_UNAVAILABLE
            if undemonstrable and not self._security.strict_isolation:
                bind(_LOG, task_id=request.task_id, component="canary").warning(
                    "the sandbox could not be demonstrated on this host and the run continues "
                    "(strict_isolation is off): %s. The write floor is not proven for this "
                    "attempt — treat .git and .worc as writable here",
                    outcome.message,
                )
                return
            raise ProviderError(outcome.error_class, outcome.message)

    def _write_guard_probes(self, request: AgentRunRequest) -> tuple[tuple[str, str], ...]:
        """The write-deny probes for this attempt's Git-control roots (empty when it has none).

        The roots come from the request the Core built, so the probes test the profile that is about
        to launch rather than a re-derived guess. Roots that cannot be probed are said out loud
        instead of quietly reducing the probe set: a collapsed one names the ancestor that covers
        it (debug — the coverage is intended), a missing directory names itself at warning level,
        because a write into a directory that does not exist would fail for want of a parent and
        read as an enforced deny.
        """
        if request.write_guard is None:
            return ()
        targets = write_guard_probe_paths(request.write_guard.denied_write_paths)
        log = bind(_LOG, task_id=request.task_id, component="canary")
        for root, ancestor in targets.covered:
            log.debug("write-guard probe for %s covered by its probed parent %s", root, ancestor)
        for root in targets.missing:
            log.warning(
                "write-guard root %s has no directory on disk, so no probe can demonstrate its "
                "deny — a write there would fail for want of a parent, which is not enforcement",
                root,
            )
        return targets.probes

    def isolation_capability_smoke(self, *, home_dir: Path) -> IsolationCapabilityReport | None:
        """Prove the generated profile is OS-enforced on this host, no model.

        Surfaced by ``worc preflight`` so the operator learns BEFORE a run that the Codex sandbox
        cannot enforce here (old CLI / missing sandbox helper) or is mis-generated — instead of a
        mid-run ``CAPABILITY_UNAVAILABLE`` / ``CONFIGURATION_ERROR`` that reads like a bug. Runs the
        no-model :func:`codex_canary.run_codex_capability_smoke` on the configured profile under a
        throwaway fixture. A proven leak is fatal (a non-fallback result); an undemonstrable
        sandbox is advisory (degrades like a capability gap).

        Runs at every value of ``strict_isolation``, including off — that is the configuration
        where the generated profile is the whole local floor, so it is the last one to excuse from
        proving it. It is also the only check that proves the profile is applied by
        the operating system rather than swallowed by the CLI, which accepts an unknown profile key
        without complaint: a typo there yields no policy and no diagnostic.
        """
        env = self._augment_child_env(build_child_env(self._security))
        report = run_codex_capability_smoke(
            command=self._command_path,
            home_dir=home_dir,
            env=env,
            permission_profile=self._config.permission_profile or _DEFAULT_PROFILE,
            # The operator's own setting, so the smoke proves the profile that will launch — in the
            # advanced mode that is the one with the volume-wide write grant, whose carve-outs are
            # the whole reason this check is not skippable there.
            strict_isolation=self._security.strict_isolation,
            runner=self._canary_runner,
        )
        return IsolationCapabilityReport(
            ok=report.ok,
            status=report.status,
            detail=report.detail,
            fatal=report.status == CAPABILITY_POLICY_FAILED,
        )

    def _sandbox_needs_windows_helper(self) -> bool:
        """Whether the configured permission profile engages the Windows sandbox helper.

        The helper backs the native-Windows OS sandbox a ``workspace-write`` profile uses; a
        ``read-only`` profile does not launch it. Conservative — it reads the configured provider
        default (there is no per-node request here), so a provider defaulted to ``workspace-write``
        that only ever runs read-only nodes still requires the helper discoverable at preflight.
        """
        return (self._config.permission_profile or _DEFAULT_PROFILE) != "read-only"

    def _augment_child_env(self, env: dict[str, str]) -> dict[str, str]:
        """Prepend the Codex ``codex-resources`` directory onto ``PATH`` on Windows.

        On Windows ``workspace-write``, Codex launches ``codex-windows-sandbox-setup.exe`` by name;
        the orchestrator's clean allowlisted ``PATH`` does not include it. Resolve the helper's
        package directory and prepend it so the CLI can find it — adjusting only the value of an
        already-allowlisted key (``PATH``), never widening the env allowlist or the sandbox.
        """
        resources = resolve_codex_resources_dir(self._config.command)
        if resources is None:
            return env
        prefix = str(resources)
        path = env.get("PATH", "")
        env["PATH"] = prefix + os.pathsep + path if path else prefix
        return env

    def _post_success_infra_error(self, stderr_text: str) -> NormalizedError | None:
        """Turn a clean terminal SUCCESS whose stderr proves a fatal sandbox-helper failure into a
        raised infra error (``permission_denied``), so the Router falls over instead of trusting a
        run that never touched the workspace. Matched narrowly (helper signatures only)."""
        if _HELPER_LAUNCH_FAILED_RE.search(stderr_text):
            return NormalizedError(
                ErrorClass.PERMISSION_DENIED, message_for(ErrorClass.PERMISSION_DENIED)
            )
        return None

    def _windows_sandbox_helper_error(self, env: Mapping[str, str]) -> str | None:
        """Operator-facing preflight message when the Windows sandbox helper is undiscoverable.

        Runs on the already-augmented ``env``. Returns ``None`` off Windows, for a sandbox that
        needs no helper, or when the helper is reachable (via the package layout or already on
        ``PATH``); otherwise a precise message naming where ``codex`` resolved and what is missing.
        """
        if platform.system() != "Windows" or not self._sandbox_needs_windows_helper():
            return None
        if resolve_codex_resources_dir(self._config.command) is not None:
            return None
        if shutil.which(_SANDBOX_HELPER_EXE, path=env.get("PATH")) is not None:
            return None
        exe = shutil.which(self._config.command) or self._config.command
        return (
            f"Codex sandbox helper {_SANDBOX_HELPER_EXE} is not discoverable for the "
            f"workspace-write sandbox on Windows: codex resolved to {exe}, but its "
            f"{_DEFAULT_RESOURCES_DIRNAME!r} package directory was not found and the helper is not "
            "on PATH. Reinstall or upgrade the Codex standalone package so its "
            f"{_DEFAULT_RESOURCES_DIRNAME} directory (with {_SANDBOX_HELPER_EXE}) exists next to "
            "bin/, or add it to PATH"
        )

    def _preflight_healthy_detail(self, env: Mapping[str, str]) -> str:
        """Show where the Windows sandbox helper resolved on the healthy preflight line."""
        if platform.system() != "Windows" or not self._sandbox_needs_windows_helper():
            return ""
        resources = resolve_codex_resources_dir(self._config.command)
        if resources is not None:
            return f" (Windows sandbox helper: {resources})"
        if shutil.which(_SANDBOX_HELPER_EXE, path=env.get("PATH")) is not None:
            return " (Windows sandbox helper: on PATH)"
        return ""

    def _preflight_capability_error(self, env: Mapping[str, str]) -> str | None:
        """Verify ``codex exec`` exposes ``-c/--config`` for model config overrides.

        Codex reasoning is set through the official ``model_reasoning_effort`` config key. Network
        grants also use ``-c``. Probe ``codex exec --help`` and fail preflight if the subcommand
        lacks config overrides, catching an incompatible CLI before a real node run. A probe that
        does not cleanly exit is treated as inconclusive (no block) — the version check already
        passed. First, on Windows, block when the sandbox helper is undiscoverable — a mid-run
        ``orchestrator_helper_launch_failed`` is far more useful surfaced here, before the flow.
        """
        helper_error = self._windows_sandbox_helper_error(env)
        if helper_error is not None:
            return helper_error
        ok, help_text = self._probe([self._config.command, "exec", "--help"], env)
        if ok and "--config" not in help_text and "-c" not in help_text:
            return (
                "codex exec does not expose -c/--config, required for "
                "model_reasoning_effort and sandbox network overrides; upgrade Codex CLI or clear "
                "Codex reasoning/network overrides"
            )
        return None

    def _preflight_degraded_reasons(self, env: Mapping[str, str]) -> tuple[str, ...]:
        """Confirm ``codex exec resume`` still accepts the options this adapter places after it.

        The resume argv is ``codex exec [exec-options] resume <SESSION_ID> --model .. -c ..`` — only
        ``-m/--model`` and ``-c/--config`` are valid after the ``resume`` subcommand. A future Codex
        that drops ``resume`` or those options would silently break every resume node (supervisor,
        documentation, rework, fixing). Probe ``codex exec resume --help`` and flag the drift so
        preflight surfaces it — fatal only when codex has no fallback provider (decided upstream),
        else a warning. Light grep contract (like the ``-c/--config`` probe): empty output is
        inconclusive (no flag); passes on the 0.152.1 grammar this adapter is verified against.
        """
        ok, help_text = self._probe([self._config.command, "exec", "resume", "--help"], env)
        if not help_text.strip():
            return ()  # inconclusive — the probe produced nothing to grep
        has_model = "--model" in help_text or "-m" in help_text
        has_config = "--config" in help_text or "-c" in help_text
        if ok and has_model and has_config:
            return ()
        return (
            (
                "codex exec resume no longer accepts the -m/--model and -c/--config options "
                "this adapter places after `resume <SESSION_ID>` (Codex CLI grammar drift); "
                "resume nodes (supervisor, documentation, rework, fixing) will fail on codex — "
                "pin a compatible Codex CLI or route these nodes to another provider"
            ),
        )

    def _preflight_auth_state(self, env: Mapping[str, str]) -> AuthProbe | None:
        """Report whether the Codex CLI holds stored credentials, via ``codex login status``.

        Credential **presence** only, and the gap matters: the verb prints the stored-credential
        line and exits 0 for an already-expired refresh token too, so a green answer means there are
        credentials to try, not that the next launch will authenticate. There is no machine-readable
        mode and no round-trip that would prove validity without spending a model call.

        The state is therefore read from the fixed sentence rather than the exit code — logged out
        is a non-zero exit printed on stderr, which the probe folds into the same text — and
        anything unrecognized stays UNKNOWN rather than a claim. ``method`` is left unset because
        the answer is prose, and pattern-matching a mechanism out of a sentence that may be reworded
        upstream would assert more than the probe knows.

        Worth knowing when this reports a logged-out CLI that is in fact logged in: on macOS the CLI
        resolves subscription credentials through the Keychain via ``USER``, so an environment
        allowlist missing that name changes the answer.
        """
        _, output = self._probe([self._config.command, "login", "status"], env)
        text = output.strip().lower()
        # Checked first: the logged-out sentence contains the logged-in one as a substring.
        if "not logged in" in text:
            return AuthProbe(
                state=AuthState.LOGGED_OUT,
                method=None,
                detail="not logged in (run 'codex login')",
            )
        if "logged in" in text:
            return AuthProbe(
                state=AuthState.LOGGED_IN,
                method=None,
                detail="the CLI reports stored credentials",
            )
        return AuthProbe(
            state=AuthState.UNKNOWN,
            method=None,
            detail="'codex login status' gave no recognizable credential answer",
        )

    def _signatures(self) -> Sequence[StderrSignature]:
        return _CODEX_SIGNATURES

    def _build_argv(
        self, request: AgentRunRequest, paths: ArtifactPaths
    ) -> tuple[list[str], tuple[str, bool]]:
        last_message_path = str(Path(paths.attempt_dir) / _LAST_MESSAGE_FILENAME)
        schema_path = self._write_output_schema(paths, request)
        argv = build_codex_argv(
            self._config,
            request,
            output_schema_path=schema_path,
            last_message_path=last_message_path,
            deny_policy=self._deny_policy,
            strict_isolation=self._security.strict_isolation,
            denied_read_paths=self._security.denied_read_paths,
            read_isolation_off=self._security.read_isolation_off,
        )
        return argv, (last_message_path, schema_path is not None)

    def _parse(
        self,
        raw_stdout: str,
        paths: ArtifactPaths,
        parse_context: tuple[str, bool],
        extra_secrets: tuple[str, ...],
    ) -> ParsedEvents:
        # The --output-last-message file is the authoritative final message; redact it on disk too
        # since it may echo agent output.
        last_message_path, schema_requested = parse_context
        last_message_text = read_text(last_message_path)
        if last_message_text and Path(last_message_path).exists():
            Path(last_message_path).write_text(
                redact_text(last_message_text, extra_secrets=extra_secrets), encoding="utf-8"
            )
        return parse_events(
            raw_stdout, last_message_text or None, schema_requested=schema_requested
        )

    def _write_output_schema(self, paths: ArtifactPaths, request: AgentRunRequest) -> str | None:
        if request.output_schema is None:
            return None
        schema_path = str(Path(paths.attempt_dir) / _OUTPUT_SCHEMA_FILENAME)
        Path(schema_path).write_text(
            json.dumps(request.output_schema, ensure_ascii=False), encoding="utf-8"
        )
        return schema_path
