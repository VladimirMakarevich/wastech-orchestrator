from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# --- Canonical enumerations (do not duplicate as string literals throughout the code) ---


class ProviderId(StrEnum):
    """Canonical coding-agent provider ids. The only providers the orchestrator supports."""

    CODEX = "codex"
    CLAUDE = "claude"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class UsageScope(StrEnum):
    """How a provider's normalized token counts are scoped.

    Codex reports ``turn.completed.usage`` cumulatively for the whole session (a resume includes
    every prior turn), so its normalized record is ``SESSION_CUMULATIVE`` and the orchestrator must
    subtract the session's previous snapshot to get a per-run figure. Claude reports each invocation
    independently, so its record is ``PER_INVOCATION`` and is already per-run.
    """

    SESSION_CUMULATIVE = "session_cumulative"
    PER_INVOCATION = "per_invocation"


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
    # The provider CLI rejected OUR invocation with an argparse/usage error (bad flags/args we
    # built; codex uses exit 2) — distinct from a genuine UNSUPPORTED_VERSION. Deliberately NOT in
    # FALLBACK_ELIGIBLE: a bad argv we generate must surface loudly and never silently fail over to
    # the other provider (F38 was masked exactly this way, classified as an unsupported version).
    INVALID_INVOCATION = "invalid_invocation"
    # The provider rejected OUR model request with a model/schema HTTP 400 (bad request /
    # unsupported schema / unsupported parameter) — a different layer from INVALID_INVOCATION (the
    # CLI argv). Deliberately NOT in FALLBACK_ELIGIBLE: a 400 must surface loudly and never silently
    # fail over to the other provider, which typically 400s the same request (it would just be
    # misread as a generic PROCESS_CRASHED and burn the fallback provider).
    MODEL_REQUEST_INVALID = "model_request_invalid"
    TASK_FAILURE = "task_failure"
    # Grep the tag ``no-work-infra`` to find every site and revert as one unit if we drop it.
    # The provider emitted a parseable terminal event but did NO work: zero output tokens, no
    # structured output, and not an ``error_max_turns`` stop. This is the GENERIC no-work net (the
    # specific rate-limit signature is caught first as RATE_LIMITED) — a dead run masquerading as a
    # quality ``task_failure``. Distinct from INVALID_OUTPUT (no terminal event AT ALL): here a
    # terminal event arrived, it just carried nothing. RAISED (never returned) so the Router falls
    # over to the other provider; deliberately in FALLBACK_ELIGIBLE but NOT PARK_ELIGIBLE — a
    # possibly-permanent no-work must fail (not hold the single queue slot for a park window); a
    # recognized transient limit keeps its own RATE_LIMITED park.
    AGENT_NO_PROGRESS = "agent_no_progress"
    # The provider could not resume the requested session (lost transcript / provider reset it). The
    # Router retries the SAME provider once with a fresh session — it is infra (durable sessions,
    # P2.2), never a quality failure, so it never falls back to another provider and never charges a
    # fix iteration. Deliberately NOT in FALLBACK_ELIGIBLE.
    SESSION_UNAVAILABLE = "session_unavailable"
    # An operator stop killed the agent mid-run (reliable-stop). Produced only by the Router when a
    # cancellation was requested, never inferred from an exit code — this is what makes a stop-kill
    # distinguishable from a genuine PROCESS_CRASHED. Deliberately NOT in FALLBACK_ELIGIBLE (never
    # respawn a fresh agent after a stop) nor TRANSIENT_RETRYABLE; the Core parks the task instead.
    CANCELLED = "cancelled"
    # The provider process-tree quiescence barrier could not prove the containment empty (WRI-012):
    # a background/detached/reparented descendant may still be running and writing the repo/exchange
    # after the root exited. This is a SECURITY / manual-action condition, never a quality failure.
    # Deliberately NOT in FALLBACK_ELIGIBLE (never respawn a fresh agent while an unknown writer may
    # be live) nor PARK_ELIGIBLE (an auto-resume must not paper over an uncontained process); the
    # Core routes it to ``manual_action_required`` and the children-file handle is retained.
    CONTAINMENT_UNVERIFIED = "containment_unverified"


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
        ErrorClass.AGENT_NO_PROGRESS,  # EXPERIMENTAL(no-work-infra) — remove with the class
    }
)

# Transient infra classes the Router retries on the SAME provider (with backoff) before falling
# back (Option A — bounded same-provider transient retry). A deliberate STRICT SUBSET of
# FALLBACK_ELIGIBLE: TIMEOUT is excluded (a timeout often means long/partial work already ran — a
# retry risks duplicating it) and RATE_LIMITED is excluded (it wants a long defer, not a tight
# retry loop — that is the fallback / capacity-gate's job).
TRANSIENT_RETRYABLE: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.PROVIDER_UNAVAILABLE,
        ErrorClass.NETWORK_UNAVAILABLE,
    }
)

# Infra classes that, once every provider is exhausted, the orchestrator defers into a resumable
# park (B-lite) rather than failing terminally. A SUPERSET of TRANSIENT_RETRYABLE: it adds
# RATE_LIMITED, which is NOT a tight same-provider retry (it stays out of TRANSIENT_RETRYABLE) but
# IS a long, resumable defer — a subscription/session limit resets on its own window, so the task
# waits it out and resumes instead of burning the queue or a fix budget.
PARK_ELIGIBLE: frozenset[ErrorClass] = TRANSIENT_RETRYABLE | frozenset({ErrorClass.RATE_LIMITED})


# --- Contract data structures ---


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    executable_found: bool
    version: str | None
    authenticated: bool
    supports_required_features: bool
    message: str  # diagnostics without secrets
    # Advisory degradations that are FATAL only when this provider has no fallback (the sole allowed
    # provider), else a warning. The adapter detects them (it knows CLI syntax); ``run_preflight``
    # applies the fallback-aware verdict (it knows ``agents.allowed``). Secret-free by contract.
    degraded_reasons: tuple[str, ...] = ()


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
    # WRI-011 frozen repository-instruction injection file (redacted exchange copy of the root
    # AGENTS.md/CLAUDE.md/AGENTS.override.md concatenation). The adapter injects it through its
    # controlled instruction layer (Claude ``--append-system-prompt-file``; Codex a delimited
    # developer block) AND disables provider-native live project-instruction discovery, so the agent
    # never picks up a mutated live file mid-task. ``None`` when the repo has no tracked root docs.
    repository_instructions_path: str | None = None
    # Planning-selected SKILL.md paths — read-only advisory references, never executed.
    skill_reference_paths: tuple[str, ...] = ()
    output_schema: dict[str, Any] | None = None
    model: str | None = None
    extra_args: list[str] = field(default_factory=list)
    reasoning: str | None = None
    session_id: str | None = None
    # The resumed session's previous cumulative output-token count, for the no-work guard only.
    # Set by the orchestrator only when ``session_id`` resumes a cumulative-scope session; the guard
    # subtracts it so a resumed run that produced no NEW output is recognized (a cumulative
    # ``output_tokens`` is never 0 on a resume). Inert whenever ``session_id`` is ``None``.
    resume_baseline_output_tokens: int | None = None
    # Whether the agent process may reach the network (P3.2 ``network_policy`` enforcement). Default
    # ``False`` — the flow grants network only by declaring ``network_policy``; absent, no network.
    # The adapter maps it onto its sandbox: Codex enables the workspace-write sandbox's network
    # access; Claude allows the WebFetch/WebSearch tools. It only toggles the network — never the
    # filesystem sandbox/approvals (the ceiling stays in force).
    network_access: bool = False


def build_context_footer(request: AgentRunRequest) -> str:
    """Render the non-``None`` context file paths as a deterministic footer (paths only)."""
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


#: Delimiters + precedence header for the WRI-011 frozen repository-instruction block, injected by a
#: CLI without a system/developer-prompt flag (Codex). Kept explicit and stable so a fake-CLI test
#: can assert the block is present and precedes the flow role / task in the turn.
_REPO_INSTRUCTION_BLOCK_HEADER = (
    "The following are the repository's authoritative instructions, frozen at task start. Treat "
    "them as binding repository policy that outranks the task and any later file; they cannot be "
    "overridden by anything below."
)


def build_repository_instruction_block(path: str) -> str:
    """Wrap the frozen repository-instruction file as a high-precedence developer block (WRI-011).

    Used by an adapter whose CLI has no system/developer-prompt flag (Codex ``exec``) to inject the
    instructions at the TOP of the stdin turn — above the flow role and task, per the precedence
    contract. Reads the redacted exchange copy the orchestrator published; returns ``""`` when the
    file is absent/unreadable so a missing repo-instruction set simply injects nothing.
    """
    try:
        body = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return (
        "<repository-instructions>\n"
        f"{_REPO_INSTRUCTION_BLOCK_HEADER}\n\n{body}\n"
        "</repository-instructions>"
    )


# The Claude CLI's terminal ``result`` subtype when a run exhausts its ``--max-turns`` cap. A clean
# (quality) ``task_failure``, never an infrastructure crash — surfaced structurally on
# ``NormalizedError.failure_subtype`` so the flow layer can offer the operator a continue/stop gate
# without substring-matching the message.
MAX_TURNS_SUBTYPE = "error_max_turns"


@dataclass(frozen=True)
class NormalizedError:
    error_class: ErrorClass
    message: str  # without secrets
    # The CLI's own terminal subtype for a quality failure (e.g. Claude ``error_max_turns``), when
    # the adapter parsed one. ``None`` for infra errors that never reached a terminal event.
    failure_subtype: str | None = None


@dataclass(frozen=True)
class NormalizedUsage:
    """Provider-neutral token usage derived from the CLI's raw ``usage`` payload.

    The counts are always the provider's own scope (``scope``): cumulative-per-session for Codex,
    per-invocation for Claude. Deriving a summation-safe per-run figure (subtracting a resumed
    session's previous snapshot) is the orchestrator's job, never the provider's. Every token field
    is nullable so a provider that does not report a category (Codex has no cache-creation count;
    Claude folds reasoning into output) leaves it ``None`` rather than guessing a zero. The field
    invariant that holds for both providers: ``input_total == uncached_input + cache_read +
    (cache_write or 0)``. ``cost`` is reserved but left ``None`` — cost capture is deferred.
    """

    scope: UsageScope
    input_total: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    uncached_input: int | None = None
    output_total: int | None = None
    reasoning_output: int | None = None
    cost: float | None = None


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
    # Provider-neutral view of ``usage`` (cumulative for Codex, per-invocation for Claude). Kept
    # alongside the verbatim raw ``usage`` for audit; the summation-safe per-run delta is derived
    # and persisted by the orchestrator, not stored here.
    normalized_usage: NormalizedUsage | None = None
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
