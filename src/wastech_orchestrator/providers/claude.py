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
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers._adapter_base import (
    BaseCliProvider,
    ParsedEvents,
)
from wastech_orchestrator.providers.artifacts import ArtifactPaths
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    ErrorClass,
    ProviderError,
    ProviderId,
    build_context_footer,
    build_effective_prompt,
)
from wastech_orchestrator.providers.errors import (
    StderrSignature,
    make_signatures,
    message_for,
)
from wastech_orchestrator.security.forbidden_args import (
    FORBIDDEN_SANDBOX_VALUE,
    find_forbidden_args,
)

__all__ = [
    "ClaudeCodeProvider",
    "ParsedEvents",
    "build_claude_argv",
    "build_context_footer",
    "build_effective_prompt",
    "isolation_reasons",
    "map_permission",
    "parse_stream_json",
]

_DEFAULT_PROFILE = "workspace-write"

# Claude Code permission modes, ordered strict → permissive. The adapter never selects a mode weaker
# than the one a profile maps to, and never selects ``bypassPermissions`` (full bypass) at all.
_MODE_ORDER: tuple[str, ...] = ("plan", "default", "acceptEdits", "bypassPermissions")
_PERMISSION_MODE_FLAG = "--permission-mode"
_BYPASS_MODE = "bypassPermissions"

# Profile → (permission mode, baseline allowed tools). ``read-only`` executes nothing because Edit
# and Write are simply absent from its allowlist (a hard tool-level gate, not the CLI's built-in
# ``plan`` mode): plan mode brings its own interactive UX (``AskUserQuestion``/``ExitPlanMode``,
# ``~/.claude/plans``) that a headless run cannot answer, so a clarifying question raised there
# never reaches the orchestrator's own durable ``human_input`` field (F21) — ``default`` mode has
# no such UX, so a read-only agent that needs to ask surfaces it through the role's structured
# output instead. ``workspace-write`` may edit files and run safe workspace commands without
# prompting — the Claude equivalent of the Codex ``workspace-write`` sandbox.
_PROFILE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "read-only": ("default", ("Read", "Glob", "Grep")),
    "workspace-write": ("acceptEdits", ("Read", "Glob", "Grep", "Edit", "Write", "Bash")),
}

# The web tools added to ``--allowedTools`` only when the flow grants network
# (request.network_access); omitted otherwise so a headless run cannot reach the network (P3.2).
_NETWORK_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")

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
            r"session not found|no such session|unknown session|no conversation"
            r"|could not resume|resume failed|invalid session",
        ),
        (
            ErrorClass.RATE_LIMITED,
            r"rate limit|\b429\b|too many requests|quota exceeded|overloaded"
            r"|session limit|usage limit|hit your (session|usage) limit|limit .* resets",
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


def _native_memory_deny_tools() -> list[str]:
    """Deny ``Write``/``Edit``/``Read`` on the Claude Code config dir so the spawned agent cannot
    read, inject, or leak **native project memory** outside the target working tree (F37).

    Claude Code keeps per-project memory at ``<config_dir>/projects/<cwd-slug>/memory/*.md`` — a
    durable store OUTSIDE the repo, so anything written there escapes ``current.diff``, the commit,
    the redaction net, and the orchestrator's own audit (an unredacted ``originSessionId`` was
    observed leaking). We block it with tool-level path denial rather than isolating
    ``CLAUDE_CONFIG_DIR``: the config dir also holds credentials (file-based on Linux/Windows), so
    redirecting it would break subscription auth there — a deny is auth-safe and cross-platform.

    The config dir is ``CLAUDE_CONFIG_DIR`` (resolved from the same env the child inherits) or the
    ``~/.claude`` default. Emitted as Claude's ``//``-anchored absolute-path glob with POSIX slashes
    (the Node CLI normalizes them), which covers both the default and a custom absolute config dir.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(raw) if raw else Path.home() / ".claude"
    glob = "//" + config_dir.resolve().as_posix().lstrip("/") + "/**"
    return [f"Write({glob})", f"Edit({glob})", f"Read({glob})"]


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
) -> list[str]:
    """Build the ``claude -p`` argv (a list, never a shell string).

    Raises :class:`ProviderError` (``CONFIGURATION_ERROR``) if ``extra_args`` carry an
    absolutely-forbidden flag (``--dangerously*`` / ``--yolo`` / ``--ignore-rules``) or the
    requested profile is the forbidden full-access mode — defence in depth over the P1 config
    validator. A ``--permission-mode`` override in ``extra_args`` (incl. ``bypassPermissions``) is
    **not** rejected here: it is operator-selectable, gated by ``strict_isolation`` at preflight,
    and appended after the orchestrator's own ``--permission-mode`` so the CLI's last-wins
    resolution applies. The prompt is delivered on stdin, never on the command line; context reaches
    Claude only as file paths. ``denied_commands`` and ``denied_read_paths`` (the ``security.*``
    lists) are enforced as ``--disallowedTools`` so the agent can never publish or read secrets. The
    native-memory deny (F37, :func:`_native_memory_deny_tools`) is also appended there, unless
    ``config.allow_native_memory`` is set — the default-off operator opt-in that lets Claude use its
    own native auto-memory (see :class:`ProviderConfig`), accepting an unaudited HOME store.
    """
    combined_extra = tuple(config.extra_args) + tuple(request.extra_args)
    reasons = find_forbidden_args(combined_extra)
    if reasons:
        raise ProviderError(
            ErrorClass.CONFIGURATION_ERROR, "rejected unsafe extra_args: " + "; ".join(reasons)
        )

    profile = request.permission_profile or config.permission_profile or _DEFAULT_PROFILE
    mode, allowed_tools = map_permission(profile)
    if request.network_access:
        # The flow granted network (network_policy): allow the web tools. Absent the grant they are
        # omitted, so a headless ``acceptEdits``/``plan`` run cannot reach the network through them.
        # This only adds network tools — it never relaxes the filesystem permission mode.
        allowed_tools = (*allowed_tools, *_NETWORK_TOOLS)

    argv = [
        config.command,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        _PERMISSION_MODE_FLAG,
        mode,
    ]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]
    denied_tools = _deny_tools_for(denied_commands) + _deny_read_tools_for(denied_read_paths)
    # F37: confine native project memory out of the spawn — unless the operator has opted in to the
    # agent's own native memory (agents.providers.claude.allow_native_memory), a deliberate,
    # default-off risk acceptance (that store is unaudited and outside the redaction net).
    if not config.allow_native_memory:
        denied_tools += _native_memory_deny_tools()
    if denied_tools:
        argv += ["--disallowedTools", ",".join(denied_tools)]
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
    """Reasons the configured Claude isolation cannot be enabled — an empty list means OK.

    Pure and offline (no CLI launched), so it can drive the ``strict_isolation`` preflight
    (:mod:`wastech_orchestrator.security.isolation`). Mirrors what :func:`build_claude_argv`
    would enforce: the permission profile must resolve to a concrete non-``bypassPermissions`` mode,
    and ``extra_args`` must not weaken the sandbox/approvals or that mode.
    """
    profile = config.permission_profile or _DEFAULT_PROFILE
    try:
        mode, _ = map_permission(profile)
    except ProviderError as exc:
        return [str(exc)]
    reasons = [f"extra_args {r}" for r in find_forbidden_args(config.extra_args)]
    try:
        _reject_weaker_permission_override(tuple(config.extra_args), mode)
    except ProviderError as exc:
        reasons.append(str(exc))
    return reasons


def parse_stream_json(stdout_text: str) -> ParsedEvents:
    """Parse a Claude ``stream-json`` event stream into :class:`ParsedEvents`.

    Tolerant of stray non-JSON lines as long as a recognizable terminal ``result`` event is present.
    Raises :class:`ProviderError` (``INVALID_OUTPUT``) when no terminal event can be found.
    """
    final_message: str | None = None
    structured_output: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
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

    def _executable_label(self) -> str:
        return "claude"

    def _signatures(self) -> Sequence[StderrSignature]:
        return _CLAUDE_SIGNATURES

    def _build_argv(self, request: AgentRunRequest, paths: ArtifactPaths) -> tuple[list[str], None]:
        self._write_output_schema(paths, request)
        argv = build_claude_argv(
            self._config,
            request,
            denied_commands=self._security.denied_commands,
            denied_read_paths=self._security.denied_read_paths,
        )
        return argv, None

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
