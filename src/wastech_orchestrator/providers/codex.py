"""CodexProvider — the Codex CLI adapter.

Implements the :class:`~wastech_orchestrator.providers.base.AgentProvider` contract for
``id = "codex"`` using ``codex exec`` (OpenAI Codex CLI). This is the **only** module that knows
Codex syntax; it composes the provider-agnostic infrastructure (process runner, env allowlist,
redaction, artifacts, error normalization).

Invariants (architecture.md / security.md): the adapter performs **no fallback** and **never**
touches the state machine; it never commits/pushes/PRs. It raises
:class:`~wastech_orchestrator.providers.base.ProviderError` (with the right
:class:`~wastech_orchestrator.providers.base.ErrorClass`) for infrastructure failures, and returns
``AgentRunResult(status=failed, error=task_failure)`` for a clean run that did not satisfy the task.
The CLI is launched as an argv list (no shell); the prompt is fed on stdin; context reaches Codex
only as file paths.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers._adapter_base import (
    BaseCliProvider,
    ParsedEvents,
    build_context_footer,
    build_effective_prompt,
    read_text,
)
from wastech_orchestrator.providers.artifacts import ArtifactPaths
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    ErrorClass,
    ProviderError,
    ProviderId,
)
from wastech_orchestrator.providers.errors import (
    StderrSignature,
    make_signatures,
    message_for,
)
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.security.forbidden_args import (
    FORBIDDEN_SANDBOX_VALUE,
    find_forbidden_args,
)

__all__ = [
    "CodexProvider",
    "ParsedEvents",
    "build_codex_argv",
    "build_context_footer",
    "build_effective_prompt",
    "isolation_reasons",
    "parse_events",
]

_DEFAULT_SANDBOX = "workspace-write"
_LAST_MESSAGE_FILENAME = "last-message.txt"
_OUTPUT_SCHEMA_FILENAME = "output-schema.json"

# Statuses on a terminal Codex ``result`` event that mark the turn as NOT having satisfied the task.
# Any other status (incl. a missing one) is treated as a completed run — task quality is judged
# later by the orchestrator's review/checks, not by the adapter.
_FAILURE_STATUSES = frozenset({"error", "failed", "failure", "incomplete", "aborted"})

# Codex stderr signatures → normalized error classes (most specific first).
_CODEX_SIGNATURES = make_signatures(
    [
        (
            ErrorClass.SESSION_UNAVAILABLE,
            r"session not found|no such session|unknown session|conversation not found"
            r"|no conversation with|thread not found|cannot resume",
        ),
        (ErrorClass.RATE_LIMITED, r"rate limit|\b429\b|too many requests|quota exceeded"),
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
            ErrorClass.UNSUPPORTED_VERSION,
            r"unsupported version|unknown option|unrecognized option|unexpected argument",
        ),
        (
            ErrorClass.PERMISSION_DENIED,
            r"sandbox denied|permission denied|operation not permitted|blocked by sandbox",
        ),
    ]
)


def build_codex_argv(
    config: ProviderConfig,
    request: AgentRunRequest,
    *,
    output_schema_path: str | None,
    last_message_path: str,
) -> list[str]:
    """Build the ``codex exec`` argv (a list, never a shell string).

    Raises :class:`ProviderError` (``CONFIGURATION_ERROR``) if ``extra_args`` would weaken the
    sandbox/approvals or the resolved sandbox is the forbidden full-access mode — defence in depth
    over the P1 config validator. The prompt is delivered on stdin (the trailing ``-``), never on
    the command line.
    """
    combined_extra = tuple(config.extra_args) + tuple(request.extra_args)
    reasons = find_forbidden_args(combined_extra)
    if reasons:
        raise ProviderError(
            ErrorClass.CONFIGURATION_ERROR, "rejected unsafe extra_args: " + "; ".join(reasons)
        )

    sandbox = config.sandbox or config.permission_profile or _DEFAULT_SANDBOX
    if sandbox == FORBIDDEN_SANDBOX_VALUE:
        raise ProviderError(ErrorClass.CONFIGURATION_ERROR, f"sandbox {sandbox!r} is forbidden")

    # Approval policy is a global Codex flag. Both Codex CLI 0.57 and current releases reject it
    # when it is placed after the ``exec`` subcommand.
    argv = [
        config.command,
        "--ask-for-approval",
        "never",
        "exec",
    ]
    # Durable session resume (P2.2; verified on codex-cli 0.139.0): ``codex exec resume <ID>``
    # continues the prior session and accepts the same global security flags + exec options. The
    # SESSION_ID is positional, right after ``resume``; the follow-up prompt is read from stdin (-).
    if request.session_id:
        argv += ["resume", request.session_id]
    argv += [
        "--cd",
        request.working_directory,
        "--sandbox",
        sandbox,
        "--json",
        "--output-last-message",
        last_message_path,
    ]
    if request.network_access:
        # The flow granted network (network_policy). Codex blocks network in the sandbox by default;
        # enable it for the workspace-write sandbox. This toggles ONLY network — the sandbox's
        # filesystem limit and the ``never`` approval policy stay in force (the ceiling holds).
        argv += ["-c", "sandbox_workspace_write.network_access=true"]
    if output_schema_path is not None:
        argv += ["--output-schema", output_schema_path]
    model = request.model or config.model
    if model:
        argv += ["--model", model]
    # Codex --reasoning-effort accepts none/low/medium/high/xhigh (aliases: extra_high, extra-high).
    # "max" is Claude-only; clamp it to xhigh (Codex's ceiling).
    # Session resume is handled above via ``exec resume <SESSION_ID>`` (durable sessions, P2.2).
    _CODEX_EFFORT_MAP: dict[str, str] = {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "xhigh",
    }
    reasoning = request.reasoning or config.reasoning
    if reasoning:
        argv += ["--reasoning-effort", _CODEX_EFFORT_MAP[reasoning]]
    argv += list(combined_extra)
    argv.append("-")  # read the prompt from stdin
    return argv


def isolation_reasons(config: ProviderConfig) -> list[str]:
    """Reasons the configured Codex isolation cannot be enabled — an empty list means OK.

    Pure and offline (no CLI launched), so it can drive the ``strict_isolation`` preflight
    (:mod:`wastech_orchestrator.security.isolation`). Mirrors what :func:`build_codex_argv`
    enforces: a non-``danger-full-access`` sandbox must be selectable and ``extra_args`` must not
    weaken the sandbox/approvals. Codex has no per-tool deny mechanism — the sandbox *is* the
    isolation, so "isolation enabled" means a real sandbox mode is in force.
    """
    sandbox = config.sandbox or config.permission_profile or _DEFAULT_SANDBOX
    reasons: list[str] = []
    if sandbox == FORBIDDEN_SANDBOX_VALUE:
        reasons.append(f"sandbox {sandbox!r} grants full filesystem access (no isolation)")
    reasons.extend(f"extra_args {r}" for r in find_forbidden_args(config.extra_args))
    return reasons


def parse_events(stdout_text: str, last_message_text: str | None = None) -> ParsedEvents:
    """Parse a Codex JSONL event stream into :class:`ParsedEvents`.

    Tolerant of stray non-JSON lines as long as a recognizable terminal ``result`` event is present.
    Raises :class:`ProviderError` (``INVALID_OUTPUT``) when no terminal event can be found.
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

    if not terminal_seen:
        raise ProviderError(ErrorClass.INVALID_OUTPUT, message_for(ErrorClass.INVALID_OUTPUT))

    if last_message_text:
        final_message = last_message_text.strip()
    return ParsedEvents(
        final_message=final_message,
        structured_output=structured_output,
        usage=usage,
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

    def _executable_label(self) -> str:
        return "codex"

    def _signatures(self) -> Sequence[StderrSignature]:
        return _CODEX_SIGNATURES

    def _build_argv(self, request: AgentRunRequest, paths: ArtifactPaths) -> tuple[list[str], str]:
        last_message_path = str(Path(paths.attempt_dir) / _LAST_MESSAGE_FILENAME)
        schema_path = self._write_output_schema(paths, request)
        argv = build_codex_argv(
            self._config,
            request,
            output_schema_path=schema_path,
            last_message_path=last_message_path,
        )
        return argv, last_message_path

    def _parse(
        self,
        raw_stdout: str,
        paths: ArtifactPaths,
        parse_context: str,
        extra_secrets: tuple[str, ...],
    ) -> ParsedEvents:
        # The --output-last-message file is the authoritative final message; redact it on disk too
        # since it may echo agent output.
        last_message_path = parse_context
        last_message_text = read_text(last_message_path)
        if last_message_text and Path(last_message_path).exists():
            Path(last_message_path).write_text(
                redact_text(last_message_text, extra_secrets=extra_secrets), encoding="utf-8"
            )
        return parse_events(raw_stdout, last_message_text or None)

    def _write_output_schema(self, paths: ArtifactPaths, request: AgentRunRequest) -> str | None:
        if request.output_schema is None:
            return None
        schema_path = str(Path(paths.attempt_dir) / _OUTPUT_SCHEMA_FILENAME)
        Path(schema_path).write_text(
            json.dumps(request.output_schema, ensure_ascii=False), encoding="utf-8"
        )
        return schema_path
