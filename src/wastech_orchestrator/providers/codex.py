"""CodexProvider — the Codex CLI adapter (spec §4.4).

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
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.observability.progress import run_with_heartbeat
from wastech_orchestrator.providers.artifacts import (
    ArtifactPaths,
    create_attempt_dir,
    write_request_artifact,
    write_result_artifact,
)
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AgentRunResult,
    ErrorClass,
    NormalizedError,
    ProviderError,
    ProviderHealth,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.providers.errors import classify, make_signatures, message_for
from wastech_orchestrator.providers.process import ProcessResult, run_process
from wastech_orchestrator.providers.redaction import (
    REDACTED,
    is_sensitive_key,
    normalized_session_id,
    read_denied_secrets,
    redact_mapping,
    redact_text,
)
from wastech_orchestrator.security.env import build_child_env
from wastech_orchestrator.security.forbidden_args import (
    FORBIDDEN_SANDBOX_VALUE,
    find_forbidden_args,
)

_DEFAULT_SANDBOX = "workspace-write"
_PREFLIGHT_TIMEOUT_SECONDS = 10
_LAST_MESSAGE_FILENAME = "last-message.txt"
_OUTPUT_SCHEMA_FILENAME = "output-schema.json"
_LOG = logging.getLogger(__name__)

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

# The injected process-runner seam (defaults to the real one).
RunProcess = Callable[..., ProcessResult]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ParsedEvents:
    """The fields extracted from a Codex JSONL event stream."""

    final_message: str | None
    structured_output: dict[str, Any] | None
    usage: dict[str, Any] | None
    session_id: str | None
    succeeded: bool


def build_context_footer(request: AgentRunRequest) -> str:
    """Render the non-``None`` context file paths as a deterministic footer (paths only, §19.5)."""
    fields = (
        ("task", request.task_path),
        ("plan", request.plan_path),
        ("diff", request.diff_path),
        ("checks", request.check_artifacts_path),
        ("review", request.review_artifacts_path),
        ("human_input", request.human_input_path),
    )
    present = [(label, path) for label, path in fields if path]
    skill_lines = [
        f"- skill (read-only reference; advisory, do not execute): {path}"
        for path in request.skill_reference_paths
    ]
    if not present and not skill_lines:
        return ""
    lines = ["Context files (read them as needed; do not assume their contents):"]
    lines += [f"- {label}: {path}" for label, path in present]
    lines += skill_lines
    return "\n".join(lines)


def build_effective_prompt(request: AgentRunRequest) -> str:
    """Combine the Core-assembled prompt with the context-files footer."""
    footer = build_context_footer(request)
    if not footer:
        return request.prompt
    return f"{request.prompt}\n\n{footer}"


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
    (:mod:`wastech_orchestrator.security.isolation`, §12.8). Mirrors what :func:`build_codex_argv`
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


class CodexProvider:
    """Codex CLI adapter implementing the ``AgentProvider`` protocol."""

    id: str = ProviderId.CODEX.value

    def __init__(
        self,
        config: ProviderConfig,
        *,
        security: SecurityConfig,
        artifacts_root: str | Path,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        run_process: RunProcess = run_process,
        heartbeat_seconds: float = 30.0,
    ) -> None:
        self._config = config
        self._security = security
        self._artifacts_root = Path(artifacts_root)
        self._clock = clock
        self._monotonic = monotonic
        self._run_process = run_process
        self._heartbeat_seconds = heartbeat_seconds

    def preflight(self) -> ProviderHealth:
        """Detect the executable and parse its version (auth is best-effort/offline in P2)."""
        env = build_child_env(self._security.allowed_environment)
        with tempfile.TemporaryDirectory() as scratch:
            stdout_path = os.path.join(scratch, "version.out")
            proc = self._run_process(
                [self._config.command, "--version"],
                cwd=scratch,
                env=env,
                timeout_seconds=_PREFLIGHT_TIMEOUT_SECONDS,
                stdout_path=stdout_path,
                monotonic=self._monotonic,
            )
            stdout_text = _read_text(stdout_path)

        if proc.launch_error is not None:
            return ProviderHealth(
                provider_id=self.id,
                executable_found=False,
                version=None,
                authenticated=False,
                supports_required_features=False,
                message="codex executable not found",
            )
        if proc.timed_out or proc.exit_code != 0:
            return ProviderHealth(
                provider_id=self.id,
                executable_found=True,
                version=None,
                authenticated=False,
                supports_required_features=False,
                message="codex was found but 'codex --version' did not succeed",
            )
        version = _parse_version(stdout_text)
        return ProviderHealth(
            provider_id=self.id,
            executable_found=True,
            version=version,
            authenticated=True,
            supports_required_features=version is not None,
            message=f"codex {version or 'unknown version'} available",
        )

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Execute a single Codex stage run. Infrastructure failures raise ``ProviderError``."""
        started_at = self._clock().isoformat()
        paths = create_attempt_dir(
            self._artifacts_root,
            request.task_id,
            request.node_id,
            request.attempt,
            self.id,
            node_run_id=request.node_run_id,
        )
        last_message_path = str(Path(paths.attempt_dir) / _LAST_MESSAGE_FILENAME)
        schema_path = self._write_output_schema(paths, request)

        try:
            argv = build_codex_argv(
                self._config,
                request,
                output_schema_path=schema_path,
                last_message_path=last_message_path,
            )
        except ProviderError:
            self._write_request(paths, request, argv=None)
            raise

        self._write_request(paths, request, argv=argv)

        env = build_child_env(self._security.allowed_environment)
        log = bind(
            _LOG,
            task_id=request.task_id,
            node_id=request.node_id,
            provider=self.id,
            attempt=request.attempt,
        )
        proc = run_with_heartbeat(
            lambda: self._run_process(
                argv,
                cwd=request.working_directory,
                env=env,
                timeout_seconds=request.timeout_seconds,
                stdout_path=paths.stdout_path,
                stdin_text=build_effective_prompt(request),
                monotonic=self._monotonic,
            ),
            logger=log,
            message="provider heartbeat",
            interval_seconds=self._heartbeat_seconds,
            fields={"timeout_seconds": request.timeout_seconds},
        )
        finished_at = self._clock().isoformat()

        # Redact every captured sink before it is written (§12.6): a leaked secret must never land
        # in stdout.log or events.jsonl. Parsing uses the in-memory raw stream for correctness.
        extra_secrets = self._extra_secrets(request)
        raw_stdout = _read_text(paths.stdout_path)
        redacted_stdout = redact_text(raw_stdout, extra_secrets=extra_secrets)
        Path(paths.stdout_path).write_text(redacted_stdout, encoding="utf-8")
        Path(paths.stderr_path).write_text(
            redact_text(proc.stderr_text, extra_secrets=extra_secrets), encoding="utf-8"
        )
        Path(paths.events_path).write_text(redacted_stdout, encoding="utf-8")

        # Infrastructure failure (launch / timeout / abnormal exit) → normalized error → raise.
        if proc.launch_error is not None or proc.timed_out or proc.exit_code != 0:
            error = classify(
                exit_code=proc.exit_code,
                stderr_text=proc.stderr_text,
                timed_out=proc.timed_out,
                launch_error=proc.launch_error,
                signatures=_CODEX_SIGNATURES,
            )
            self._finalize_failure(paths, request, started_at, finished_at, proc, error)
            raise ProviderError(error.error_class, error.message)

        # Clean exit: parse the structured event stream (the --output-last-message file is the
        # authoritative final message; redact it on disk too since it may echo agent output).
        last_message_text = _read_text(last_message_path)
        if last_message_text and Path(last_message_path).exists():
            Path(last_message_path).write_text(
                redact_text(last_message_text, extra_secrets=extra_secrets), encoding="utf-8"
            )
        try:
            parsed = parse_events(raw_stdout, last_message_text or None)
        except ProviderError as exc:
            error = NormalizedError(exc.error_class, str(exc))
            self._finalize_failure(paths, request, started_at, finished_at, proc, error)
            raise

        # The raw session id lives ONLY in state.db (durable sessions, P2.2). The resume id was
        # already redacted via ``extra_secrets``; scrub the freshly emitted id from the on-disk
        # streams too, while keeping the raw id on the in-memory result for the lineage store.
        if parsed.session_id:
            self._scrub_raw_session(paths, parsed.session_id)

        final_message = (
            redact_text(parsed.final_message, extra_secrets=extra_secrets)
            if parsed.final_message
            else None
        )
        usage = redact_mapping(parsed.usage, extra_secrets=extra_secrets) if parsed.usage else None
        if parsed.succeeded:
            status, error_obj = RunStatus.SUCCEEDED, None
        else:
            status = RunStatus.FAILED
            error_obj = NormalizedError(
                ErrorClass.TASK_FAILURE, message_for(ErrorClass.TASK_FAILURE)
            )

        result = AgentRunResult(
            status=status,
            provider=self.id,
            node_id=request.node_id,
            attempt=request.attempt,
            exit_code=proc.exit_code,
            started_at=started_at,
            finished_at=finished_at,
            final_message=final_message,
            structured_output=parsed.structured_output,
            usage=usage,
            session_id=parsed.session_id,
            stdout_path=paths.stdout_path,
            stderr_path=paths.stderr_path,
            event_log_path=paths.events_path,
            error=error_obj,
        )
        # The on-disk result.json carries the normalized (non-secret) session id; the raw id is
        # returned in-memory for the orchestrator's editing_lineage store (state.db only).
        write_result_artifact(paths, _redact_result_session(result))
        return result

    # --- internals ---

    def _scrub_raw_session(self, paths: ArtifactPaths, raw_session_id: str) -> None:
        """Replace a raw session id with :data:`REDACTED` in the on-disk stdout/events streams."""
        for path in (paths.stdout_path, paths.events_path):
            existing = _read_text(path)
            if raw_session_id and raw_session_id in existing:
                Path(path).write_text(existing.replace(raw_session_id, REDACTED), encoding="utf-8")

    def _write_output_schema(self, paths: ArtifactPaths, request: AgentRunRequest) -> str | None:
        if request.output_schema is None:
            return None
        schema_path = str(Path(paths.attempt_dir) / _OUTPUT_SCHEMA_FILENAME)
        Path(schema_path).write_text(
            json.dumps(request.output_schema, ensure_ascii=False), encoding="utf-8"
        )
        return schema_path

    def _write_request(
        self, paths: ArtifactPaths, request: AgentRunRequest, *, argv: list[str] | None
    ) -> None:
        representation = self._request_representation(request, argv)
        redacted = redact_mapping(representation, extra_secrets=self._extra_secrets(request))
        write_request_artifact(paths, redacted)

    def _request_representation(
        self, request: AgentRunRequest, argv: list[str] | None
    ) -> dict[str, Any]:
        context_paths = {
            "task_path": request.task_path,
            "plan_path": request.plan_path,
            "diff_path": request.diff_path,
            "check_artifacts_path": request.check_artifacts_path,
            "review_artifacts_path": request.review_artifacts_path,
            "human_input_path": request.human_input_path,
        }
        return {
            "provider": self.id,
            "task_id": request.task_id,
            "node_id": request.node_id,
            "node_run_id": request.node_run_id,
            "attempt": request.attempt,
            "working_directory": request.working_directory,
            "permission_profile": request.permission_profile,
            "timeout_seconds": request.timeout_seconds,
            "model": request.model or self._config.model or None,
            "prompt": request.prompt,
            "context_paths": {k: v for k, v in context_paths.items() if v},
            "extra_args": list(request.extra_args),
            "config_extra_args": list(self._config.extra_args),
            "argv": argv,
        }

    def _finalize_failure(
        self,
        paths: ArtifactPaths,
        request: AgentRunRequest,
        started_at: str,
        finished_at: str,
        proc: ProcessResult,
        error: NormalizedError,
    ) -> None:
        result = AgentRunResult(
            status=RunStatus.FAILED,
            provider=self.id,
            node_id=request.node_id,
            attempt=request.attempt,
            exit_code=proc.exit_code,
            started_at=started_at,
            finished_at=finished_at,
            stdout_path=paths.stdout_path,
            stderr_path=paths.stderr_path,
            event_log_path=paths.events_path,
            error=error,
        )
        write_result_artifact(paths, result)

    def _extra_secrets(self, request: AgentRunRequest) -> tuple[str, ...]:
        """Literal secrets to redact: secret-named parent env values + denied-read file contents +
        the raw resume session id (durable sessions, P2.2 — it must never leave state.db, so it is
        scrubbed from the request argv / stdout / stderr / events / result)."""
        session = (request.session_id,) if request.session_id else ()
        return (
            self._secret_env_values()
            + read_denied_secrets(request.working_directory, self._security.denied_read_paths)
            + session
        )

    def _secret_env_values(self) -> tuple[str, ...]:
        """Values of non-allowlisted, secret-named parent env vars, for defensive redaction."""
        allowed = set(self._security.allowed_environment)
        return tuple(
            value
            for key, value in os.environ.items()
            if key not in allowed and len(value) >= 8 and is_sensitive_key(key)
        )


def _read_text(path: str) -> str:
    candidate = Path(path)
    if not candidate.exists():
        return ""
    return candidate.read_text(encoding="utf-8", errors="replace")


def _redact_result_session(result: AgentRunResult) -> AgentRunResult:
    """A copy of ``result`` whose session id is the normalized (non-secret) form, for the artifact.

    The raw session id is kept on the in-memory result (so the orchestrator can persist it to the
    ``editing_lineage`` store, state.db only) and never written to ``result.json``.
    """
    if result.session_id is None:
        return result
    return replace(result, session_id=normalized_session_id(result.session_id))


def _parse_version(text: str) -> str | None:
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
    return match.group(1) if match else None
