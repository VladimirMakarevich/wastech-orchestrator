from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

# --- Canonical enumerations (do not duplicate as string literals throughout the code) ---


class ProviderId(StrEnum):
    """Canonical coding-agent provider ids. The only providers the orchestrator supports."""

    CODEX = "codex"
    CLAUDE = "claude"


class Stage(StrEnum):
    REFINEMENT = "refinement"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    REVIEW = "review"
    FIXING = "fixing"
    SUMMARY = "summary"
    PUBLISHING = "publishing"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ErrorClass(StrEnum):
    """Normalized provider error classes (spec §7.1)."""

    BINARY_NOT_FOUND = "binary_not_found"
    UNSUPPORTED_VERSION = "unsupported_version"
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    RATE_LIMITED = "rate_limited"
    NETWORK_UNAVAILABLE = "network_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    PROCESS_CRASHED = "process_crashed"
    INVALID_OUTPUT = "invalid_output"
    PERMISSION_DENIED = "permission_denied"
    CONFIGURATION_ERROR = "configuration_error"
    TASK_FAILURE = "task_failure"


# Error classes that unconditionally allow fallback (spec §7.2).
# authorization_failed / permission_denied are a conditional fallback, decided by the Router,
# not here, so they are not part of this set.
FALLBACK_ELIGIBLE: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.BINARY_NOT_FOUND,
        ErrorClass.UNSUPPORTED_VERSION,
        ErrorClass.AUTHENTICATION_FAILED,
        ErrorClass.RATE_LIMITED,
        ErrorClass.NETWORK_UNAVAILABLE,
        ErrorClass.PROVIDER_UNAVAILABLE,
        ErrorClass.TIMEOUT,
        ErrorClass.PROCESS_CRASHED,
        ErrorClass.INVALID_OUTPUT,
    }
)


# --- Contract data structures (spec §4.3) ---


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    executable_found: bool
    version: str | None
    authenticated: bool
    supports_required_features: bool
    message: str  # diagnostics without secrets


@dataclass(frozen=True)
class AgentRunRequest:
    task_id: str
    stage: Stage
    working_directory: str
    prompt: str
    permission_profile: str
    timeout_seconds: int
    attempt: int
    stage_run_id: int
    # Paths to context artifacts (see spec §6, §10).
    task_path: str | None = None
    plan_path: str | None = None
    diff_path: str | None = None
    check_artifacts_path: str | None = None
    review_artifacts_path: str | None = None
    human_input_path: str | None = None
    # Planning-selected SKILL.md paths — read-only advisory references, never executed (§2.1).
    skill_reference_paths: tuple[str, ...] = ()
    output_schema: dict[str, Any] | None = None
    model: str | None = None
    extra_args: list[str] = field(default_factory=list)
    reasoning: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class NormalizedError:
    error_class: ErrorClass
    message: str  # without secrets


@dataclass(frozen=True)
class AgentRunResult:
    status: RunStatus
    provider: str
    stage: Stage
    attempt: int
    exit_code: int | None
    started_at: str
    finished_at: str
    final_message: str | None = None
    structured_output: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    session_id: str | None = None  # for auditing only
    stdout_path: str | None = None
    stderr_path: str | None = None
    event_log_path: str | None = None
    error: NormalizedError | None = None


class ProviderError(Exception):
    """Provider exception carrying a normalized error class."""

    def __init__(self, error_class: ErrorClass, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class

    @property
    def is_fallback_eligible(self) -> bool:
        return self.error_class in FALLBACK_ELIGIBLE


# --- Provider contract ---


@runtime_checkable
class AgentProvider(Protocol):
    """Common interface for Codex and Claude Code.

    Adapters implement this protocol. They do NOT perform fallback and do NOT change the
    state machine — that is the responsibility of the Router and Core (see
    docs/rules/architecture.md).
    """

    id: str

    def preflight(self) -> ProviderHealth:
        """Check executable availability, version, authentication, and required capabilities."""
        ...

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Execute a single stage run. Infrastructure failures → ProviderError."""
        ...
