"""ClaudeCodeProvider — the Claude Code CLI adapter (spec §4.4).

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
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
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
    is_sensitive_key,
    read_denied_secrets,
    redact_mapping,
    redact_text,
)
from wastech_orchestrator.security.env import build_child_env
from wastech_orchestrator.security.forbidden_args import (
    FORBIDDEN_SANDBOX_VALUE,
    find_forbidden_args,
)

_DEFAULT_PROFILE = "workspace-write"
_PREFLIGHT_TIMEOUT_SECONDS = 10

# Claude Code permission modes, ordered strict → permissive. The adapter never selects a mode weaker
# than the one a profile maps to, and never selects ``bypassPermissions`` (full bypass) at all.
_MODE_ORDER: tuple[str, ...] = ("plan", "default", "acceptEdits", "bypassPermissions")
_PERMISSION_MODE_FLAG = "--permission-mode"
_BYPASS_MODE = "bypassPermissions"

# Profile → (permission mode, baseline allowed tools). ``read-only`` proposes but executes nothing;
# ``workspace-write`` may edit files and run safe workspace commands without prompting — the Claude
# equivalent of the Codex ``workspace-write`` sandbox.
_PROFILE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "read-only": ("plan", ("Read", "Glob", "Grep")),
    "workspace-write": ("acceptEdits", ("Read", "Glob", "Grep", "Edit", "Write", "Bash")),
}

# Statuses on the terminal ``result`` event that mark the turn as NOT having satisfied the task. Any
# other outcome is treated as a completed run — task quality is judged later by the orchestrator's
# review/checks, not by the adapter.
_SUCCESS_SUBTYPE = "success"

# Claude stderr signatures → normalized error classes (most specific first).
_CLAUDE_SIGNATURES = make_signatures(
    [
        (
            ErrorClass.RATE_LIMITED,
            r"rate limit|\b429\b|too many requests|quota exceeded|overloaded",
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
            ErrorClass.UNSUPPORTED_VERSION,
            r"unsupported version|unknown option|unrecognized option|unexpected argument",
        ),
        (
            ErrorClass.PERMISSION_DENIED,
            r"permission denied|not allowed|operation not permitted|blocked",
        ),
    ]
)

# The injected process-runner seam (defaults to the real one).
RunProcess = Callable[..., ProcessResult]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ParsedEvents:
    """The fields extracted from a Claude ``stream-json`` event stream."""

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
    )
    present = [(label, path) for label, path in fields if path]
    if not present:
        return ""
    lines = ["Context files (read them as needed; do not assume their contents):"]
    lines += [f"- {label}: {path}" for label, path in present]
    return "\n".join(lines)


def build_effective_prompt(request: AgentRunRequest) -> str:
    """Combine the Core-assembled prompt with the context-files footer."""
    footer = build_context_footer(request)
    if not footer:
        return request.prompt
    return f"{request.prompt}\n\n{footer}"


def _deny_tools_for(denied_commands: Sequence[str]) -> list[str]:
    """Translate the denied-commands blacklist into Claude ``Bash(<cmd>:*)`` tool patterns (§12).

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
    """Translate ``denied_read_paths`` into Claude ``Read(<glob>)`` disallowed-tool patterns (§12).

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
    """Reject an ``extra_args`` ``--permission-mode`` that is weaker than the required mode.

    Defence-in-depth over the P1 config validator: a task or config must never relax the profile the
    adapter was handed. ``--dangerously-skip-permissions`` is already rejected by
    :func:`find_forbidden_args`; this also blocks the ``--permission-mode bypassPermissions`` vector
    and any mode more permissive than ``required_mode``.
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
    output_schema_path: str | None = None,
) -> list[str]:
    """Build the ``claude -p`` argv (a list, never a shell string).

    Raises :class:`ProviderError` (``CONFIGURATION_ERROR``) if ``extra_args`` would weaken isolation
    or the requested profile is the forbidden full-access mode — defence in depth over the P1 config
    validator. The prompt is delivered on stdin, never on the command line; context reaches Claude
    only as file paths. ``denied_commands`` and ``denied_read_paths`` (the ``security.*`` lists) are
    enforced as ``--disallowedTools`` so the agent can never publish or read secret files (§12).
    """
    combined_extra = tuple(config.extra_args) + tuple(request.extra_args)
    reasons = find_forbidden_args(combined_extra)
    if reasons:
        raise ProviderError(
            ErrorClass.CONFIGURATION_ERROR, "rejected unsafe extra_args: " + "; ".join(reasons)
        )

    profile = request.permission_profile or config.permission_profile or _DEFAULT_PROFILE
    mode, allowed_tools = map_permission(profile)
    _reject_weaker_permission_override(combined_extra, mode)

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
    if denied_tools:
        argv += ["--disallowedTools", ",".join(denied_tools)]
    model = request.model or config.model
    if model:
        argv += ["--model", model]
    if config.max_turns is not None:
        argv += ["--max-turns", str(config.max_turns)]
    argv += list(combined_extra)
    return argv


def isolation_reasons(config: ProviderConfig) -> list[str]:
    """Reasons the configured Claude isolation cannot be enabled — an empty list means OK.

    Pure and offline (no CLI launched), so it can drive the ``strict_isolation`` preflight
    (:mod:`wastech_orchestrator.security.isolation`, §12.8). Mirrors what :func:`build_claude_argv`
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
        if event_type == "result":
            terminal_seen = True
            subtype = str(event.get("subtype", _SUCCESS_SUBTYPE)).lower()
            is_error = bool(event.get("is_error", subtype != _SUCCESS_SUBTYPE))
            succeeded = (not is_error) and subtype == _SUCCESS_SUBTYPE
            text = event.get("result")
            if isinstance(text, str):
                final_message = text
            output = event.get("structured_output")
            if isinstance(output, dict):
                structured_output = output
            run_usage = event.get("usage")
            if isinstance(run_usage, dict):
                usage = run_usage

    if not terminal_seen:
        raise ProviderError(ErrorClass.INVALID_OUTPUT, message_for(ErrorClass.INVALID_OUTPUT))

    return ParsedEvents(
        final_message=final_message,
        structured_output=structured_output,
        usage=usage,
        session_id=session_id,
        succeeded=succeeded,
    )


class ClaudeCodeProvider:
    """Claude Code CLI adapter implementing the ``AgentProvider`` protocol."""

    id: str = ProviderId.CLAUDE.value

    def __init__(
        self,
        config: ProviderConfig,
        *,
        security: SecurityConfig,
        artifacts_root: str | Path,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        run_process: RunProcess = run_process,
    ) -> None:
        self._config = config
        self._security = security
        self._artifacts_root = Path(artifacts_root)
        self._clock = clock
        self._monotonic = monotonic
        self._run_process = run_process

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
                message="claude executable not found",
            )
        if proc.timed_out or proc.exit_code != 0:
            return ProviderHealth(
                provider_id=self.id,
                executable_found=True,
                version=None,
                authenticated=False,
                supports_required_features=False,
                message="claude was found but 'claude --version' did not succeed",
            )
        version = _parse_version(stdout_text)
        return ProviderHealth(
            provider_id=self.id,
            executable_found=True,
            version=version,
            authenticated=True,
            supports_required_features=version is not None,
            message=f"claude {version or 'unknown version'} available",
        )

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Execute a single Claude stage run. Infrastructure failures raise ``ProviderError``."""
        started_at = self._clock().isoformat()
        paths = create_attempt_dir(
            self._artifacts_root, request.task_id, request.stage, request.attempt, self.id
        )
        schema_path = self._write_output_schema(paths, request)

        try:
            argv = build_claude_argv(
                self._config,
                request,
                denied_commands=self._security.denied_commands,
                denied_read_paths=self._security.denied_read_paths,
                output_schema_path=schema_path,
            )
        except ProviderError:
            self._write_request(paths, request, argv=None)
            raise

        self._write_request(paths, request, argv=argv)

        env = build_child_env(self._security.allowed_environment)
        proc = self._run_process(
            argv,
            cwd=request.working_directory,
            env=env,
            timeout_seconds=request.timeout_seconds,
            stdout_path=paths.stdout_path,
            stdin_text=build_effective_prompt(request),
            monotonic=self._monotonic,
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
                signatures=_CLAUDE_SIGNATURES,
            )
            self._finalize_failure(paths, request, started_at, finished_at, proc, error)
            raise ProviderError(error.error_class, error.message)

        # Clean exit: parse the structured event stream.
        try:
            parsed = parse_stream_json(raw_stdout)
        except ProviderError as exc:
            error = NormalizedError(exc.error_class, str(exc))
            self._finalize_failure(paths, request, started_at, finished_at, proc, error)
            raise

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
            stage=request.stage,
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
        write_result_artifact(paths, result)
        return result

    # --- internals ---

    def _write_output_schema(self, paths: ArtifactPaths, request: AgentRunRequest) -> str | None:
        if request.output_schema is None:
            return None
        schema_path = str(Path(paths.attempt_dir) / "output-schema.json")
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
        }
        return {
            "provider": self.id,
            "task_id": request.task_id,
            "stage": request.stage.value,
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
            stage=request.stage,
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
        """Literal secrets to redact: secret-named parent env values + denied-read file contents."""
        return self._secret_env_values() + read_denied_secrets(
            request.working_directory, self._security.denied_read_paths
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


def _parse_version(text: str) -> str | None:
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
    return match.group(1) if match else None
