"""ClaudeCodeProvider — the Claude Code CLI adapter.

Implements the :class:`~wastech_orchestrator.providers.base.AgentProvider` contract for
``id = "claude"`` using ``claude -p`` (headless/print mode). This is the **only** module that knows
Claude Code syntax; it composes the provider-agnostic infrastructure (process runner, env allowlist,
redaction, artifacts, error normalization) introduced in Phase 2 — exactly as ``codex.py`` does, so
the two adapters are interchangeable behind the contract.

Invariants: the adapter performs **no fallback** and **never**
touches the state machine; it never commits/pushes/PRs itself, and it renders the denied-commands
blacklist as ``--disallowedTools`` patterns. Read that list as friction and telemetry rather than a
boundary: it never bound a shell (path denies are not rendered into ``Bash(...)`` patterns at all),
and under ``security.strict_isolation: false`` the agent has a shell, the network and credentials it
picks up by itself, so what keeps publication the orchestrator's is the product mandate plus
after-the-fact detection — not this process being unable to. It raises
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
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers._adapter_base import (
    CAPABILITY_PASSED,
    CAPABILITY_POLICY_FAILED,
    CAPABILITY_UNSUPPORTED,
    BaseCliProvider,
    IsolationCapabilityReport,
    ParsedEvents,
    coerce_usage_cost,
    coerce_usage_int,
)
from wastech_orchestrator.providers.artifacts import ArtifactPaths
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AuthProbe,
    AuthState,
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
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.runtime_layout import (
    CONTROL_HOME_DIRNAME,
    InternalDenyPolicy,
    ProviderWriteGuardPolicy,
)
from wastech_orchestrator.security.forbidden_args import (
    find_forbidden_args,
)
from wastech_orchestrator.security.shell_reach import ShellQuery

__all__ = [
    "ClaudeCodeProvider",
    "ClaudeToolPlan",
    "ParsedEvents",
    "SandboxCapability",
    "attempt_has_shell",
    "build_claude_argv",
    "build_context_footer",
    "build_effective_prompt",
    "build_paid_probe_fixture",
    "build_sandbox_settings",
    "classify_paid_probe",
    "default_sandbox_probe",
    "host_floor_gap",
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
# The flag the adapter emits. The bypass *value* is not named here: it is absolutely forbidden and
# owned by the shared detector, while ``_MODE_ORDER`` above still ranks it as the most permissive
# mode, which is what the escalation check compares against.
_PERMISSION_MODE_FLAG = "--permission-mode"

# The mode advanced mode selects for BOTH profiles. ``acceptEdits`` and not something more
# permissive: it auto-approves reads, edits and workspace commands without prompting, which is all a
# headless run needs. ``auto``/``bypassPermissions`` are not merely unused — they are refused: the
# bypass value absolutely (``find_forbidden_args``), ``auto`` as a weaker rank than the profile's
# mode, checked over config **and** flow-node ``extra_args`` where the argv is built.
_ADVANCED_MODE_PERMISSION_MODE = "acceptEdits"

# Profile → (permission mode, baseline allowed tools). ``read-only`` executes nothing because Edit,
# Write, and Bash are simply absent from its allowlist (a hard tool-level gate). ``dontAsk`` is the
# documented headless read-only mode: it auto-denies every non-allowlisted tool with no prompt and
# no ``plan``-mode interactive UX (``AskUserQuestion``/``ExitPlanMode``, ``~/.claude/plans``) that a
# headless run cannot answer — a read-only agent that needs to ask surfaces it through the role's
# structured output instead. ``workspace-write`` maps to ``acceptEdits`` (auto-approve reads +
# edits + safe workspace commands without prompting) — the Claude equivalent of the Codex
# ``workspace-write`` sandbox. The ``Bash`` baseline is removed on native Windows (no OS sandbox) by
# :func:`resolve_claude_tools`.
_PROFILE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "read-only": ("dontAsk", ("Read", "Glob", "Grep")),
    "workspace-write": ("acceptEdits", ("Read", "Glob", "Grep", "Edit", "Write", "Bash")),
}

# The web tools added to ``--allowedTools``/``--tools`` only when the flow grants network
# (request.network_access); omitted otherwise so a headless run cannot reach the network.
_NETWORK_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")

# The read-only git verbs a node may execute when the operator enabled the git-evidence grant and
# the node declared it. Every verb reports; none mutates the repository and none publishes, so the
# grant buys history inspection without a second path to commit/push/PR. Rendered as scoped
# ``Bash(git <verb>:*)`` auto-approve patterns — the OS sandbox, not this list, is what makes such
# a node read-only. ``security.denied_commands`` is not a second floor beneath it but friction and
# telemetry (see :func:`_deny_tools_for`): a deny does beat an allow inside the CLI, so a denied
# verb is refused and logged, and that log line is the whole value — prefix matching is walked
# around by ``bash -c``, an absolute path or ``git --git-dir=``.
_GIT_EVIDENCE_VERBS: tuple[str, ...] = (
    "log",
    "show",
    "diff",
    "blame",
    "status",
    "rev-list",
    "rev-parse",
    "ls-files",
    "shortlog",
    "describe",
    "cat-file",
    "for-each-ref",
)

# The built-in tool NAMES in the lists below were read out of the `claude` binary named by
# `TOOL_REGISTRY_READ_FROM_VERSION`, and that version is written down because it is what makes "how
# far has this drifted" answerable: the CLI validates no tool name (``claude -p --tools
# BogusToolXYZ`` behaves like a correct one) and offers no way to enumerate the set at run time.
# What it DOES have — and what the earlier note here said it did not — is an explicit registry of
# names inside the binary, readable offline. That is where the fourth editor (`MultiEdit`) came
# from, and reading it is how the floor stopped being a list written from memory. A name added on
# spec still denies nothing, so re-read the binary when the pinned version moves; the health probe
# below turns that into a warning rather than a thing to remember.
#
# Advanced mode (``security.strict_isolation: false``) hands EVERY node a shell, ``read-only``
# included. ``Bash`` and ``PowerShell`` are the two shells; the other three are the shell's own
# bookkeeping and are pointless to withhold once a shell exists. They join the profile baseline as
# bare names, so every invocation auto-approves — a headless run has nobody to answer a prompt.
_ADVANCED_MODE_TOOLS: tuple[str, ...] = (
    "Bash",
    "PowerShell",
    "TodoWrite",
    "BashOutput",
    "KillShell",
)

# FRICTION AND TELEMETRY — deliberately NOT part of the floor, and not to be read as one. A shell
# walks around every one of these, so what they buy is a line in the log saying the agent tried.
# Only the first is an honest ban on its merits: a headless run has nobody to answer a question, so
# the node would hang and burn turns. The other three are kept because they cost nothing, but their
# stated justifications no longer hold and saying so is part of the requirement: ``CronCreate`` and
# ``RemoteTrigger`` were justified by the process-silence barrier, yet the right to write outside
# the clone (``~/Library/LaunchAgents``, ``~/.config/systemd/user``, a shell rc file) buys the same
# persistence with no tool at all; ``EnterWorktree`` was justified by a worktree's own gitdir
# escaping the write guard, but that guard covers ``git_common_dir`` too — which IS the linked
# worktree case — and ``git worktree add`` must write into the denied ``.git/worktrees/`` anyway.
# Persistence is therefore NOT held by this mode, and the shipped guide says so outright.
_ADVANCED_MODE_FRICTION_DENIES: tuple[str, ...] = (
    "AskUserQuestion",
    "CronCreate",
    "RemoteTrigger",
    "EnterWorktree",
)


def _write_anywhere_root(working_directory: str) -> Path | None:
    """The workspace volume's root — how "write outside the clone" is expressed to the sandbox.

    The anchor of the workspace path, so one expression covers every platform this settings file is
    ever written on: ``/`` on macOS and Linux/WSL2, a drive root on native Windows (where no Bash
    sandbox exists, so no such file is written at all today). ``None`` for a relative path — a unit
    harness rather than a real attempt — because a relative anchor names no volume, and a grant on
    ``.`` would be a rule about the process's cwd.
    """
    anchor = Path(working_directory).anchor
    return Path(anchor) if anchor else None


def _effective_network_access(granted: bool, *, strict_isolation: bool) -> bool:
    """Whether this attempt reaches the network — the ONE place the formula lives.

    The flow's grant, OR the advanced mode, which hands every node the whole network whatever its
    flow said (ТA.8.1). Both surfaces read it: the OS sandbox's ``allowedDomains`` and the built-in
    web tools, which do NOT pass through that sandbox — so a single formula is what keeps them from
    disagreeing about whether this run is online.
    """
    return granted or not strict_isolation


#: The `claude` build the tool-name lists in this module were read out of. Not a supported-version
#: floor and not compared for ordering: it is the answer to "which binary was inventoried", so a
#: newer CLI can be reported as a list that may have gone stale (see
#: :meth:`ClaudeCodeProvider.preflight`).
TOOL_REGISTRY_READ_FROM_VERSION = "2.1.234"

#: Every built-in tool that EDITS A FILE, as read out of that binary's registry. The floor rests on
#: this set being complete: these tools do not pass through the OS sandbox, so a path deny that
#: misses one leaves that path editable by a tool nobody named. ``MultiEdit`` is the reason this is
#: a constant with a provenance rather than three names inline — it was missing from every list for
#: the whole campaign, which meant a ``read-only`` node in the advanced mode could edit the working
#: tree with a tool that appeared in no deny at all.
_EDITOR_TOOL_NAMES: tuple[str, ...] = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def _write_deny_kinds(*, advanced_mode: bool) -> tuple[str, ...]:
    """The built-in write tools a path deny has to name — THE FLOOR, and the only part of the
    disallowed list a shell cannot walk around, because these tools do not pass through the OS
    sandbox at all.

    The full editor set (:data:`_EDITOR_TOOL_NAMES`) in advanced mode; the historical pair on the
    shipped default. The asymmetry is deliberate rather than an oversight: with ``--tools`` still
    emitted a tool that is not in the allowlist does not exist for the session, so naming it in a
    deny would be noise in every argv the shipped default builds. Once the existence gate is gone
    every editor is as reachable as ``Write``, so every editor has to be named.
    """
    return _EDITOR_TOOL_NAMES if advanced_mode else ("Write", "Edit")


class SandboxCapability(StrEnum):
    """Whether Claude's OS-enforced Bash sandbox can be used on the host.

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
    """The resolved per-attempt Claude tool posture.

    ``mode`` is the ``--permission-mode`` value; ``tools`` is the built-in tool set;
    ``allow_patterns`` holds the scoped ``Tool(arg:*)`` entries that replace a bare name in the
    ``--allowedTools`` auto-approve list (see :attr:`allowed_tools`); ``needs_sandbox`` is True only
    when the resolved set keeps ``Bash`` on a host that can OS-sandbox it (so the adapter emits the
    private ``--settings`` sandbox file).

    What ``tools`` MEANS depends on the mode, and the two readings are not interchangeable. Under
    ``strict_isolation: true`` it is the hard ``--tools`` existence gate: a tool absent from it does
    not exist for the session, which is the entire mechanism behind the word "read-only". Under
    ``strict_isolation: false`` no gate is emitted and every built-in tool exists; the set is then
    merely the auto-approve baseline, and the boundary has moved to ``--disallowedTools``.
    """

    mode: str
    tools: tuple[str, ...]
    needs_sandbox: bool
    allow_patterns: tuple[str, ...] = ()

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        """The ``--allowedTools`` entries: bare names, except where a pattern scopes the tool.

        A bare tool name in this list auto-approves **every** invocation of that tool and wins over
        a narrower pattern for the same tool in the same list, so a scoped tool must appear as its
        patterns *only* — listing both would hand back the unrestricted shell the patterns exist to
        prevent. Verified against the CLI, not assumed. With no patterns this is exactly ``tools``,
        so a plan that scopes nothing produces today's argv byte for byte.
        """
        scoped = {pattern.split("(", 1)[0] for pattern in self.allow_patterns}
        return (*(t for t in self.tools if t not in scoped), *self.allow_patterns)


def resolve_claude_tools(
    profile: str,
    capability: SandboxCapability,
    network_access: bool,
    *,
    strict_isolation: bool = True,
    git_evidence: bool = False,
) -> ClaudeToolPlan:
    """Resolve the mode + built-in tool set + sandbox need for a profile on a host.

    The single source of the platform decision (used by both :func:`build_claude_argv` and the
    settings-file write so they never disagree). ``git_evidence`` is the resolved per-node grant:
    it adds a shell to a shell-less profile and scopes it to the read-only git verbs, so an audit
    node can read delivery history instead of substituting a changelog grep for it.

    ``strict_isolation: false`` is the advanced mode, and it changes what this function returns in
    four ways. Every profile gains :data:`_ADVANCED_MODE_TOOLS`, so a ``read-only`` node gets the
    shell it has never had, and :data:`_NETWORK_TOOLS` join it whatever the flow granted
    (:func:`_effective_network_access`). The git-evidence construction is not applied at all — its
    whole purpose was to hand a shell-less profile a shell narrowed to twelve verbs, and both are
    moot once every node has an unscoped one; the names go out bare. And the mode becomes
    ``acceptEdits`` for both profiles, because ``dontAsk`` auto-denies every tool not on the
    allow-list: with the existence gate gone that would leave a ``read-only`` node exactly as
    tool-bound as before, only less legibly. The accepted cost is stated in the ADR — an unknown
    tool in a future CLI release auto-approves instead of auto-denying — and what holds a read-only
    node is then the bare write denies plus the whole-clone ``denyWrite`` in the sandbox file.

    The platform arm is keyed on **"does the resolved set keep Bash"**, not on the profile name: a
    read-only attempt that was granted a shell needs exactly the protection a workspace-write one
    does. Raises :class:`ProviderError` (``CAPABILITY_UNAVAILABLE``) — a deterministic *pre-model*
    infrastructure error — when such an attempt needs the Bash sandbox on a supported host
    (Linux/WSL2) whose sandbox dependencies are missing: the adapter refuses to run Bash
    unsandboxed rather than silently weakening isolation. Under ``strict_isolation: false`` the
    operator has accepted the risk, so Bash stays (unsandboxed) and the run is reported as
    unisolated by the existing preflight verdict.
    """
    mode, tools = map_permission(profile)
    allow_patterns: tuple[str, ...] = ()
    if not strict_isolation:
        mode = _ADVANCED_MODE_PERMISSION_MODE
        # Order-preserving de-dup: ``workspace-write`` already carries ``Bash``, and a name repeated
        # in ``--allowedTools`` is at best noise in an argv that gets read during an incident.
        tools = tuple(dict.fromkeys((*tools, *_ADVANCED_MODE_TOOLS)))
    elif git_evidence and "Bash" not in tools:
        # Grant the shell and scope it to the read-only verbs. Guarded on Bash being absent so the
        # grant only ever adds reach: on a profile that already carries an unscoped shell, scoping
        # it here would be a silent restriction wearing the name of a capability.
        tools = (*tools, "Bash")
        allow_patterns = tuple(f"Bash(git {verb}:*)" for verb in _GIT_EVIDENCE_VERBS)
    needs_sandbox = False
    if "Bash" in tools:
        if capability is SandboxCapability.NATIVE_WINDOWS:
            if strict_isolation:
                # No supported Bash sandbox on native Windows: drop Bash (restricted mode). Read
                # isolation rides ``--tools`` + the Read/Write/Edit tool denies; Edit/Write remain.
                # A granted read-only shell drops with it — the capability-conditional wording in
                # the role prompt then applies — rather than becoming an unsandboxed shell here.
                tools = tuple(t for t in tools if t != "Bash")
                allow_patterns = ()
            # Under strict_isolation: false the operator keeps unsandboxed Bash (owns the risk).
        elif capability is SandboxCapability.LINUX_MISSING_DEPS and strict_isolation:
            # The only place a floor-less host still stops anything. Preflight and the run log name
            # such a host in a loud line but never refuse, so this refusal is both narrower and more
            # useful than a preamble stop would be: it fires for the attempt that actually keeps a
            # shell, with the node's profile in hand, and leaves the rest of the run — and a
            # fallback provider that can isolate here — free to proceed.
            raise ProviderError(
                ErrorClass.CAPABILITY_UNAVAILABLE,
                f"Claude's Bash sandbox for a {profile} node that keeps a shell requires "
                "bubblewrap+socat on PATH (Linux/WSL2); refusing to run Bash unsandboxed under "
                "strict_isolation",
            )
        elif _bash_sandbox_available(capability):
            needs_sandbox = True
        # else: LINUX_MISSING_DEPS/NATIVE_WINDOWS under strict_isolation:false → keep Bash
        # unsandboxed.
    if _effective_network_access(network_access, strict_isolation=strict_isolation):
        # The advanced mode is online whatever the flow granted, so the web tools join every node's
        # auto-approve list there. Outside it nothing changes: no grant, no web tools.
        tools = (*tools, *_NETWORK_TOOLS)
    return ClaudeToolPlan(
        mode=mode, tools=tools, needs_sandbox=needs_sandbox, allow_patterns=allow_patterns
    )


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


def _parse_auth_status(output: str) -> dict[str, Any] | None:
    """The credential-status object out of a combined stdout+stderr probe answer, else ``None``.

    Tries the whole answer first (a pretty-printed object spanning lines), then line by line,
    because the probe appends stderr to stdout and a startup notice on either stream must not turn a
    good answer into an unknown. Only an object actually carrying ``loggedIn`` counts as the answer.
    """
    for candidate in (output.strip(), *(line.strip() for line in output.splitlines())):
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "loggedIn" in payload:
            return payload
    return None


def _limit_resets_at(payload: object) -> float | None:
    """The Unix instant a reported limit window reopens, when the event carries one.

    Anything that is not a plain number yields ``None``: this is provider input, and a missing wake
    instant costs only the blind next-tick retry the orchestrator would do anyway.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("resetsAt")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


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
            # A separate class so it surfaces loudly instead of silently failing over. A stale
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


#: The shell surfaces a command pattern is projected onto. Both, because the advanced mode hands
#: every node both — and on Windows ``PowerShell`` is the primary one, so a pattern emitted for
#: ``Bash`` alone left the log with no trace of an attempt there. Duplicating a pattern across both
#: is the vendor's own practice: the pinned binary contains a function that expands each command
#: pattern into exactly this pair.
_SHELL_TOOL_NAMES: tuple[str, ...] = ("Bash", "PowerShell")


def _deny_tools_for(denied_commands: Sequence[str]) -> list[str]:
    """Translate the denied-commands blacklist into ``Bash(<cmd>:*)`` / ``PowerShell(<cmd>:*)``.

    FRICTION AND TELEMETRY, not a boundary — the earlier wording ("tool-level enforcement of that
    invariant") claimed more than the mechanism delivers, and a control described as stronger than
    it is gets trusted for decisions it cannot carry. Prefix matching on a normalized command string
    is walked around by ``bash -c``, an absolute path, ``git -C``, ``git --git-dir=``, a Makefile
    target, a child process, ``gh api`` or ``curl``. What it does buy is worth the zero it costs: a
    blocked invocation is the one signal in the log that the agent reached for publication — which
    is exactly why it is emitted for BOTH shells the mode hands out. Emitting it for ``Bash`` alone
    meant the reasoning behind keeping this list at all ("otherwise there is no trace") did not hold
    on the platform where ``PowerShell`` is the shell. The actual local floor is the OS sandbox plus
    the path-scoped write denies (:func:`_write_deny_kinds`); the remote half is held by detection.
    """
    patterns: list[str] = []
    for command in denied_commands:
        normalized = " ".join(command.split())
        if not normalized:
            continue
        patterns += [f"{shell}({normalized}:*)" for shell in _SHELL_TOOL_NAMES]
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

    Resolved from the same environment the spawned child inherits. Shared with the
    ``InternalDenyPolicy`` assembly (composition root) so the provider-owned auth/config home is a
    single source of truth instead of a literal duplicated across the deny surfaces.

    Raises ``RuntimeError`` when neither source answers — no ``CLAUDE_CONFIG_DIR`` and no resolvable
    home directory. Callers that build a deny out of it must not swallow that: a deny built on a
    guessed path protects nothing (see :func:`native_memory_optin_error`).
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(raw) if raw else Path.home() / ".claude"
    return config_dir.resolve()


def native_memory_optin_error(config: ProviderConfig) -> str | None:
    """Why ``allow_native_memory: true`` cannot be honored on this host, or ``None`` when it can.

    The opt-in is expressed as a *narrowed* deny — everything under the config home stays
    write-denied except the per-project memory subtree (:func:`_native_memory_deny_tools`) — so it
    needs the config home's real path. When that cannot be determined the honest answer is to refuse
    the configuration rather than to emit a deny over a guessed path or, worse, no deny at all:
    that home holds the credentials, and the opt-in was never meant to open it.
    """
    if not config.allow_native_memory:
        return None
    try:
        claude_config_home()
    except (RuntimeError, OSError) as exc:
        return (
            "allow_native_memory is on but the Claude config home cannot be resolved "
            f"({exc}) — set CLAUDE_CONFIG_DIR to an absolute path, or turn the opt-in off: the "
            "write-deny that keeps the rest of that home (credentials included) closed is built "
            "from this path, and one cannot be written for a path nobody can name"
        )
    return None


#: The segment Claude keeps a project's memory files under, inside its per-project directory. The
#: opt-in's whole surface: ``<config home>/projects/<cwd-slug>/memory/**``.
_MEMORY_SEGMENT = "memory"


def _native_memory_optin_deny_tools(kinds: Sequence[str]) -> list[str]:
    """Deny *kinds* on the config home EXCEPT the per-project memory subtree (the opt-in's scope).

    Owner decision (2026-08-20): ``allow_native_memory`` opens the memory store, not the config
    home. It used to drop the deny for the whole home, and since the internal projection carves that
    home out too, the opt-in left ``~/.claude`` — credentials, settings, every project's session
    transcripts — with no tool-level deny at all, in the one mode where the agent also has an
    unscoped shell.

    Expressed by depth, because the deny language is globs and has no "except": the memory store
    sits three segments below the home (``projects/<slug>/memory/<file>``), so denying the first two
    levels closes the credential files, the top-level settings and each project's own files while
    leaving the store reachable. What is NOT closed is anything else three levels down; that is the
    accepted residue of the decision, and the OS sandbox's ``denyWrite`` still covers the whole home
    for Bash.
    """
    home = claude_config_home().as_posix().lstrip("/")
    globs = (f"//{home}/*", f"//{home}/*/*")
    return [f"{kind}({glob})" for glob in globs for kind in kinds]


def _native_memory_deny_tools(
    kinds: Sequence[str] = ("Write", "Edit", "Read"),
) -> list[str]:
    """Deny *kinds* (default ``Write``/``Edit``/``Read``) on the Claude Code config dir so the
    spawned agent cannot read, inject, or leak **native project memory** outside the target tree.

    Claude Code keeps per-project memory at ``<config_dir>/projects/<cwd-slug>/memory/*.md`` — a
    durable store OUTSIDE the repo, so anything written there escapes ``current.diff``, the commit,
    the redaction net, and the orchestrator's own audit (an unredacted ``originSessionId`` was
    observed leaking). We block it with tool-level path denial rather than isolating
    ``CLAUDE_CONFIG_DIR``: the config dir also holds credentials (file-based on Linux/Windows), so
    redirecting it would break subscription auth there — a deny is auth-safe and cross-platform.

    The config dir (:func:`claude_config_home`) is ``CLAUDE_CONFIG_DIR`` or the ``~/.claude``
    default. Emitted as Claude's ``//``-anchored absolute-path glob with POSIX slashes (the Node CLI
    normalizes them), which covers both the default and a custom absolute config dir.

    *kinds* exists because the read and write axes are gated differently: relaxing read-isolation
    restores native discovery (a ``Read``), but never permission to *write* an unaudited store.
    """
    glob = "//" + claude_config_home().as_posix().lstrip("/") + "/**"
    return [f"{kind}({glob})" for kind in kinds]


# Claude flags an operator may NOT supply through config/flow ``extra_args`` because they replace or
# extend the authority the adapter owns (tools, settings/config sources, MCP, plugins, agents,
# additional directories/files, Chrome/IDE/remote-control/background/worktree, system prompt, and
# session selection). Distinct from ``forbidden_args`` (the cross-provider absolute sandbox/approval
# bypass, which also owns ``--permission-mode bypassPermissions``): both clusters are rejected
# regardless of ``strict_isolation``, because no configuration grants an operator full access and
# re-opening a closed surface is never a sanctioned opt-out.
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
# ``--tools`` (the hard built-in-tool existence gate), ``--allowedTools`` (the auto-approve list,
# and the only place a scoped tool pattern is expressed — a granted read-only shell is confined to
# its verbs by that list plus the OS sandbox), ``--disallowedTools`` (every path-scoped write deny;
# in the advanced mode it is the ONLY thing left carrying the floor, which is exactly why a CLI that
# renamed it has to be caught here rather than after a paid model call). Probed at preflight so
# enum/flag drift is caught before the model runs (the Claude counterpart to Codex's
# ``exec --help`` ``-c/--config`` probe).
_REQUIRED_CLAUDE_FLAGS: tuple[str, ...] = (
    "--permission-mode",
    "--setting-sources",
    "--strict-mcp-config",
    "--tools",
    "--allowedTools",
    "--disallowedTools",
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
    deny_write_root: Path | None = None,
    allow_write_root: Path | None = None,
) -> dict[str, Any]:
    """Build the adapter-owned Claude OS Bash-sandbox settings.

    The private internal read-deny set is sealed for both read AND write; the write-guard roots
    (exchange, gitdir/common-dir/hooks, ``tasks/``) are write-denied only (they stay readable).
    Paths
    use the OS-sandbox grammar (plain absolute), never the ``//`` tool-glob syntax. Network is
    binary
    from ``network_access`` (no domain granularity is available). Only the hardened keys are
    emitted:
    never ``enableWeakerNestedSandbox``, ``allowUnsandboxedCommands: true``, a non-empty
    ``excludedCommands``, a credential ``mask``, or ``tlsTerminate``. The one permissive key that IS
    written is ``autoAllowBashIfSandboxed: true``, and it is written precisely so it cannot change
    under us: a vendor default that flipped to ``false`` would make every sandboxed command prompt,
    with nobody headless to answer. ``credentials.files`` denies the
    resolved internal env-file (the purpose-built surface) with ``mode: "deny"`` only.

    ``deny_write_root`` write-denies one whole subtree on top of the sets above. The adapter passes
    the workspace root for a read-only attempt that was granted a shell: what keeps such a node
    read-only is then the OS sandbox — the same mechanism Codex relies on — and not the goodwill of
    a verb allowlist. One qualifier, symmetric with ``allow_write_root`` below: in the advanced mode
    this deny sits INSIDE the volume-wide allow, so the shell half of "read-only cannot change the
    repository" rests on the nesting order described there. The tool half does not: the whole editor
    set (:data:`_EDITOR_TOOL_NAMES`) is denied on the clone for such a node, and those tools never
    pass through this sandbox.

    ``allow_write_root`` write-ALLOWS one whole subtree, and the adapter passes the workspace
    volume's root in the advanced mode: without it the sandbox permits writes only inside the
    workspace and the session temp, which is what makes ``dotnet build`` fail on ``~/.nuget`` rather
    than on anything to do with ``dotnet``. It is emitted as ``filesystem.allowWrite`` and only when
    given, so the file this function builds outside the mode is unchanged. Two things to know before
    reading this as a boundary. The deny sets above are *more specific* paths inside it, and nesting
    a deny inside an allow is a construction the vendor supports outright: the pinned binary
    (:data:`TOOL_REGISTRY_READ_FROM_VERSION`) carries ``filesystem.denyWrite`` into a field named
    ``denyWithinAllow`` and applies it inside the allowed set. That is read out of the binary, not
    proven on this host, and the loud preflight line says exactly that on floor 1 — the instrument
    that can settle it is ``worc preflight --paid-isolation-probe``. And on Claude what actually
    holds ``.git``/``.worc`` regardless of that ranking is the tool-level editor denies, which never
    pass through this sandbox at all.
    """
    internal = [_sandbox_path(p) for p in deny_policy.denied_paths]
    # With read-isolation OFF the private set stays WRITE-denied (control plane immutable) but
    # is no longer read-denied, so the sandboxed Bash may read it for native discovery. The env-file
    # ``credentials`` deny below is a targeted secret protection and is kept regardless.
    deny_read = [] if read_isolation_off else list(internal)
    deny_write = list(internal)
    if write_guard is not None:
        deny_write.extend(_sandbox_path(p) for p in write_guard.denied_write_paths)
    if deny_write_root is not None:
        deny_write.append(_sandbox_path(deny_write_root))
    deny_write = list(dict.fromkeys(deny_write))  # order-preserving de-dup
    filesystem: dict[str, Any] = {}
    if allow_write_root is not None:
        # Grant first, carve out second — the order this dict is read in, not a precedence claim.
        filesystem["allowWrite"] = [_sandbox_path(allow_write_root)]
    filesystem["denyRead"] = deny_read
    filesystem["denyWrite"] = deny_write
    sandbox: dict[str, Any] = {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        # Stated rather than inherited from the vendor default, at every isolation setting. Should
        # that default ever be ``false``, every sandboxed command would start asking permission —
        # and a headless run has nobody to ask, so the node would burn its turns on prompts nobody
        # answers. The key rides in the settings file, so today's argv is unchanged either way.
        "autoAllowBashIfSandboxed": True,
        "filesystem": filesystem,
        "network": {"allowedDomains": ["*"] if network_access else []},
    }
    if deny_policy.env_file is not None:
        sandbox["credentials"] = {
            "files": [{"path": _sandbox_path(deny_policy.env_file), "mode": "deny"}]
        }
    return {"sandbox": sandbox}


def map_permission(profile: str) -> tuple[str, tuple[str, ...]]:
    """Map a request permission profile to a Claude ``(permission_mode, allowed_tools)`` pair.

    Raises :class:`ProviderError` (``CONFIGURATION_ERROR``) for any profile outside the map — which
    includes the removed provider full-access value, refused here as simply unsupported rather than
    by a branch of its own: the schema does not accept it, and the selectors that could ask for it
    are closed absolutely by :func:`find_forbidden_args` on three surfaces. The adapter never
    silently relaxes isolation and never selects ``bypassPermissions``.
    """
    mapping = _PROFILE_MAP.get(profile)
    if mapping is None:
        raise ProviderError(
            ErrorClass.CONFIGURATION_ERROR, f"unsupported permission profile {profile!r}"
        )
    return mapping


def _reject_weaker_permission_override(extra_args: Sequence[str], required_mode: str) -> None:
    """Reject a ``--permission-mode`` in *extra_args* that is weaker than the required mode.

    Two callers, deliberately: :func:`isolation_reasons` surfaces it offline from the operator's
    config (a preflight reason), and :func:`build_claude_argv` enforces it on the **combined** set —
    provider config plus the flow node's own ``extra_args`` — because that is where a node could
    otherwise win by last-wins ordering. The outright bypass value is caught earlier and absolutely
    by :func:`find_forbidden_args`; what is left here is the quieter vector — a mode that is merely
    *more permissive* than the one the requested profile maps to, which no list of forbidden tokens
    can recognize without knowing that profile.
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
        if value in _MODE_ORDER and _MODE_ORDER.index(value) > required_rank:
            raise ProviderError(
                ErrorClass.CONFIGURATION_ERROR,
                f"{_PERMISSION_MODE_FLAG} {value!r} is weaker than the requested profile",
            )


def _disallowed_tools(
    *,
    profile: str,
    advanced_mode: bool,
    allow_native_memory: bool,
    read_isolation_off: bool,
    denied_commands: Sequence[str],
    denied_read_paths: Sequence[str],
    internal_deny_read_paths: Sequence[Path],
    write_guard: ProviderWriteGuardPolicy | None,
) -> list[str]:
    """Assemble the whole ``--disallowedTools`` value for one attempt.

    Split out of :func:`build_claude_argv` because in the advanced mode this list stops being one
    input among several and becomes the entire tool-side boundary — everything the removed
    ``--tools`` gate used to say implicitly now has to be said here, out loud, in one readable
    place. Two categories live in it and must not be read as one: the path-scoped write denies
    (:func:`_write_deny_kinds`) are the floor a shell cannot walk around, because the CLI's own
    editing tools never pass through the OS sandbox; the command patterns and
    :data:`_ADVANCED_MODE_FRICTION_DENIES` are friction and telemetry.

    Returns the entries in a stable order; an empty list means the caller emits no flag at all.
    """
    denied_tools = _deny_tools_for(denied_commands) + _deny_read_tools_for(denied_read_paths)
    if advanced_mode:
        # Everything below is named because the existence gate no longer names it. The write tools
        # are the floor for a node that is not meant to write; the friction set is not a floor and
        # is documented as such. The web tools used to be denied here for a node whose flow granted
        # no network — that was this list holding the network axis shut for one phase, until the
        # phase that opens it deliberately. The mode is online for every node now, so nothing about
        # the network is said here at all.
        if profile == "read-only":
            denied_tools += list(_write_deny_kinds(advanced_mode=True))
        denied_tools += list(_ADVANCED_MODE_FRICTION_DENIES)
    # Confine native project memory out of the spawn unless the operator opted in
    # (agents.providers.claude.allow_native_memory) — a deliberate, operator-owned restoration of
    # Claude's own native memory (that store is unaudited and outside the redaction net). The claude
    # config home is left entirely to this rule, so the internal deny below excludes it to avoid
    # re-denying ``~/.claude`` and breaking the opt-in.
    #
    # Read-isolation OFF lifts only the READ side. It used to skip this rule wholesale, and because
    # the internal projection excludes ``~/.claude`` too, that left the store with ZERO deny rules
    # on the shipped default — agents were observed writing memory files into the operator's HOME,
    # i.e. outside the workspace clone, the frozen instruction bundle, and the redaction net. The
    # write side was never part of the hatch: relaxing reads restores native *discovery*, not
    # permission to mutate an unaudited store. (The Bash sandbox write-denies it either way, but the
    # CLI's own Write/Edit tools never go through that sandbox — hence only Bash was blocked.)
    write_kinds = _write_deny_kinds(advanced_mode=advanced_mode)
    if not allow_native_memory:
        denied_tools += _native_memory_deny_tools(
            write_kinds if read_isolation_off else (*write_kinds, "Read")
        )
    else:
        # The opt-in is scoped to the memory store, not to the home it lives in: everything above
        # that subtree keeps its write-deny (owner decision 2026-08-20). The read side is not
        # narrowed here — this axis is `disable_read_isolation`'s, and the opt-in has never been a
        # read grant.
        denied_tools += _native_memory_optin_deny_tools(write_kinds)
    claude_home = claude_config_home()
    read_deny_paths = [p for p in internal_deny_read_paths if p != claude_home]
    # The private set is Read+Write+Edit-denied at EVERY read-isolation setting. It used to become
    # merely WRITE-denied when read-isolation was off — that is, on the shipped default — and since
    # the resolved env-file is part of this set, the plain ``Read`` tool could open the
    # orchestrator's own ``.env``. Read-isolation relaxes native *discovery*, and discovery needs
    # nothing from here: the provider's own config home is carved out just above (only
    # ``allow_native_memory`` decides that one), and a project's instructions/settings are ordinary
    # repository paths outside this set. The public ``denied_read_paths`` blacklist is unchanged.
    denied_tools += _internal_deny_tools(
        read_deny_paths, ("Read", *_write_deny_kinds(advanced_mode=advanced_mode))
    )
    if write_guard is not None:
        denied_tools += _internal_deny_tools(
            write_guard.denied_write_paths, _write_deny_kinds(advanced_mode=advanced_mode)
        )
    return denied_tools


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

    ``read_isolation_off`` (the effective ``security.read_isolation_off``) relaxes only the
    READ side: ``--setting-sources project`` (not ``""``) restores native ``CLAUDE.md`` + project
    settings/hooks/MCP/skills discovery, ``--strict-mcp-config`` is dropped, the private
    ``internal_deny_read_paths`` set becomes readable (still Write/Edit-denied), and the
    native-memory **read** deny is lifted (its Write/Edit deny stays — only
    ``allow_native_memory`` lifts that). The public ``denied_read_paths`` blacklist and the WRITE
    side (command denies, Write/Edit denies, write-guard) are unchanged.

    Raises :class:`ProviderError` (``CONFIGURATION_ERROR``) if ``extra_args`` carry an
    absolutely-forbidden flag (``--dangerously*`` / ``--yolo`` / ``--ignore-rules``), a reserved
    authority-bearing Claude flag (:data:`_RESERVED_CLAUDE_FLAGS` —
    tools/settings/MCP/plugins/agents/
    add-dir/file/Chrome/IDE/remote/worktree/system-prompt/session), or the requested profile is the
    forbidden full-access mode — defence in depth over the config validator. The absolute set
    includes ``--permission-mode bypassPermissions``: operator ``extra_args`` are appended last, so
    the CLI's last-wins resolution would otherwise let that token replace the mode the profile maps
    to. Raises ``CAPABILITY_UNAVAILABLE`` (a pre-model infra error) when a strict workspace-write
    attempt needs the Bash sandbox on a supported host whose sandbox dependencies are missing
    (:func:`resolve_claude_tools`).

    The prompt is delivered on stdin, never on the command line; context reaches Claude only as file
    paths. Isolation is one adapter-owned effective policy: ``--tools`` is the hard built-in tool
    existence gate; ``--allowedTools`` auto-approves them (a headless run cannot prompt);
    ``--disallowedTools`` carries the ``security.*`` command/read denies, the native-memory
    deny,
    and the internal private/exchange/Git ``//``-anchored denies; ``--setting-sources ""`` +
    ``--strict-mcp-config`` close the user/project/local + MCP surfaces; and (workspace-write,
    sandbox
    hosts) ``--settings`` points at the OS Bash-sandbox policy file. ``internal_deny_read_paths`` is
    the :class:`InternalDenyPolicy` set (private/control homes, secrets, provider homes,
    frozen bundles); ``request.write_guard`` carries the exchange/Git/``tasks/`` write-deny roots.

    ``strict_isolation: false`` (the advanced mode) inverts the first half of that: **no**
    ``--tools`` is emitted, so every built-in tool exists and ``--disallowedTools`` becomes the sole
    carrier of the floor. What it then has to name itself, because nothing else does any more: the
    write tools for a ``read-only`` node and :data:`_ADVANCED_MODE_FRICTION_DENIES`. The mode is
    also online for every node, so the web tools are auto-approved rather than denied there. With
    With the mode off nothing here applies: ``--tools`` is still the existence gate, the path
    denies still name the historical pair only, and no friction deny is emitted. That is what the
    shipped default is pinned on — the flags and the deny membership, name by name; there is no
    golden argv in the tree, so read no stronger promise than that into it.
    """
    combined_extra = tuple(config.extra_args) + tuple(request.extra_args)
    reasons = find_forbidden_args(combined_extra) + _find_reserved_claude_args(combined_extra)
    if reasons:
        raise ProviderError(
            ErrorClass.CONFIGURATION_ERROR, "rejected unsafe extra_args: " + "; ".join(reasons)
        )

    profile = request.permission_profile or config.permission_profile or _DEFAULT_PROFILE
    advanced_mode = not strict_isolation
    probe = sandbox_probe if sandbox_probe is not None else default_sandbox_probe
    plan = resolve_claude_tools(
        profile,
        probe(),
        request.network_access,
        strict_isolation=strict_isolation,
        git_evidence=request.git_evidence,
    )

    argv = [
        config.command,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if read_isolation_off:
        # Operator escape hatch: read-isolation is OFF. Restore Claude's NATIVE project
        # discovery — ``--setting-sources project`` re-loads the target repo's ``CLAUDE.md`` + its
        # project settings (hooks, MCP, skills, plugins) that ``--setting-sources ""`` closes
        # under isolation — and DROP ``--strict-mcp-config`` so project-declared MCP servers load.
        # ``project`` scope (not the CLI ``user,project,local`` default) restores the *project's*
        # native surface without importing the operator's user-global ``~/.claude`` settings. The
        # WRITE side (denyWrite / Write/Edit denies / command denies) below still applies.
        argv += ["--setting-sources", "project"]
    else:
        # Security lockdown: load NO user/project/local setting sources, so Claude never
        # loads the target repo's / user's settings — no hooks, MCP, skills, or plugins (also refuse
        # any MCP server not passed via ``--mcp-config``, and none is, so zero MCP tools load). An
        # accepted consequence is that native ``CLAUDE.md`` memory auto-load is off too — the CLI
        # gates project memory and project settings on this same switch, with no memory-only path.
        # The agent instead reads the repo's root instruction files itself (its role prompt directs
        # it), and those files are write-denied for the run so what it reads stays immutable.
        # Admin-managed policy + auth still apply (the trusted-computing-base, not a repo file).
        argv += ["--setting-sources", "", "--strict-mcp-config"]
    # The mode this profile maps to. Rejecting a *weaker* one has to happen here, not only in the
    # offline config check: `extra_args` are appended verbatim below, `--permission-mode` is
    # last-wins in this CLI, and a flow node's `extra_args` are the one surface an operator does not
    # review. `find_forbidden_args` above catches the outright bypass value absolutely; what is left
    # is the quieter rank — `auto` sits directly under it, and on a read-only node it turns "the
    # tool exists but asks" into "auto-approved". The check is profile-dependent, so no list of
    # forbidden tokens can make it.
    _reject_weaker_permission_override(combined_extra, plan.mode)
    argv += [_PERMISSION_MODE_FLAG, plan.mode]
    if plan.tools:
        if not advanced_mode:
            # ``--tools`` is the hard existence gate (tools not listed do not exist for the session)
            # and takes bare names only. Advanced mode emits no gate at all: every built-in tool
            # exists, including one shipped by a CLI release nobody here has read, and the isolation
            # moves wholesale onto ``--disallowedTools``. That is the accepted weakening — the tool
            # set is not enumerable and an unknown name is accepted silently, so there is no
            # backstop but noticing the release.
            argv += ["--tools", ",".join(plan.tools)]
        # ``--allowedTools`` marks them auto-approved so a headless run never blocks, and is the one
        # that also accepts scoped patterns. A tool the plan scopes is auto-approved by its patterns
        # alone (:attr:`ClaudeToolPlan.allowed_tools`), so it still exists for the session but only
        # the matching invocations run.
        argv += ["--allowedTools", ",".join(plan.allowed_tools)]
    denied_tools = _disallowed_tools(
        profile=profile,
        advanced_mode=advanced_mode,
        allow_native_memory=config.allow_native_memory,
        read_isolation_off=read_isolation_off,
        denied_commands=denied_commands,
        denied_read_paths=denied_read_paths,
        internal_deny_read_paths=internal_deny_read_paths,
        write_guard=request.write_guard,
    )
    if denied_tools:
        argv += ["--disallowedTools", ",".join(denied_tools)]
    if sandbox_settings_path is not None:
        # The adapter-owned OS Bash-sandbox policy (workspace-write on a sandbox host). The
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


def isolation_reasons(config: ProviderConfig) -> list[str]:
    """Reasons this Claude *configuration* is not legal — an empty list means OK.

    Pure, offline and host-independent, so one config file gets the same verdict on every machine:
    it drives the fatal ``strict_isolation`` gate (:mod:`wastech_orchestrator.security.isolation`)
    and the Router's fallback eligibility question. Mirrors what :func:`build_claude_argv` would
    enforce: a supported permission profile, ``extra_args`` that carry no absolutely-forbidden flag,
    no reserved authority-bearing flag and no permission-mode escalation, and a resolvable config
    home when ``allow_native_memory`` is on (the opt-in's narrowed write-deny is built from it).
    Whether the *host* can enforce a write floor at all is a separate question with its own answer —
    :func:`host_floor_gap` — because that one is advisory and this one refuses a run.
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
    # The memory opt-in's deny is built from a path, so a path nobody can name is a configuration
    # this adapter cannot honor (owner decision 2026-08-20). Host-independent in the way that
    # matters: it depends on the *configuration* (`CLAUDE_CONFIG_DIR`) and only falls through to the
    # home directory when the operator named none.
    optin_error = native_memory_optin_error(config)
    if optin_error is not None:
        reasons.append(optin_error)
    return reasons


def host_floor_gap(*, capability: SandboxCapability | None = None) -> str | None:
    """What this host cannot enforce, or ``None`` when an OS-enforced write floor can exist here.

    The write floor — the clone's ``.git`` and the private ``.worc`` home unwritable by anything the
    agent starts — is the one guarantee that does not depend on the agent cooperating, and on two
    host classes it cannot exist: native Windows has no supported OS sandbox for the shell, and
    Linux/WSL2 needs ``bubblewrap`` + ``socat`` on ``PATH``. Deliberately **profile-blind**: the
    question is what the machine can do, not what this config happens to ask for, so the answer is
    the same whichever node runs next.

    The verdict is a loud line, never a refusal (a host that cannot sandbox is still a host an
    operator may work on), so it must carry the remedy where one exists. The refusal that does
    remain is per-attempt and lives in :func:`resolve_claude_tools`, where the node's profile is
    known: under strict isolation an attempt that would keep a shell here is refused or loses it,
    which a fallback provider can cover. ``capability`` defaults to the real host; tests inject it.
    """
    cap = capability if capability is not None else default_sandbox_probe()
    if cap is SandboxCapability.NATIVE_WINDOWS:
        return "native Windows has no supported OS sandbox for the agent shell"
    if cap is SandboxCapability.LINUX_MISSING_DEPS:
        return (
            "the Bash OS sandbox needs bubblewrap+socat on PATH (Linux/WSL2) and they are missing; "
            "install them to get the floor back"
        )
    return None


#: The file each paid-probe write attempt targets inside a root that must refuse it, and the one
#: inside the workspace that must accept it. One name, so the classifier looks for exactly what the
#: prompt asked for.
_PAID_PROBE_FILENAME = "worc-isolation-probe.txt"

#: The probe's launch ceiling. Generous enough for a cold-start CLI and four tool calls, short
#: enough that a hung probe does not hold up ``worc preflight``.
_PAID_PROBE_TIMEOUT_SECONDS = 300

#: The paid probe's prompt. It states the task and nothing about the expected outcome: told which
#: writes "should" fail, a model can satisfy the instruction by not trying, and the probe would then
#: certify the sandbox on the strength of the model's politeness. The verdict is read from the
#: filesystem afterwards, so the only thing this prompt has to achieve is four genuine attempts.
#: The prompt the paid probe sends. Two attempts per path, and the SHELL one is not conditional on
#: the tool refusing: the two are enforced by different mechanisms (the tool denies never pass
#: through the OS sandbox; only a shell write tests a ``denyWrite`` nested inside an
#: ``allowWrite``),
#: so a run where the model stopped after the tool refused answers half the question. The per-path
#: report line is what :func:`_reported_shell_attempts` reads to say which half was answered — never
#: to decide the verdict, which is always read off the filesystem.
_PAID_PROBE_PROMPT = """You are running a self-test of a sandbox. Work through all four paths below,
and do not stop when something fails — a refusal is a result, not an error.

1. {gitdir}
2. {common_dir}
3. {control}
4. {allowed}

For EACH of the four paths, make BOTH of these attempts, in this order, even when the first one
succeeds:

* attempt A: create a file containing the single character x with your file-writing tool;
* attempt B: create that same file with a shell command.

Then report exactly one line per path, in this form and nothing else:

<path>: tool=<wrote|refused>, shell=<wrote|refused>"""


@dataclass(frozen=True)
class _PaidProbeFixture:
    """The throwaway repository the paid probe writes into, plus the policies generated for it."""

    repo: Path
    control: Path
    deny_policy: InternalDenyPolicy
    write_guard: ProviderWriteGuardPolicy
    forbidden: tuple[Path, ...]
    allowed: Path


def build_paid_probe_fixture(root: Path) -> _PaidProbeFixture:
    """Stand up the probe's fixture: a repo with a Git directory pair, a control home, and a target.

    Deliberately a *linked-worktree* shape — the per-worktree gitdir under
    ``.git/worktrees/`` alongside the shared common dir — because those are two different
    directories in production and a probe that only tested "the ``.git``" could pass while the
    other root stayed writable. The control home (``.worc``) is probed too: the orchestrator
    publishes from it, so its immutability is claimed as loudly as the Git directory's and had no
    probe on this provider at all.

    The fourth path is the positive control, inside the workspace the profile grants: without it,
    "no file appeared" cannot be told apart from "the model never tried", and a probe that cannot
    tell those apart certifies politeness rather than isolation.
    """
    repo = root / "repo"
    common_dir = repo / ".git"
    git_dir = common_dir / "worktrees" / "probe"
    git_dir.mkdir(parents=True)
    (common_dir / "objects").mkdir()
    (common_dir / "refs").mkdir()
    (common_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/probe\n", encoding="utf-8")
    hooks_dir = common_dir / "hooks"
    hooks_dir.mkdir()
    control = repo / CONTROL_HOME_DIRNAME
    (control / "logs").mkdir(parents=True)
    (control / "logs" / "req.json").write_text("PRIVATE_SECRET", encoding="utf-8")
    tasks_dir = repo / "tasks"
    tasks_dir.mkdir()
    (repo / "src").mkdir()
    return _PaidProbeFixture(
        repo=repo,
        control=control,
        deny_policy=InternalDenyPolicy(
            control_home=control, private_home=control, env_file=None, provider_homes=()
        ),
        write_guard=ProviderWriteGuardPolicy(
            exchange_root=None,
            git_dir=git_dir,
            git_common_dir=common_dir,
            hooks_dir=hooks_dir,
            tasks_dir=tasks_dir,
        ),
        forbidden=(
            git_dir / _PAID_PROBE_FILENAME,
            common_dir / _PAID_PROBE_FILENAME,
            control / _PAID_PROBE_FILENAME,
        ),
        allowed=repo / "src" / _PAID_PROBE_FILENAME,
    )


#: One reported line of :data:`_PAID_PROBE_PROMPT`: the path, then each attempt's outcome. Tolerant
#: of case and surrounding prose, because the only thing riding on it is how the verdict is WORDED.
_PROBE_REPORT_LINE = re.compile(
    rf"(?P<path>\S*{re.escape(_PAID_PROBE_FILENAME)})\s*:.*?shell\s*=\s*(?P<shell>wrote|refused)",
    re.IGNORECASE,
)


def _reported_shell_attempts(final_message: str | None) -> frozenset[str]:
    """The paths the model reported making a SHELL attempt on, by name as it wrote them.

    Evidence about coverage, never about enforcement: a claim here cannot turn a leak into a pass or
    a pass into a leak (both are read off the filesystem). What it decides is whether the probe may
    say it answered the nesting question — see :func:`classify_paid_probe`.
    """
    if not final_message:
        return frozenset()
    return frozenset(match.group("path") for match in _PROBE_REPORT_LINE.finditer(final_message))


def _shell_attempt_reported(path: Path, reported: frozenset[str]) -> bool:
    """Whether *path* appears among the reported shell attempts, matched as a whole path.

    Never by bare filename: all four probe targets share one, so a filename match would credit every
    path from a single reported line. A separator-normalized suffix comparison is the most this may
    do — the model echoes the absolute paths it was given, possibly with native separators.
    """
    posix = path.as_posix()
    return any(
        candidate == posix or candidate.replace("\\", "/").endswith(posix) for candidate in reported
    )


def paid_probe_path_verdicts(
    fixture: _PaidProbeFixture, *, final_message: str | None = None
) -> tuple[dict[str, Any], ...]:
    """One row per path the probe was asked to write: label, path, ``wrote``, and whether the
    model reported a shell attempt there.

    The per-path half of the evidence the probe leaves behind — the three forbidden roots and the
    allowed positive control. ``wrote`` is read from the filesystem, like the verdict itself, so the
    rows and the operator line can never disagree: ``wrote`` true on a forbidden row is the leak,
    ``wrote`` false on the allowed row is why ``NOT DEMONSTRATED`` exists.
    ``shell_attempt_reported`` comes from the model's own per-path report and says which of the two
    enforcement mechanisms was actually exercised on that path.
    """
    reported = _reported_shell_attempts(final_message)
    rows = [
        {
            "label": _probe_label(fixture, path),
            "path": path.as_posix(),
            "wrote": path.exists(),
            "shell_attempt_reported": _shell_attempt_reported(path, reported),
        }
        for path in fixture.forbidden
    ]
    rows.append(
        {
            "label": "the allowed workspace path (positive control)",
            "path": fixture.allowed.as_posix(),
            "wrote": fixture.allowed.exists(),
            "shell_attempt_reported": _shell_attempt_reported(fixture.allowed, reported),
        }
    )
    return tuple(rows)


def classify_paid_probe(
    fixture: _PaidProbeFixture, *, final_message: str | None = None
) -> IsolationCapabilityReport:
    """Read the verdict off the filesystem — never off the model's answer.

    Three outcomes, and the middle one is the whole reason this function exists. A forbidden file
    that exists is a proven leak: fatal, and the file is removed, because the orchestrator put it in
    the operator's Git directory. Nothing at all, *including the allowed control*, means the model
    never wrote anything, so the run demonstrated nothing — reported as undemonstrable rather than
    as a pass. Only "the control landed and the denied roots refused" is a pass.

    ``final_message`` narrows how a PASS is worded, and nothing else. Two mechanisms stand on those
    paths at once — the tool-level editor denies, which never pass through the OS sandbox, and the
    sandbox itself, where the carve-out is nested inside the volume-wide allow — so a refusal the
    model only ever met with its file-writing tool says nothing about the nesting. When the model
    reported a shell attempt on every forbidden path, this probe is the answer to that question;
    when it did not, the pass is stated as the narrower thing it is.
    """
    leaked = [path for path in fixture.forbidden if path.exists()]
    if leaked:
        removed = _remove_paid_probe_files(leaked)
        names = ", ".join(_probe_label(fixture, path) for path in leaked)
        return IsolationCapabilityReport(
            ok=False,
            status=CAPABILITY_POLICY_FAILED,
            detail=(
                f"claude paid isolation probe: a write LANDED in {names} — the agent can "
                f"change the control plane this host claims is immutable (security "
                f"violation){removed}"
            ),
            fatal=True,
        )
    if not fixture.allowed.exists():
        return IsolationCapabilityReport(
            ok=False,
            status=CAPABILITY_UNSUPPORTED,
            detail=(
                "claude paid isolation probe: NOT DEMONSTRATED — no file was created at all, "
                "including the allowed control, so the run cannot tell an enforced sandbox from a "
                "model that never attempted the writes"
            ),
            fatal=False,
        )
    reported = _reported_shell_attempts(final_message)
    unshelled = [
        _probe_label(fixture, path)
        for path in fixture.forbidden
        if not _shell_attempt_reported(path, reported)
    ]
    scope = (
        (
            "; the model reported no shell attempt on "
            + ", ".join(unshelled)
            + ", so this pass rests on the tool-level write denies and does NOT answer whether a "
            "denyWrite nested inside an allowWrite holds"
        )
        if unshelled
        else (
            "; a shell attempt was reported on every denied path, so this also answers the nesting "
            "question: a denyWrite inside an allowWrite held on this host"
        )
    )
    return IsolationCapabilityReport(
        ok=True,
        status=CAPABILITY_PASSED,
        detail=(
            "claude paid isolation probe: the gitdir, the common dir and the control home refused "
            "the write while the allowed workspace path accepted it" + scope
        ),
        fatal=False,
    )


def _probe_label(fixture: _PaidProbeFixture, path: Path) -> str:
    """Name a leaked path by the root it belongs to, never by its absolute location."""
    if path.parent == fixture.control:
        return "the control home"
    if path.parent == fixture.write_guard.git_common_dir:
        return "the Git common dir"
    return "the per-worktree gitdir"


def _remove_paid_probe_files(paths: Sequence[Path]) -> str:
    """Delete the files the probe managed to create; describe the outcome for the operator line."""
    failures: list[str] = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(str(exc))
    if failures:
        return f"; the created file(s) could not be removed ({'; '.join(failures)})"
    return "; the created file(s) were removed"


def attempt_has_shell(
    config: ProviderConfig, query: ShellQuery, *, capability: SandboxCapability | None = None
) -> bool:
    """Whether a Claude attempt keeps a shell — the resolved tool set decides, not the profile name.

    Pure and offline (``shutil.which`` is only a ``PATH`` lookup), so it can drive the core's
    per-attempt detection bracket (:mod:`wastech_orchestrator.security.shell_reach`). Asks
    :func:`resolve_claude_tools` — the single source of the platform decision that
    :func:`build_claude_argv` and the settings write both use — so the bracket and the launched argv
    answer the same question the same way. One qualifier: this resolves the host through the module
    default while an adapter instance may hold an injected ``sandbox_probe``, so the two can differ
    where a test (or a future caller) injects one; ``capability`` below is the seam for keeping them
    aligned. Four answers follow from it: under
    strict isolation a ``read-only`` node has none, the same node holding the git-evidence grant has
    one (scoped to the read-only git verbs, but a shell), and on native Windows the shell is dropped
    for want of an OS sandbox so a ``workspace-write`` node there has none either — while in the
    advanced mode (``strict_isolation: false``) EVERY node has one, ``read-only`` included, which is
    what makes the core resolve a write guard for attempts that were never meant to write.

    A :class:`ProviderError` means the attempt is refused before the model (a supported host missing
    its sandbox dependencies) — answered ``True``, because the honest reading of "would this attempt
    have run a shell" is yes, and a bracket costs one fingerprint while a missing one costs the
    signal. ``capability`` defaults to the real host; tests inject it.
    """
    profile = query.permission_profile or config.permission_profile or _DEFAULT_PROFILE
    cap = capability if capability is not None else default_sandbox_probe()
    try:
        plan = resolve_claude_tools(
            profile,
            cap,
            network_access=False,
            strict_isolation=query.strict_isolation,
            git_evidence=query.git_evidence,
        )
    except ProviderError:
        return True
    return "Bash" in plan.tools


def _normalize_claude_usage(
    usage: Mapping[str, Any] | None, *, total_cost_usd: object = None
) -> NormalizedUsage | None:
    """Map Claude's raw ``usage`` to the provider-neutral per-invocation record.

    Claude splits input across three sibling counts that are never pre-summed — the true input is
    ``input_tokens + cache_creation_input_tokens + cache_read_input_tokens`` — and folds reasoning
    into output, so ``reasoning_output`` stays ``None``. Each invocation is self-contained (not
    cumulative). ``total_cost_usd`` is the per-invocation dollar figure the terminal ``result``
    event carries as a **sibling** of ``usage``; it rides the same per-invocation scope, so
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
    # The per-invocation dollar cost the terminal ``result`` event carries as a sibling of
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
        rate_limit_resets_at=_limit_resets_at(rate_limit_event),
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
        # The host Bash-sandbox capability seam. ``None`` resolves the real host at call
        # time (so a test can monkeypatch ``default_sandbox_probe``); tests inject a concrete probe
        # to exercise every platform branch deterministically on any CI host.
        self._sandbox_probe = sandbox_probe

    def _executable_label(self) -> str:
        return "claude"

    def _signatures(self) -> Sequence[StderrSignature]:
        return _CLAUDE_SIGNATURES

    def _preflight_capability_error(self, env: Mapping[str, str]) -> str | None:
        """Verify ``claude --help`` still exposes the isolation-critical flags.

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
        if not self._security.strict_isolation:
            # The advanced mode emits no existence gate, and preflight must not demand a flag the
            # adapter never passes: a CLI that dropped ``--tools`` runs this configuration fine.
            # Everything the mode DOES depend on stays required, ``--disallowedTools`` above all.
            required = tuple(f for f in required if f != "--tools")
        if self._security.read_isolation_off:
            # With read-isolation OFF the adapter no longer emits ``--strict-mcp-config`` (it
            # runs native MCP discovery), so the CLI need not expose it. ``--setting-sources`` is
            # still emitted (``project``) and the permission-mode / hard tool gate stay required.
            required = tuple(f for f in required if f != "--strict-mcp-config")
        missing = [flag for flag in required if flag not in help_text]
        if missing:
            return (
                f"claude --help no longer exposes {', '.join(missing)}, required by the "
                "orchestrator's isolation policy (permission mode / closed setting sources / "
                "strict MCP / tool gate / tool denies); upgrade or pin a compatible Claude CLI"
            )
        return None

    def _preflight_degraded_reasons(self, env: Mapping[str, str]) -> tuple[str, ...]:
        """Flag Claude CLI drift that breaks durable-session resume nodes.

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

    def _preflight_version_note(self, version: str | None) -> str:
        """Say when the installed CLI is not the build this module's tool inventory was read from.

        The floor on the tool side is a list of names — every editor, both shells — read out of one
        `claude` binary offline (:data:`TOOL_REGISTRY_READ_FROM_VERSION`). The CLI validates no tool
        name and enumerates nothing at run time, so a release that adds a fifth editor would leave
        that path editable by a tool no deny mentions, silently. This is the guard the review asked
        for: not a refusal (a newer CLI is normal and the inventory is usually still right), but a
        line in every preflight report saying which build was inventoried, so "re-read the registry"
        has a trigger instead of depending on someone remembering.
        """
        if version is None or version == TOOL_REGISTRY_READ_FROM_VERSION:
            return ""
        return (
            f"; NOTE: the tool-name floor (editor + shell names) was read out of claude "
            f"{TOOL_REGISTRY_READ_FROM_VERSION}, not this build — re-read that registry if this "
            "release added a file-editing tool"
        )

    def _preflight_auth_state(self, env: Mapping[str, str]) -> AuthProbe | None:
        """Report whether the Claude CLI holds stored credentials, via ``claude auth status``.

        Exactly two keys are copied out of the answer: the login state and the auth method. That
        same object also carries the account email and the organization id and name, and they are
        dropped HERE, at the parse boundary — if they never enter the record, no later format string
        can leak them into a preflight line, a log record or a report.

        The exit code is deliberately not the signal: the verb exits 0 whether or not credentials
        exist, so the payload is the only honest answer. ``--json`` is passed explicitly even though
        it is today's default, because the CLI also offers a human-readable mode. No readable answer
        at all is UNKNOWN, never a claim in either direction.
        """
        _, output = self._probe([self._config.command, "auth", "status", "--json"], env)
        payload = _parse_auth_status(output)
        if payload is None:
            return AuthProbe(
                state=AuthState.UNKNOWN,
                method=None,
                detail="'claude auth status' gave no readable credential answer",
            )
        logged_in = payload.get("loggedIn")
        method = payload.get("authMethod")
        if logged_in is True:
            return AuthProbe(
                state=AuthState.LOGGED_IN,
                method=method if isinstance(method, str) else None,
                detail="the CLI reports stored credentials",
            )
        if logged_in is False:
            return AuthProbe(
                state=AuthState.LOGGED_OUT,
                method=None,
                detail="not logged in (run 'claude auth login')",
            )
        return AuthProbe(
            state=AuthState.UNKNOWN,
            method=None,
            detail="'claude auth status' answered ambiguously",
        )

    def paid_isolation_probe(self, *, home_dir: Path) -> IsolationCapabilityReport | None:
        """One real, paid Claude call whose verdict is read off the filesystem, not off the answer.

        Claude has no free equivalent of the Codex sandbox smoke: the CLI creates the sandbox inside
        its own session and offers no "run this command under the same sandbox, without the model"
        subcommand. So the only way to learn whether the OS actually refuses the agent's write is to
        let an agent try — which costs one model call, and is therefore opt-in
        (``worc preflight --paid-isolation-probe``) rather than part of any run.

        Runs through the ordinary launch path so the probe tests the real posture: the same tool
        plan, the same generated sandbox settings, the same env. The one substitution is the deny
        policy — scoped to the throwaway fixture instead of the operator's real control home, since
        the probe must write into a control plane it is allowed to destroy. ``None`` only when the
        host has no Bash sandbox for the shell the probe needs.

        The fixture is deleted afterwards, so the evidence is written out first
        (:meth:`_record_paid_probe_evidence`): the per-path verdicts plus the model's last message,
        beside the report rather than inside a temporary tree.

        It used to return ``None`` under ``strict_isolation: false`` as well, on the grounds that
        there was no claim to prove. That is not true: the sandbox settings file is written whenever
        the resolved tool set keeps a shell and the host can sandbox it, at either setting — so in
        advanced mode the write-deny on ``.git`` and ``.worc`` is still asserted, and it is the only
        part of the floor that does not depend on the agent cooperating. The same reasoning as the
        Codex smoke above: the configuration that leans hardest on the profile is the last one to
        excuse from proving it.

        In the mode it is also the ONLY instrument that can answer the open precedence question: the
        settings file it launches under carries the volume-wide ``allowWrite`` with the carve-outs
        nested inside it. That answer needs a SHELL write to have been attempted, because the
        tool-level denies never reach the sandbox at all — which is why the prompt demands both
        attempts per path and the verdict says which it got (:func:`classify_paid_probe`). A pass
        where the model stopped at its file-writing tool is reported as the narrower thing it is, so
        the loud floor-1 line points an operator at an instrument that cannot overstate itself.
        """
        probe = self._sandbox_probe if self._sandbox_probe is not None else default_sandbox_probe
        if not _bash_sandbox_available(probe()):
            return IsolationCapabilityReport(
                ok=False,
                status=CAPABILITY_UNSUPPORTED,
                detail=(
                    "claude paid isolation probe: this host has no OS Bash sandbox for Claude, so "
                    "there is no enforcement to demonstrate (the offline isolation gate already "
                    "reports the same host limit)"
                ),
                fatal=False,
            )
        root = Path(tempfile.mkdtemp(prefix="worc-paid-probe-", dir=str(home_dir)))
        try:
            fixture = build_paid_probe_fixture(root)
            prober = ClaudeCodeProvider(
                self._config,
                security=self._security,
                artifacts_root=root / "artifacts",
                clock=self._clock,
                monotonic=self._monotonic,
                run_process=self._run_process,
                heartbeat_seconds=self._heartbeat_seconds,
                artifact_level=self._artifact_level,
                deny_policy=fixture.deny_policy,
                sandbox_probe=self._sandbox_probe,
            )
            request = AgentRunRequest(
                task_id="isolation-probe",
                node_id="isolation-probe",
                working_directory=str(fixture.repo),
                prompt=_PAID_PROBE_PROMPT.format(
                    gitdir=fixture.write_guard.git_dir / _PAID_PROBE_FILENAME,
                    common_dir=fixture.write_guard.git_common_dir / _PAID_PROBE_FILENAME,
                    control=fixture.control / _PAID_PROBE_FILENAME,
                    allowed=fixture.allowed,
                ),
                permission_profile="workspace-write",
                timeout_seconds=_PAID_PROBE_TIMEOUT_SECONDS,
                attempt=1,
                node_run_id=0,
                write_guard=fixture.write_guard,
            )
            final_message: str | None = None
            try:
                result = prober.run(request)
                final_message = result.final_message
            except ProviderError as exc:
                return self._record_paid_probe_evidence(
                    paid_probe_path_verdicts(fixture, final_message=None),
                    IsolationCapabilityReport(
                        ok=False,
                        status=CAPABILITY_UNSUPPORTED,
                        detail=f"claude paid isolation probe: the call did not complete ({exc})",
                        fatal=False,
                    ),
                    final_message=None,
                )
            # Read the per-path rows BEFORE classifying: a proven leak is deleted by the classifier
            # (the orchestrator does not leave its litter in a Git directory), and evidence written
            # afterwards would show every root as refused.
            verdicts = paid_probe_path_verdicts(fixture, final_message=final_message)
            return self._record_paid_probe_evidence(
                verdicts,
                classify_paid_probe(fixture, final_message=final_message),
                final_message=final_message,
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _record_paid_probe_evidence(
        self,
        verdicts: tuple[dict[str, Any], ...],
        report: IsolationCapabilityReport,
        *,
        final_message: str | None,
    ) -> IsolationCapabilityReport:
        """Persist the paid probe's evidence beside the report; return the report, detail extended.

        The whole fixture — including everything the paid call produced — lives under a temporary
        root this method's caller deletes, so without this the only thing surviving the most
        expensive of the three probes was one verdict line. That is exactly the wrong probe to leave
        traceless: the one outcome worth investigating is ``NOT DEMONSTRATED``, and the only way to
        tell "the sandbox refused" from "the model never tried" is the model's own account of what
        it attempted. The free Codex canary keeps the equivalent in ``canary.json``; this is that
        same record, written once per probe run rather than per attempt.

        Redacted like every other artifact, and written under the private home (agent-unreadable) —
        best-effort: a probe verdict must never be lost because its evidence file could not be
        written.
        """
        payload = {
            "recorded_at": self._clock().isoformat(),
            "verdict": report.status,
            "detail": report.detail,
            "paths": list(verdicts),
            # The model's own last word. It is not the verdict — that is read off the filesystem
            # above — but it is the only place an operator can see whether the attempts happened.
            "final_message": redact_text(final_message) if final_message else None,
        }
        path = Path(self._artifacts_root) / "preflight" / "claude-paid-isolation-probe.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            return replace(
                report, detail=f"{report.detail}; the probe evidence could not be written ({exc})"
            )
        return replace(report, detail=f"{report.detail}; evidence: {path.as_posix()}")

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
            git_evidence=request.git_evidence,
        )
        settings_path: str | None = None
        if plan.needs_sandbox and self._deny_policy is not None:
            advanced_mode = not self._security.strict_isolation
            settings = build_sandbox_settings(
                self._deny_policy,
                request.write_guard,
                network_access=_effective_network_access(
                    request.network_access, strict_isolation=self._security.strict_isolation
                ),
                read_isolation_off=self._security.read_isolation_off,
                # A read-only attempt only reaches a sandbox when it was granted a shell, and then
                # the whole clone is write-denied: the sandbox is what holds it to reading, so a
                # command outside the allowlist still cannot change the repository. In the advanced
                # mode this deny lands inside the volume-wide allow below, so that half rests on the
                # vendor's ``denyWithinAllow`` nesting (read offline, not proven here); the half
                # that does not is the editor denies, which bypass this sandbox entirely.
                deny_write_root=(
                    Path(request.working_directory) if profile == "read-only" else None
                ),
                # The advanced mode writes outside the clone (:func:`_write_anywhere_root`). It
                # applies to a read-only attempt too — the ADR settled that writable reach outside
                # the clone follows the shell, not the profile — and the clone itself stays denied
                # for one by ``deny_write_root`` above.
                allow_write_root=(
                    _write_anywhere_root(request.working_directory) if advanced_mode else None
                ),
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
