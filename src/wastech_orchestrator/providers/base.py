from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

# --- Canonical enumerations (do not duplicate as string literals throughout the code) ---


class ProviderId(StrEnum):
    """Canonical coding-agent provider ids. The only providers the orchestrator supports."""

    CODEX = "codex"
    CLAUDE = "claude"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ErrorClass(StrEnum):
    """Normalized provider error classes."""

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
    # The provider could not resume the requested session (lost transcript / provider reset it). The
    # Router retries the SAME provider once with a fresh session — it is infra (durable sessions,
    # P2.2), never a quality failure, so it never falls back to another provider and never charges a
    # fix iteration. Deliberately NOT in FALLBACK_ELIGIBLE.
    SESSION_UNAVAILABLE = "session_unavailable"


# Error classes that unconditionally allow fallback.
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


# --- Contract data structures ---


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
    #: the flow node's id — the per-node identity for routing audit, artifact namespacing, and the
    #: typed-output / HITL artifact paths. Providers use it only to namespace artifacts.
    node_id: str
    working_directory: str
    prompt: str
    permission_profile: str
    timeout_seconds: int
    attempt: int
    node_run_id: int
    # Paths to context artifacts.
    task_path: str | None = None
    plan_path: str | None = None
    diff_path: str | None = None
    check_artifacts_path: str | None = None
    review_artifacts_path: str | None = None
    human_input_path: str | None = None
    # Planning-selected SKILL.md paths — read-only advisory references, never executed.
    skill_reference_paths: tuple[str, ...] = ()
    output_schema: dict[str, Any] | None = None
    model: str | None = None
    extra_args: list[str] = field(default_factory=list)
    reasoning: str | None = None
    session_id: str | None = None
    # Whether the agent process may reach the network (P3.2 ``network_policy`` enforcement). Default
    # ``False`` — the flow grants network only by declaring ``network_policy``; absent, no network.
    # The adapter maps it onto its sandbox: Codex enables the workspace-write sandbox's network
    # access; Claude allows the WebFetch/WebSearch tools. It only toggles the network — never the
    # filesystem sandbox/approvals (the ceiling stays in force).
    network_access: bool = False


@dataclass(frozen=True)
class NormalizedError:
    error_class: ErrorClass
    message: str  # without secrets


@dataclass(frozen=True)
class AgentRunResult:
    status: RunStatus
    provider: str
    node_id: str
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
    .agents/rules/architecture.md).
    """

    id: str

    def preflight(self) -> ProviderHealth:
        """Check executable availability, version, authentication, and required capabilities."""
        ...

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Execute a single stage run. Infrastructure failures → ProviderError."""
        ...
