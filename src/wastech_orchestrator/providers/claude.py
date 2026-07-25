"""ClaudeCodeProvider — the Claude Code CLI adapter.

Implements the :class:`~wastech_orchestrator.providers.base.AgentProvider` contract for
``id = "claude"`` using ``claude -p`` (headless/print mode). This is the **only** module that knows
Claude Code syntax; it composes the provider-agnostic infrastructure (process runner, env allowlist,
redaction, artifacts, error normalization) introduced in Phase 2 — exactly as ``codex.py`` does, so
the two adapters are interchangeable behind the contract.

Invariants (architecture.md / security.md): the adapter performs **no fallback** and **never**
touches the state machine; it never commits/pushes/PRs (the denied-commands blacklist is enforced as
``--disallowedTools`` so the agent process cannot publish). It raises
:class:`~wastech_orchestrator.providers.base.ProviderError` (with the right
:class:`~wastech_orchestrator.providers.base.ErrorClass`) for infrastructure failures, and returns
``AgentRunResult(status=failed, error=task_failure)`` for a clean run that did not satisfy the task.
The CLI is launched as an argv list (no shell); the prompt is fed on stdin; context reaches Claude
only as file paths. The permission mapping is at least as strict as the requested profile and never
selects an isolation-weakening mode.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers._adapter_base import (
    BaseCliProvider,
    ParsedEvents,
    coerce_usage_cost,
    coerce_usage_int,
)
from wastech_orchestrator.providers.artifacts import ArtifactPaths
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    ErrorClass,
    NormalizedUsage,
    ProviderError,
    ProviderId,
    UsageScope,
    build_context_footer,
    build_effective_prompt,
)
from wastech_orchestrator.providers.errors import (
    StderrSignature,
    make_signatures,
    message_for,
)
from wastech_orchestrator.runtime_layout import InternalDenyPolicy, ProviderWriteGuardPolicy
from wastech_orchestrator.security.forbidden_args import (
    FORBIDDEN_SANDBOX_VALUE,
    find_forbidden_args,
)

__all__ = [
    "ClaudeCodeProvider",
    "ClaudeToolPlan",
    "ParsedEvents",
    "SandboxCapability",
    "build_claude_argv",
    "build_context_footer",
    "build_effective_prompt",
    "build_sandbox_settings",
    "default_sandbox_probe",
    "isolation_reasons",
    "map_permission",
    "parse_stream_json",
    "resolve_claude_tools",
]

_DEFAULT_PROFILE = "workspace-write"

# Claude Code permission modes, ordered strict → permissive by auto-execution breadth (the
# documented
# 2.1.x choices; the legacy ``default`` alias is dropped). The adapter never selects a mode weaker
# than the one a profile maps to, and never selects ``bypassPermissions`` (full bypass) at all.
_MODE_ORDER: tuple[str, ...] = (
    "plan",
    "dontAsk",
    "manual",
    "acceptEdits",
    "auto",
    "bypassPermissions",
)
_PERMISSION_MODE_FLAG = "--permission-mode"
_BYPASS_MODE = "bypassPermissions"

# Profile → (permission mode, baseline allowed tools). ``read-only`` executes nothing because Edit,
# Write, and Bash are simply absent from its allowlist (a hard tool-level gate). ``dontAsk`` is the
# documented headless read-only mode: it auto-denies every non-allowlisted tool with no prompt and
# no ``plan``-mode interactive UX (``AskUserQuestion``/``ExitPlanMode``, ``~/.claude/plans``) that a
# headless run cannot answer — a read-only agent that needs to ask surfaces it through the role's
# structured output instead (F21). ``workspace-write`` maps to ``acceptEdits`` (auto-approve reads +
# edits + safe workspace commands without prompting) — the Claude equivalent of the Codex
# ``workspace-write`` sandbox. The ``Bash`` baseline is removed on native Windows (no OS sandbox) by
# :func:`resolve_claude_tools`.
_PROFILE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "read-only": ("dontAsk", ("Read", "Glob", "Grep")),
    "workspace-write": ("acceptEdits", ("Read", "Glob", "Grep", "Edit", "Write", "Bash")),
}

# The web tools added to ``--allowedTools``/``--tools`` only when the flow grants network
# (request.network_access); omitted otherwise so a headless run cannot reach the network (P3.2).
_NETWORK_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")


class SandboxCapability(StrEnum):
    """Whether Claude's OS-enforced Bash sandbox can be used on the host (WRI-002).

    macOS uses Seatbelt (always available). Linux and WSL2 both report ``platform.system()==
    "Linux"`` and need ``bubblewrap`` (``bwrap``) + ``socat`` on ``PATH``. Native Windows has no
    supported Bash sandbox. Resolved offline (no CLI launched) by :func:`default_sandbox_probe`.
    """

    MACOS = "macos-sandbox"
    LINUX_AVAILABLE = "linux-wsl-sandbox-available"
    LINUX_MISSING_DEPS = "linux-wsl-sandbox-missing-deps"
    NATIVE_WINDOWS = "native-windows"


#: The host Bash-sandbox classification seam. Injectable (like ``exchange.default_file_inspector``)
#: so every branch is unit-testable on any host without a real probe.
SandboxProbe = Callable[[], SandboxCapability]

# The Linux/WSL2 Bash-sandbox runtime dependencies (documented for Claude Code 2.1.x).
_LINUX_SANDBOX_DEPS: tuple[str, ...] = ("bwrap", "socat")


def default_sandbox_probe(
    system: str | None = None, which: Callable[[str], str | None] | None = None
) -> SandboxCapability:
    """Classify the host's Bash-sandbox capability offline (``platform.system`` + ``shutil.which``).

    ``system``/``which`` resolve from the real host at call time and are injectable for tests, so no
    real probe runs in the deterministic suite. ``shutil.which`` is a ``PATH`` lookup — it launches
    no CLI, so this stays a pure offline check usable by the ``strict_isolation`` preflight.
    """
    name = system if system is not None else platform.system()
    if name == "Darwin":
        return SandboxCapability.MACOS
    if name == "Windows":
        return SandboxCapability.NATIVE_WINDOWS
    which_fn = which if which is not None else shutil.which
    if all(which_fn(dep) for dep in _LINUX_SANDBOX_DEPS):
        return SandboxCapability.LINUX_AVAILABLE
    return SandboxCapability.LINUX_MISSING_DEPS


def _bash_sandbox_available(capability: SandboxCapability) -> bool:
    """True when the host can OS-sandbox a Bash tool (macOS or a dependency-complete Linux/WSL2)."""
    return capability in (SandboxCapability.MACOS, SandboxCapability.LINUX_AVAILABLE)


@dataclass(frozen=True)
class ClaudeToolPlan:
    """The resolved per-attempt Claude tool posture (WRI-002).

    ``mode`` is the ``--permission-mode`` value; ``tools`` is the exact built-in tool set (both the
    hard ``--tools`` existence gate and the ``--allowedTools`` auto-approve list); ``needs_sandbox``
    is True only when a workspace-write attempt keeps ``Bash`` on a host that can OS-sandbox it (so
    the adapter emits the private ``--settings`` sandbox file).
    """

    mode: str
    tools: tuple[str, ...]
    needs_sandbox: bool


def resolve_claude_tools(
    profile: str,
    capability: SandboxCapability,
    network_access: bool,
    *,
    strict_isolation: bool = True,
) -> ClaudeToolPlan:
    """Resolve the mode + built-in tool set + sandbox need for a profile on a host (WRI-002).

    The single source of the platform decision (used by both :func:`build_claude_argv` and the
    settings-file write so they never disagree). Raises :class:`ProviderError`
    (``CAPABILITY_UNAVAILABLE``) — a deterministic *pre-model* infrastructure error — when a strict
    workspace-write attempt needs the Bash sandbox on a supported host (Linux/WSL2) whose sandbox
    dependencies are missing: the adapter refuses to run Bash unsandboxed rather than silently
    weakening isolation. Under ``strict_isolation: false`` the operator has accepted the risk, so
    Bash stays (unsandboxed) and the run is reported as unisolated by the existing preflight
    verdict.
    """
    mode, tools = map_permission(profile)
    needs_sandbox = False
    if profile == "workspace-write":
        if capability is SandboxCapability.NATIVE_WINDOWS:
            if strict_isolation:
                # No supported Bash sandbox on native Windows: drop Bash (restricted mode). Read
                # isolation rides ``--tools`` + the Read/Write/Edit tool denies; Edit/Write remain.
                tools = tuple(t for t in tools if t != "Bash")
            # Under strict_isolation: false the operator keeps unsandboxed Bash (owns the risk).
        elif capability is SandboxCapability.LINUX_MISSING_DEPS and strict_isolation:
            raise ProviderError(
                ErrorClass.CAPABILITY_UNAVAILABLE,
                "Claude's Bash sandbox for a workspace-write node requires bubblewrap+socat on "
                "PATH (Linux/WSL2); refusing to run Bash unsandboxed under strict_isolation",
            )
        elif _bash_sandbox_available(capability):
            needs_sandbox = True
        # else: LINUX_MISSING_DEPS/NATIVE_WINDOWS under strict_isolation:false → keep Bash
        # unsandboxed.
    if network_access:
        tools = (*tools, *_NETWORK_TOOLS)
    return ClaudeToolPlan(mode=mode, tools=tools, needs_sandbox=needs_sandbox)


# Statuses on the terminal ``result`` event that mark the turn as NOT having satisfied the task. Any
# other outcome is treated as a completed run — task quality is judged later by the orchestrator's
# review/checks, not by the adapter.
_SUCCESS_SUBTYPE = "success"

# A subscription/usage-limit banner Claude surfaces inside the terminal ``result`` message (with
# empty stderr) — e.g. "You've hit your session limit · resets 6:30am". Mirrors the extra limit
# patterns added to the stderr RATE_LIMITED signature below.
_LIMIT_BANNER = re.compile(
    r"session limit|usage limit|hit your (session|usage) limit|limit .* resets",
    re.IGNORECASE,
)


def _is_limit_event(payload: object) -> bool:
    """True when a ``rate_limit_event`` payload marks a rejected / capped request."""
    return isinstance(payload, dict) and (
        str(payload.get("status", "")).lower() == "rejected"
        or bool(payload.get("rateLimitType"))
        or bool(payload.get("overageDisabledReason"))
    )


# Claude stderr signatures → normalized error classes (most specific first).
_CLAUDE_SIGNATURES = make_signatures(
    [
        (
            ErrorClass.SESSION_UNAVAILABLE,
            (
                r"session not found|no such session|unknown session|no conversation"
                r"|could not resume|resume failed|invalid session"
            ),
        ),
        (
            ErrorClass.RATE_LIMITED,
            (
                r"rate limit|\b429\b|too many requests|quota exceeded|overloaded"
                r"|session limit|usage limit|hit your (session|usage) limit|limit .* resets"
            ),
        ),
        (
            ErrorClass.AUTHENTICATION_FAILED,
            r"not logged in|claude login|invalid api key|authentication|unauthorized|\b401\b",
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
            # argparse/usage rejection of OUR argv — a bad-argv bug on our side, not a version gate.
            # A separate class so it surfaces loudly instead of silently failing over (F38). A stale
            # CLI that emits "unknown option" for a newer flag is caught by the preflight check.
            ErrorClass.INVALID_INVOCATION,
            r"unknown option|unrecognized option|unexpected argument",
        ),
        (
            ErrorClass.PERMISSION_DENIED,
            r"permission denied|not allowed|operation not permitted|blocked",
        ),
    ]
)


def _deny_tools_for(denied_commands: Sequence[str]) -> list[str]:
    """Translate the denied-commands blacklist into Claude ``Bash(<cmd>:*)`` tool patterns.

    The agent process must never be able to commit/push/open PRs; denying the corresponding ``Bash``
    tool patterns is the tool-level enforcement of that invariant.
    """
    patterns: list[str] = []
    for command in denied_commands:
        normalized = " ".join(command.split())
        if not normalized:
            continue
        patterns.append(f"Bash({normalized}:*)")
    return patterns


def _deny_read_tools_for(denied_read_paths: Sequence[str]) -> list[str]:
    """Translate ``denied_read_paths`` into Claude ``Read(<glob>)`` disallowed-tool patterns.

    The agent must never read secret files (``.env``, ``secrets/**``); denying the ``Read`` tool on
    those paths is the tool-level enforcement, paired with the redaction net for what leaks.
    """
    patterns: list[str] = []
    for path in denied_read_paths:
        normalized = path.strip()
        if not normalized:
            continue
        patterns.append(f"Read({normalized})")
    return patterns


def claude_config_home() -> Path:
    """The Claude Code config/credential home: ``$CLAUDE_CONFIG_DIR`` or the ``~/.claude`` default.

    Resolved from the same environment the spawned child inherits. Shared with the WRI-004
    ``InternalDenyPolicy`` assembly (composition root) so the provider-owned auth/config home is a
    single source of truth instead of a literal duplicated across the deny surfaces.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(raw) if raw else Path.home() / ".claude"
    return config_dir.resolve()


def _native_memory_deny_tools() -> list[str]:
    """Deny ``Write``/``Edit``/``Read`` on the Claude Code config dir so the spawned agent cannot
    read, inject, or leak **native project memory** outside the target working tree (F37).

    Claude Code keeps per-project memory at ``<config_dir>/projects/<cwd-slug>/memory/*.md`` — a
    durable store OUTSIDE the repo, so anything written there escapes ``current.diff``, the commit,
    the redaction net, and the orchestrator's own audit (an unredacted ``originSessionId`` was
    observed leaking). We block it with tool-level path denial rather than isolating
    ``CLAUDE_CONFIG_DIR``: the config dir also holds credentials (file-based on Linux/Windows), so
    redirecting it would break subscription auth there — a deny is auth-safe and cross-platform.

    The config dir (:func:`claude_config_home`) is ``CLAUDE_CONFIG_DIR`` or the ``~/.claude``
    default. Emitted as Claude's ``//``-anchored absolute-path glob with POSIX slashes (the Node CLI
    normalizes them), which covers both the default and a custom absolute config dir.
    """
    glob = "//" + claude_config_home().as_posix().lstrip("/") + "/**"
    return [f"Write({glob})", f"Edit({glob})", f"Read({glob})"]


# Claude flags an operator may NOT supply through config/flow ``extra_args`` because they replace or
# extend the authority the adapter owns (tools, settings/config sources, MCP, plugins, agents,
# additional directories/files, Chrome/IDE/remote-control/background/worktree, system prompt, and
# session selection). Distinct from ``forbidden_args`` (the cross-provider absolute sandbox/approval
# bypass) and from the ``strict_isolation``-gated ``--permission-mode bypassPermissions``: these are
# hard-rejected regardless of ``strict_isolation`` (an operator who wants full access already has
# the
# gated ``bypassPermissions`` path; re-opening a closed surface is never the sanctioned opt-out).
_RESERVED_CLAUDE_FLAGS: frozenset[str] = frozenset(
    {
        "--tools",
        "--allowedTools",
        "--allowed-tools",
        "--disallowedTools",
        "--disallowed-tools",
        "--settings",
        "--setting-sources",
        "--mcp-config",
        "--strict-mcp-config",
        "--add-dir",
        "--file",
        "--agent",
        "--agents",
        "--plugin-dir",
        "--plugin-url",
        "--chrome",
        "--no-chrome",
        "--ide",
        "--remote-control",
        "--remote-control-session-name-prefix",
        "--bg",
        "--background",
        "--worktree",
        "-w",
        "--tmux",
        "--system-prompt",
        "--system-prompt-file",
        "--append-system-prompt",
        "--append-system-prompt-file",
        "--session-id",
        "--fork-session",
        "--no-session-persistence",
        "--resume",
        "-r",
        "--continue",
        "-c",
        "--from-pr",
        "--safe-mode",
        "--bare",
        "--disable-slash-commands",
    }
)


# The Claude flags the adapter's read-isolation policy hard-depends on. A CLI that renamed or
# dropped any of them would otherwise reach a paid model call and only then fail at runtime with
# "unknown option": ``--permission-mode`` (the strict headless mode), ``--setting-sources`` (closes
# user/project/local + skill/plugin/hook discovery), ``--strict-mcp-config`` (no stray MCP server),
# ``--tools`` (the hard built-in-tool existence gate). Probed at preflight so enum/flag drift is
# caught before the model runs (WRI-002 / final-review H2 — the Claude counterpart to Codex's
# ``exec --help`` ``-c/--config`` probe).
_REQUIRED_CLAUDE_FLAGS: tuple[str, ...] = (
    "--permission-mode",
    "--setting-sources",
    "--strict-mcp-config",
    "--tools",
)

# Claude flags whose loss degrades — but does not break — a run: ``--resume`` backs every
# durable-session resume node. A CLI missing it still runs fresh (non-resume) nodes, so this is
# fallback-aware (fatal only when claude is the sole allowed provider), mirroring Codex's
# ``exec resume`` degradation probe.
_DEGRADABLE_CLAUDE_FLAGS: tuple[str, ...] = ("--resume",)


def _find_reserved_claude_args(args: Sequence[str]) -> list[str]:
    """Return a reason per ``extra_args`` token that is a reserved authority-bearing Claude flag.

    Handles both split (``--tools X``) and inline (``--tools=X``) forms. An empty list means safe.
    """
    reasons: list[str] = []
    for token in args:
        flag = token.split("=", 1)[0]
        if flag in _RESERVED_CLAUDE_FLAGS:
            reasons.append(
                f"flag {flag!r} is reserved by the orchestrator's Claude isolation policy"
            )
    return reasons


def _abs_tool_globs(path: Path) -> tuple[str, str]:
    """The ``//``-anchored absolute path node and its descendant glob for a Claude tool-rule.

    Emits both ``//<abs>`` (the exact file/dir node) and ``//<abs>/**`` (its subtree) so a single
    deny covers a secret *file* and a private *directory* without stat'ing the (maybe
    not-yet-created)
    path — and so an exact known-secret path is denied regardless of how ``**`` treats dotfiles. The
    OS sandbox ``denyRead``/``denyWrite`` (a plain absolute path) is the robust dir+dotfile layer;
    this tool glob is the belt that also holds on native Windows where there is no sandbox.
    """
    base = "//" + path.as_posix().lstrip("/")
    return base, f"{base}/**"


def _internal_deny_tools(paths: Sequence[Path], tools: Sequence[str]) -> list[str]:
    """Build ``Tool(//abs)``/``Tool(//abs/**)`` denies for each absolute internal path."""
    patterns: list[str] = []
    for path in paths:
        for glob in _abs_tool_globs(path):
            patterns.extend(f"{tool}({glob})" for tool in tools)
    return patterns


def _sandbox_path(path: Path) -> str:
    """The OS-sandbox filesystem-grammar form of an absolute path (plain, not the ``//`` glob)."""
    return path.as_posix()


def build_sandbox_settings(
    deny_policy: InternalDenyPolicy,
    write_guard: ProviderWriteGuardPolicy | None,
    *,
    network_access: bool,
    read_isolation_off: bool = False,
) -> dict[str, Any]:
    """Build the adapter-owned Claude OS Bash-sandbox settings (WRI-002).

    The private internal read-deny set is sealed for both read AND write; the write-guard roots
    (exchange, gitdir/common-dir/hooks, ``tasks/``) are write-denied only (they stay readable).
    Paths
    use the OS-sandbox grammar (plain absolute), never the ``//`` tool-glob syntax. Network is
    binary
    from ``network_access`` (no domain granularity is available). Only the hardened keys are
    emitted:
    never ``enableWeakerNestedSandbox``, ``allowUnsandboxedCommands: true``, a non-empty
    ``excludedCommands``, a credential ``mask``, or ``tlsTerminate``. ``credentials.files`` denies
    the
    resolved internal env-file (the purpose-built surface) with ``mode: "deny"`` only.
    """
    internal = [_sandbox_path(p) for p in deny_policy.denied_paths]
    # VF-6: with read-isolation OFF the private set stays WRITE-denied (control plane immutable) but
    # is no longer read-denied, so the sandboxed Bash may read it for native discovery. The env-file
    # ``credentials`` deny below is a targeted secret protection and is kept regardless.
    deny_read = [] if read_isolation_off else list(internal)
    deny_write = list(internal)
    if write_guard is not None:
        deny_write.extend(_sandbox_path(p) for p in write_guard.denied_write_paths)
    deny_write = list(dict.fromkeys(deny_write))  # order-preserving de-dup
    sandbox: dict[str, Any] = {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        "filesystem": {"denyRead": deny_read, "denyWrite": deny_write},
        "network": {"allowedDomains": ["*"] if network_access else []},
    }
    if deny_policy.env_file is not None:
        sandbox["credentials"] = {
            "files": [{"path": _sandbox_path(deny_policy.env_file), "mode": "deny"}]
        }
    return {"sandbox": sandbox}


def map_permission(profile: str) -> tuple[str, tuple[str, ...]]:
    """Map a request permission profile to a Claude ``(permission_mode, allowed_tools)`` pair.

    Raises :class:`ProviderError` (``CONFIGURATION_ERROR``) for the forbidden full-access profile or
    an unknown profile — the adapter never silently relaxes isolation and never selects
    ``bypassPermissions``.
    """
    if profile == FORBIDDEN_SANDBOX_VALUE:
        raise ProviderError(ErrorClass.CONFIGURATION_ERROR, f"profile {profile!r} is forbidden")
    mapping = _PROFILE_MAP.get(profile)
    if mapping is None:
        raise ProviderError(
            ErrorClass.CONFIGURATION_ERROR, f"unsupported permission profile {profile!r}"
        )
    return mapping


def _reject_weaker_permission_override(extra_args: Sequence[str], required_mode: str) -> None:
    """Report an ``extra_args`` ``--permission-mode`` that is weaker than the required mode.

    Used **only** by :func:`isolation_reasons` to drive the ``strict_isolation`` preflight — it is
    no longer a runtime hard-raise in :func:`build_claude_argv` (a permission-mode escalation, incl.
    ``bypassPermissions``, is operator-selectable and gated by ``strict_isolation``, not absolutely
    forbidden). ``--dangerously-skip-permissions`` stays absolutely forbidden via
    :func:`find_forbidden_args`; this flags the ``--permission-mode bypassPermissions`` vector and
    any mode more permissive than ``required_mode``.
    """
    required_rank = _MODE_ORDER.index(required_mode)
    for index, token in enumerate(extra_args):
        flag, _, inline = token.partition("=")
        if flag != _PERMISSION_MODE_FLAG:
            continue
        if "=" in token:
            value = inline
        else:
            value = extra_args[index + 1] if index + 1 < len(extra_args) else ""
        if value == _BYPASS_MODE:
            raise ProviderError(
                ErrorClass.CONFIGURATION_ERROR,
                f"{_PERMISSION_MODE_FLAG} {_BYPASS_MODE!r} may not disable the sandbox/approvals",
            )
        if value in _MODE_ORDER and _MODE_ORDER.index(value) > required_rank:
            raise ProviderError(
                ErrorClass.CONFIGURATION_ERROR,
                f"{_PERMISSION_MODE_FLAG} {value!r} is weaker than the requested profile",
            )


def build_claude_argv(
    config: ProviderConfig,
    request: AgentRunRequest,
    *,
    denied_commands: Sequence[str] = (),
    denied_read_paths: Sequence[str] = (),
    internal_deny_read_paths: Sequence[Path] = (),
    sandbox_settings_path: str | None = None,
    sandbox_probe: SandboxProbe | None = None,
    strict_isolation: bool = True,
    read_isolation_off: bool = False,
) -> list[str]:
    """Build the ``claude -p`` argv (a list, never a shell string).

    ``read_isolation_off`` (VF-6, the effective ``security.read_isolation_off``) relaxes only the
    READ side: ``--setting-sources project`` (not ``""``) restores native ``CLAUDE.md`` + project
    settings/hooks/MCP/skills discovery, ``--strict-mcp-config`` is dropped, the private
    ``internal_deny_read_paths`` set becomes readable (still Write/Edit-denied), and the F37
    native-memory deny is lifted. The public ``denied_read_paths`` blacklist and the WRITE side
    (command denies, Write/Edit denies, write-guard) are unchanged.

    Raises :class:`ProviderError` (``CONFIGURATION_ERROR``) if ``extra_args`` carry an
    absolutely-forbidden flag (``--dangerously*`` / ``--yolo`` / ``--ignore-rules``), a reserved
    authority-bearing Claude flag (:data:`_RESERVED_CLAUDE_FLAGS` —
    tools/settings/MCP/plugins/agents/
    add-dir/file/Chrome/IDE/remote/worktree/system-prompt/session), or the requested profile is the
    forbidden full-access mode — defence in depth over the P1 config validator. Raises
    ``CAPABILITY_UNAVAILABLE`` (a pre-model infra error) when a strict workspace-write attempt needs
    the Bash sandbox on a supported host whose sandbox dependencies are missing (:func:
    `resolve_claude_tools`). A ``--permission-mode`` override in ``extra_args`` (incl.
    ``bypassPermissions``) is **not** rejected here: it is operator-selectable, gated by
    ``strict_isolation`` at preflight, and appended last so the CLI's last-wins resolution applies.

    The prompt is delivered on stdin, never on the command line; context reaches Claude only as file
    paths. Isolation is one adapter-owned effective policy: ``--tools`` is the hard built-in tool
    existence gate; ``--allowedTools`` auto-approves them (a headless run cannot prompt);
    ``--disallowedTools`` carries the ``security.*`` command/read denies, the F37 native-memory
    deny,
    and the internal private/exchange/Git ``//``-anchored denies; ``--setting-sources ""`` +
    ``--strict-mcp-config`` close the user/project/local + MCP surfaces; and (workspace-write,
    sandbox
    hosts) ``--settings`` points at the OS Bash-sandbox policy file. ``internal_deny_read_paths`` is
    the WRI-004 :class:`InternalDenyPolicy` set (private/control homes, secrets, provider homes,
    frozen bundles); ``request.write_guard`` carries the exchange/Git/``tasks/`` write-deny roots.
    """
    combined_extra = tuple(config.extra_args) + tuple(request.extra_args)
    reasons = find_forbidden_args(combined_extra) + _find_reserved_claude_args(combined_extra)
    if reasons:
        raise ProviderError(
            ErrorClass.CONFIGURATION_ERROR, "rejected unsafe extra_args: " + "; ".join(reasons)
        )

    profile = request.permission_profile or config.permission_profile or _DEFAULT_PROFILE
    probe = sandbox_probe if sandbox_probe is not None else default_sandbox_probe
    plan = resolve_claude_tools(
        profile, probe(), request.network_access, strict_isolation=strict_isolation
    )

    argv = [
        config.command,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if read_isolation_off:
        # VF-6 operator escape hatch: read-isolation is OFF. Restore Claude's NATIVE project
        # discovery — ``--setting-sources project`` re-loads the target repo's ``CLAUDE.md`` + its
        # project settings (hooks, MCP, skills, plugins) that ``--setting-sources ""`` closes
        # under isolation — and DROP ``--strict-mcp-config`` so project-declared MCP servers load.
        # ``project`` scope (not the CLI ``user,project,local`` default) restores the *project's*
        # native surface without importing the operator's user-global ``~/.claude`` settings. The
        # WRITE side (denyWrite / Write/Edit denies / command denies) below still applies.
        argv += ["--setting-sources", "project"]
    else:
        # Security lockdown (WRI-002): load NO user/project/local setting sources, so Claude never
        # loads the target repo's / user's settings — no hooks, MCP, skills, or plugins (also refuse
        # any MCP server not passed via ``--mcp-config``, and none is, so zero MCP tools load). An
        # accepted consequence is that native ``CLAUDE.md`` memory auto-load is off too — the CLI
        # gates project memory and project settings on this same switch, with no memory-only path.
        # The agent instead reads the repo's root instruction files itself (its role prompt directs
        # it), and those files are write-denied for the run so what it reads stays immutable.
        # Admin-managed policy + auth still apply (the trusted-computing-base, not a repo file).
        argv += ["--setting-sources", "", "--strict-mcp-config"]
    argv += [_PERMISSION_MODE_FLAG, plan.mode]
    if plan.tools:
        # ``--tools`` is the hard existence gate (tools not listed do not exist for the session);
        # ``--allowedTools`` marks the same set auto-approved so a headless run never blocks.
        joined_tools = ",".join(plan.tools)
        argv += ["--tools", joined_tools, "--allowedTools", joined_tools]
    denied_tools = _deny_tools_for(denied_commands) + _deny_read_tools_for(denied_read_paths)
    # F37: confine native project memory out of the spawn — unless the operator has opted in to the
    # agent's own native memory (agents.providers.claude.allow_native_memory) OR read-isolation is
    # OFF (VF-6), both deliberate, operator-owned restorations of Claude's own native memory (that
    # store is unaudited and outside the redaction net). The claude config home is left to this F37
    # rule (gated by the opt-in), so the internal deny below excludes it to avoid re-denying
    # ``~/.claude`` and breaking the opt-in.
    if not config.allow_native_memory and not read_isolation_off:
        denied_tools += _native_memory_deny_tools()
    claude_home = claude_config_home()
    read_deny_paths = [p for p in internal_deny_read_paths if p != claude_home]
    # VF-6: with read-isolation OFF the private set stays WRITE-denied (the control plane must stay
    # immutable) but becomes READABLE so the agent can natively discover it; under isolation it is
    # Read+Write+Edit-denied. The public ``denied_read_paths`` blacklist (above) is unchanged.
    internal_deny_kinds = ("Write", "Edit") if read_isolation_off else ("Read", "Write", "Edit")
    denied_tools += _internal_deny_tools(read_deny_paths, internal_deny_kinds)
    if request.write_guard is not None:
        denied_tools += _internal_deny_tools(
            request.write_guard.denied_write_paths, ("Write", "Edit")
        )
    if denied_tools:
        argv += ["--disallowedTools", ",".join(denied_tools)]
    if sandbox_settings_path is not None:
        # WRI-002: the adapter-owned OS Bash-sandbox policy (workspace-write on a sandbox host). The
        # CLI parent reads this file directly (outside the sandbox), so a private-home path is fine.
        argv += ["--settings", sandbox_settings_path]
    model = request.model or config.model
    if model:
        argv += ["--model", model]
    # --effort enables adaptive thinking at the specified depth (low/medium/high/xhigh/max).
    # No separate --thinking flag needed; --effort alone activates it.
    reasoning = request.reasoning or config.reasoning
    if reasoning:
        argv += ["--effort", reasoning]
    if request.session_id:
        argv += ["--resume", request.session_id]
    if request.output_schema is not None:
        argv += [
            "--json-schema",
            json.dumps(request.output_schema, separators=(",", ":"), sort_keys=True),
        ]
    # ``None`` (config ``max_turns: none`` / ``max`` / null) means no orchestrator-imposed cap:
    # omit ``--max-turns`` so the CLI runs without a turn limit. A positive int caps the turns.
    if config.max_turns is not None:
        argv += ["--max-turns", str(config.max_turns)]
    argv += list(combined_extra)
    return argv


def isolation_reasons(
    config: ProviderConfig, *, capability: SandboxCapability | None = None
) -> list[str]:
    """Reasons the configured Claude isolation cannot be enabled — an empty list means OK.

    Pure and offline (no CLI launched — ``shutil.which`` is only a ``PATH`` lookup), so it drives
    the
    ``strict_isolation`` preflight (:mod:`wastech_orchestrator.security.isolation`) and the Router's
    ``CAPABILITY_UNAVAILABLE`` host-verified fallback gate. Mirrors what :func:`build_claude_argv`
    would enforce: a concrete non-``bypassPermissions`` mode, no forbidden/reserved/weakening
    ``extra_args``, and — host-aware — that a *configured* workspace-write profile can actually get
    its Bash OS sandbox on this host. The last check is conservative: it reads the configured
    provider profile, so a flow that only ever runs Claude read-only but leaves the provider default
    at ``workspace-write`` over-flags on a broken-sandbox host. Native Windows is **not** flagged
    (it
    degrades to a Bash-less restricted mode, not a preflight failure). ``capability`` defaults to
    the
    real host; tests inject it.
    """
    profile = config.permission_profile or _DEFAULT_PROFILE
    try:
        mode, _ = map_permission(profile)
    except ProviderError as exc:
        return [str(exc)]
    reasons = [f"extra_args {r}" for r in find_forbidden_args(config.extra_args)]
    reasons += [f"extra_args {r}" for r in _find_reserved_claude_args(config.extra_args)]
    try:
        _reject_weaker_permission_override(tuple(config.extra_args), mode)
    except ProviderError as exc:
        reasons.append(str(exc))
    cap = capability if capability is not None else default_sandbox_probe()
    if profile == "workspace-write" and cap is SandboxCapability.LINUX_MISSING_DEPS:
        reasons.append(
            "Bash sandbox unavailable for a workspace-write node (bubblewrap+socat missing on "
            "PATH); install them, or set this provider read-only"
        )
    return reasons


def _normalize_claude_usage(
    usage: Mapping[str, Any] | None, *, total_cost_usd: object = None
) -> NormalizedUsage | None:
    """Map Claude's raw ``usage`` to the provider-neutral per-invocation record.

    Claude splits input across three sibling counts that are never pre-summed — the true input is
    ``input_tokens + cache_creation_input_tokens + cache_read_input_tokens`` — and folds reasoning
    into output, so ``reasoning_output`` stays ``None``. Each invocation is self-contained (not
    cumulative). ``total_cost_usd`` is the per-invocation dollar figure the terminal ``result``
    event carries as a **sibling** of ``usage`` (VF-8); it rides the same per-invocation scope, so
    no baseline subtraction applies. Returns ``None`` when no usage was emitted.
    """
    if not usage:
        return None
    uncached_input = coerce_usage_int(usage.get("input_tokens"))
    cache_write = coerce_usage_int(usage.get("cache_creation_input_tokens"))
    cache_read = coerce_usage_int(usage.get("cache_read_input_tokens"))
    input_parts = [part for part in (uncached_input, cache_write, cache_read) if part is not None]
    return NormalizedUsage(
        scope=UsageScope.PER_INVOCATION,
        input_total=sum(input_parts) if input_parts else None,
        cache_read=cache_read,
        cache_write=cache_write,
        uncached_input=uncached_input,
        output_total=coerce_usage_int(usage.get("output_tokens")),
        reasoning_output=None,
        cost=coerce_usage_cost(total_cost_usd),
    )


def parse_stream_json(stdout_text: str) -> ParsedEvents:
    """Parse a Claude ``stream-json`` event stream into :class:`ParsedEvents`.

    Tolerant of stray non-JSON lines as long as a recognizable terminal ``result`` event is present.
    Raises :class:`ProviderError` (``INVALID_OUTPUT``) when no terminal event can be found.
    """
    final_message: str | None = None
    structured_output: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    # VF-8: the per-invocation dollar cost the terminal ``result`` event carries as a sibling of
    # ``usage`` (not inside it), so it is captured separately and threaded into the normalizer.
    total_cost_usd: object = None
    session_id: str | None = None
    terminal_seen = False
    succeeded = False
    failure_subtype: str | None = None
    rate_limited = False
    rate_limit_event: dict[str, Any] | None = None

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
        if "session_id" in event:
            session_id = event.get("session_id", session_id)
        # A ``rate_limit_event`` may arrive as its own line or nested on another event — capture it
        # wherever it appears so the terminal ``result`` check below can see it.
        if event_type == "rate_limit_event":
            rate_limit_event = event
        nested_rle = event.get("rate_limit_event")
        if isinstance(nested_rle, dict):
            rate_limit_event = nested_rle
        if event_type == "result":
            terminal_seen = True
            subtype = str(event.get("subtype", _SUCCESS_SUBTYPE)).lower()
            is_error = bool(event.get("is_error", subtype != _SUCCESS_SUBTYPE))
            succeeded = (not is_error) and subtype == _SUCCESS_SUBTYPE
            failure_subtype = None if succeeded else subtype
            text = event.get("result")
            if isinstance(text, str):
                final_message = text
            output = event.get("structured_output")
            if isinstance(output, dict):
                structured_output = output
            run_usage = event.get("usage")
            if isinstance(run_usage, dict):
                usage = run_usage
            if "total_cost_usd" in event:
                total_cost_usd = event.get("total_cost_usd")
            # A subscription/usage rate-limit terminal: HTTP 429, a rejected ``rate_limit_event``,
            # or a "session limit … resets" banner. Recognized only on an error terminal so a
            # normal run is never misread. The adapter RAISES it as RATE_LIMITED.
            limit_signal = (
                str(event.get("api_error_status")) == "429"
                or _is_limit_event(rate_limit_event)
                or (isinstance(text, str) and bool(_LIMIT_BANNER.search(text)))
            )
            if is_error and limit_signal:
                rate_limited = True

    if not terminal_seen:
        raise ProviderError(ErrorClass.INVALID_OUTPUT, message_for(ErrorClass.INVALID_OUTPUT))

    return ParsedEvents(
        final_message=final_message,
        structured_output=structured_output,
        usage=usage,
        normalized_usage=_normalize_claude_usage(usage, total_cost_usd=total_cost_usd),
        session_id=session_id,
        succeeded=succeeded,
        failure_subtype=failure_subtype,
        rate_limited=rate_limited,
    )


class ClaudeCodeProvider(BaseCliProvider):
    """Claude Code CLI adapter implementing the ``AgentProvider`` protocol.

    Carries only the Claude-specific syntax (argv, stderr signatures, ``stream-json`` parsing,
    inline output schema); the run/preflight/redaction spine lives in :class:`BaseCliProvider`.
    """

    id: str = ProviderId.CLAUDE.value

    def __init__(
        self,
        config: ProviderConfig,
        *,
        sandbox_probe: SandboxProbe | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        # WRI-002: the host Bash-sandbox capability seam. ``None`` resolves the real host at call
        # time (so a test can monkeypatch ``default_sandbox_probe``); tests inject a concrete probe
        # to exercise every platform branch deterministically on any CI host.
        self._sandbox_probe = sandbox_probe

    def _executable_label(self) -> str:
        return "claude"

    def _signatures(self) -> Sequence[StderrSignature]:
        return _CLAUDE_SIGNATURES

    def _preflight_capability_error(self, env: Mapping[str, str]) -> str | None:
        """Verify ``claude --help`` still exposes the isolation-critical flags (WRI-002 / H2).

        The adapter's read-isolation rests on :data:`_REQUIRED_CLAUDE_FLAGS`. A CLI that renamed or
        removed any would otherwise reach a paid model call and only then fail with "unknown
        option". Probe ``claude --help`` and fail preflight if any is absent — the Claude
        counterpart to Codex's ``exec --help`` config-override probe, so CLI drift cannot reach a
        model invocation. A probe that does not cleanly exit is inconclusive (no block); the version
        check already passed. Light grep contract (mirrors Codex): substring presence in the help.
        """
        ok, help_text = self._probe([self._config.command, "--help"], env)
        if not ok:
            return None
        required = _REQUIRED_CLAUDE_FLAGS
        if self._security.read_isolation_off:
            # VF-6: with read-isolation OFF the adapter no longer emits ``--strict-mcp-config`` (it
            # runs native MCP discovery), so the CLI need not expose it. ``--setting-sources`` is
            # still emitted (``project``) and the permission-mode / hard tool gate stay required.
            required = tuple(f for f in required if f != "--strict-mcp-config")
        missing = [flag for flag in required if flag not in help_text]
        if missing:
            return (
                f"claude --help no longer exposes {', '.join(missing)}, required by the "
                "orchestrator's read-isolation policy (permission mode / closed setting sources / "
                "strict MCP / hard tool gate); upgrade or pin a compatible Claude CLI"
            )
        return None

    def _preflight_degraded_reasons(self, env: Mapping[str, str]) -> tuple[str, ...]:
        """Flag Claude CLI drift that breaks durable-session resume nodes (H2).

        ``--resume`` backs every durable-session resume node. A CLI missing it still runs fresh
        (non-resume) nodes, so — like Codex's ``exec resume`` probe — this is advisory: fatal only
        when claude has no fallback provider, else a warning. Empty ``--help`` output is
        inconclusive (no flag).
        """
        ok, help_text = self._probe([self._config.command, "--help"], env)
        if not ok or not help_text.strip():
            return ()  # inconclusive — the probe produced nothing to grep
        missing = [flag for flag in _DEGRADABLE_CLAUDE_FLAGS if flag not in help_text]
        if not missing:
            return ()
        return (
            (
                f"claude --help no longer exposes {', '.join(missing)}; durable-session resume "
                "nodes will fail on claude — pin a compatible Claude CLI or route these nodes "
                "to another provider"
            ),
        )

    def _build_argv(self, request: AgentRunRequest, paths: ArtifactPaths) -> tuple[list[str], None]:
        self._write_output_schema(paths, request)
        # Resolve the tool plan first — a strict workspace-write attempt on a supported host whose
        # Bash sandbox is unavailable raises ``CAPABILITY_UNAVAILABLE`` here, PRE-MODEL: the base
        # ``run`` writes the request artifact and re-raises without launching anything.
        profile = request.permission_profile or self._config.permission_profile or _DEFAULT_PROFILE
        probe = self._sandbox_probe if self._sandbox_probe is not None else default_sandbox_probe
        plan = resolve_claude_tools(
            profile,
            probe(),
            request.network_access,
            strict_isolation=self._security.strict_isolation,
        )
        settings_path: str | None = None
        if plan.needs_sandbox and self._deny_policy is not None:
            settings = build_sandbox_settings(
                self._deny_policy,
                request.write_guard,
                network_access=request.network_access,
                read_isolation_off=self._security.read_isolation_off,
            )
            settings_path = self._write_sandbox_settings(paths, settings)
        argv = build_claude_argv(
            self._config,
            request,
            denied_commands=self._security.denied_commands,
            denied_read_paths=self._security.denied_read_paths,
            internal_deny_read_paths=(
                self._deny_policy.denied_paths if self._deny_policy is not None else ()
            ),
            sandbox_settings_path=settings_path,
            sandbox_probe=self._sandbox_probe,
            strict_isolation=self._security.strict_isolation,
            read_isolation_off=self._security.read_isolation_off,
        )
        return argv, None

    def _write_sandbox_settings(self, paths: ArtifactPaths, settings: dict[str, Any]) -> str:
        """Write the private OS Bash-sandbox policy under the attempt dir; return its path.

        Mirrors :meth:`_write_output_schema`. The file lives under ``private_home`` (the CLI parent
        reads it directly, outside the sandbox, so a private-home path causes no chicken-and-egg).
        """
        settings_path = str(Path(paths.attempt_dir) / "claude-sandbox-settings.json")
        Path(settings_path).write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        return settings_path

    def _parse(
        self,
        raw_stdout: str,
        paths: ArtifactPaths,
        parse_context: None,
        extra_secrets: tuple[str, ...],
    ) -> ParsedEvents:
        return parse_stream_json(raw_stdout)

    def _representation_extras(self, request: AgentRunRequest) -> dict[str, Any]:
        return {"reasoning": request.reasoning or self._config.reasoning or None}

    def _write_output_schema(self, paths: ArtifactPaths, request: AgentRunRequest) -> str | None:
        if request.output_schema is None:
            return None
        schema_path = str(Path(paths.attempt_dir) / "output-schema.json")
        Path(schema_path).write_text(
            json.dumps(request.output_schema, ensure_ascii=False), encoding="utf-8"
        )
        return schema_path
