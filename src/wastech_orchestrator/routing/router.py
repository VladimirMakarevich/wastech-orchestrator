"""Agent Router: route resolution and infrastructure-only fallback (PRE.1).

The layer between the Orchestrator Core and the provider adapters. For each node it:

* resolves the ``(primary, fallback)`` pair from the flow node's ``provider`` field — the node's
  declared provider runs it, else the config's single global primary
  (``agents.providers.<id>.primary``); the fallback is that global primary (the sole infra-fallback
  target) unless the primary already *is* it (PRE.1);
* runs the primary and, **only** for infrastructure ``ProviderError`` classes (plus the conditional
  auth/permission case), falls back to the global primary;
* counts ``stage_attempts`` across the fallback, bounded by ``agents.max_stage_attempts``;
* exposes the partial-change diff to the fallback without ever rolling back.

Invariants: the Router depends **only** on the ``AgentProvider``
contract — no CLI syntax, no provider internals — and it changes no state-machine state. It is
stateless beyond the :class:`StageOutcome` it returns; persistence and transitions are the Core's
job. A quality ``AgentRunResult(status=failed)`` is never a fallback trigger; only a raised
infrastructure ``ProviderError`` is.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from wastech_orchestrator.config.loader import ConfigError
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.providers.base import (
    FALLBACK_ELIGIBLE,
    TRANSIENT_RETRYABLE,
    AgentProvider,
    AgentRunRequest,
    AgentRunResult,
    ErrorClass,
    NormalizedError,
    ProviderError,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.providers.capabilities import map_reasoning_for_provider_switch
from wastech_orchestrator.routing.snapshots import PartialChange, SnapshotHook
from wastech_orchestrator.security.isolation import IsolationCheck
from wastech_orchestrator.security.profiles import is_same_or_stricter
from wastech_orchestrator.security.shell_reach import (
    ShellCheck,
    ShellQuery,
    any_provider_grants_shell,
)

_LOG = logging.getLogger(__name__)

# Error classes whose fallback is CONDITIONAL, decided here (not in providers.base):
# * authorization_failed / permission_denied — only when the fallback provider runs in the same or a
#   stricter permission profile (never relaxing the policy);
# * capability_unavailable — only when the fallback is same-or-stricter AND is itself configured to
#   isolate (``fallback_can_isolate``), so a capability refusal is never recovered by falling over
#   to a provider whose own config forbids isolating. Whether the *host* can enforce a floor is
#   deliberately not part of this: that answer is advisory everywhere else, and the fallback
#   provider decides it per attempt, with the node's declaration in hand, before any CLI launches.
CONDITIONAL_FALLBACK: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.AUTHORIZATION_FAILED,
        ErrorClass.PERMISSION_DENIED,
        ErrorClass.CAPABILITY_UNAVAILABLE,
    }
)


def fallback_allowed(
    error_class: ErrorClass,
    *,
    primary_profile: str,
    fallback_profile: str,
    fallback_can_isolate: bool = True,
) -> bool:
    """Decide whether a raised ``ProviderError`` permits fallback.

    Unconditional for the infrastructure classes in
    :data:`~wastech_orchestrator.providers.base.FALLBACK_ELIGIBLE`; conditional for
    ``authorization_failed`` / ``permission_denied`` (only when the fallback profile is the same or
    stricter — never relaxing the policy) and for ``capability_unavailable`` (same-or-stricter AND
    ``fallback_can_isolate`` — the fallback's own configuration must permit isolating);
    never for quality (``task_failure``) or configuration errors. Pure and directly unit-tested as a
    decision table.
    """
    if error_class in FALLBACK_ELIGIBLE:
        return True
    if error_class in CONDITIONAL_FALLBACK:
        same_or_stricter = is_same_or_stricter(fallback_profile, primary_profile)
        if error_class is ErrorClass.CAPABILITY_UNAVAILABLE:
            return same_or_stricter and fallback_can_isolate
        return same_or_stricter
    return False


def _earlier(current: str | None, candidate: str | None) -> str | None:
    """The earlier of two ISO-8601 reset instants, tolerating absent or unparseable input.

    Provider input, so an instant that cannot be compared is dropped rather than allowed to hide a
    usable one: the result is always either a parseable instant or ``None``.
    """
    if candidate is None:
        return current
    if current is None:
        return candidate
    try:
        return min(current, candidate, key=datetime.fromisoformat)
    except ValueError:
        return current


def _resolve_global_primary(config: OrchestratorConfig) -> ProviderId:
    """The single ``agents.providers.<id>.primary: true`` provider (PRE.1).

    ``validate_config`` already guarantees exactly one; this is defensive so a router built from an
    unvalidated config fails loud (``ConfigError``) rather than silently picking a provider.
    """
    primaries = [pid for pid, provider in config.agents.providers.items() if provider.primary]
    if len(primaries) != 1:
        raise ConfigError(
            [
                (
                    "agents.providers: exactly one provider must set primary: true "
                    f"(found {len(primaries)}: {sorted(p.value for p in primaries)})"
                )
            ]
        )
    return primaries[0]


class RouteSource(StrEnum):
    """Where a node's provider came from — recorded for the audit (persisted in node_runs)."""

    CONFIG = "config"  # defaulted to the global primary (the node declared no provider)
    FLOW_NODE = "flow_node"  # the flow node declared an explicit provider


@dataclass(frozen=True)
class ResolvedRoute:
    """The chosen primary/fallback for a node, with its route source.

    ``node_id`` is the flow node's id, carried for audit/logging only — it never selects the
    provider (routing is node-based via the node's ``provider`` field, PRE.1).
    """

    node_id: str
    primary: ProviderId
    fallback: ProviderId | None
    source: RouteSource


@dataclass(frozen=True)
class ProviderAttempt:
    """One provider invocation within a stage (a provider_attempts audit row)."""

    provider: ProviderId
    attempt: int
    status: RunStatus | None  # None when the run raised a ProviderError
    error_class: ErrorClass | None
    result: AgentRunResult | None


@dataclass(frozen=True)
class StageOutcome:
    """Everything the Core needs to act on a stage run. The Router decides nothing downstream."""

    route: ResolvedRoute
    # The final SUCCEEDED or quality-FAILED result; None when every attempt raised an infra error.
    result: AgentRunResult | None
    provider_used: ProviderId | None
    stage_attempts: int  # total attempts incl. fallback, bounded by max_stage_attempts
    # Set iff ``result`` is None (infra-exhausted or a non-fallback infra error).
    terminal_error: NormalizedError | None
    attempts: tuple[ProviderAttempt, ...]
    partial_change: PartialChange | None = None


class AgentRouter:
    """Resolves routes and runs a stage with infra-only fallback. Holds the provider instances."""

    def __init__(
        self,
        config: OrchestratorConfig,
        providers: Mapping[ProviderId, AgentProvider],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        is_cancelled: Callable[[], bool] = lambda: False,
        isolation_checks: Mapping[ProviderId, IsolationCheck] | None = None,
        shell_checks: Mapping[ProviderId, ShellCheck] | None = None,
    ) -> None:
        self._config = config
        self._providers = providers
        self._monotonic = monotonic
        self._sleep = sleep
        # Set only by the watch daemon: True once an operator stop was requested. Checked before any
        # fallback/retry so a stop-killed agent is never respawned on another provider.
        self._is_cancelled = is_cancelled
        # The offline ProviderId→isolation-check table (the same one composition binds), so a
        # ``CAPABILITY_UNAVAILABLE`` fallback is allowed only to a provider whose own configuration
        # is legal. The router imports no concrete adapter — the table is injected.
        self._isolation_checks = isolation_checks or {}
        # The offline ProviderId→"does this attempt get a shell?" table (also injected, same
        # reason). Empty means every answer is fail-closed ``True``: a caller that cannot classify
        # an attempt brackets it rather than leaving it unwatched.
        self._shell_checks = shell_checks or {}
        self._global_primary = _resolve_global_primary(config)

    def _can_isolate(self, pid: ProviderId) -> bool:
        """Whether ``pid``'s config permits isolating (offline, host-independent, fail-closed).

        Reuses the injected offline isolation check (``isolation_reasons``): an empty reason list
        means nothing in that provider's config stands in the way. A missing check/config is treated
        as *cannot* isolate (fail-closed), so a ``CAPABILITY_UNAVAILABLE`` fallback never silently
        reaches an unverifiable provider. It asks nothing about the host: a host that cannot enforce
        a floor is announced, not refused, and turning that into a denied fallback would resurrect
        the refusal here — costing a legitimate hop (a read-only node needs no sandbox at all) while
        the target provider already refuses the genuinely unsafe attempt itself.
        """
        check = self._isolation_checks.get(pid)
        cfg = self._config.agents.providers.get(pid)
        if check is None or cfg is None:
            return False
        return not check(cfg)

    def route_grants_shell(
        self, route: ResolvedRoute, *, permission_profile: str | None, git_evidence: bool
    ) -> bool:
        """Whether this attempt gets a shell on any provider it may land on (offline; fail-closed).

        Both ends of the route are asked because the caller takes its detection bracket *before* the
        run, and an infra failure on the primary can move the attempt to the fallback — a different
        CLI with its own answer. Command execution, not the permission profile, is what makes a
        working-tree write or a ``.git`` mutation reachable, so this is the question a bracket that
        watches for one has to key on; the per-provider answers live in the adapters and reach here
        through the injected table (see
        :mod:`wastech_orchestrator.security.shell_reach`).
        """
        providers = [route.primary]
        if route.fallback is not None:
            providers.append(route.fallback)
        return any_provider_grants_shell(
            providers,
            self._config.agents.providers,
            self._shell_checks,
            ShellQuery(
                permission_profile=permission_profile,
                git_evidence=git_evidence,
                strict_isolation=self._config.security.strict_isolation,
            ),
        )

    def resolve_route(
        self,
        node_id: str,
        provider: ProviderId | None = None,
    ) -> ResolvedRoute:
        """Resolve ``(primary, fallback)`` for a node from its declared ``provider`` (PRE.1).

        A non-``None`` ``provider`` (the flow node's declared executor) runs the node; ``None``
        defaults to the config's global primary. When the resolved primary differs from the global
        primary, the fallback is that global primary. When the resolved primary already *is* the
        global primary, the fallback is the single *other* allowed+configured provider — symmetric
        cross-provider failover (transient provider recovery, extending PRE.1's single fallback
        target). With only one allowed provider there is no fallback (an infra failure on it is
        handled by the same-provider retry budget, then the soft pause). ``node_id`` is carried for
        audit/logging only; it no longer selects the provider. Raises :class:`ConfigError` on an
        unknown or unavailable provider.
        """
        primary = provider if provider is not None else self._global_primary
        source = RouteSource.FLOW_NODE if provider is not None else RouteSource.CONFIG
        fallback: ProviderId | None
        if primary != self._global_primary:
            fallback = self._global_primary
        else:
            fallback = self._other_allowed_provider(primary)
        self._assert_available(node_id, primary, fallback)
        return ResolvedRoute(node_id=node_id, primary=primary, fallback=fallback, source=source)

    def _other_allowed_provider(self, primary: ProviderId) -> ProviderId | None:
        """The single *other* allowed+configured provider, or ``None`` (symmetric fallback target).

        When the resolved primary is the global primary, the fallback is the one other provider in
        ``agents.allowed`` that also has a configured ``providers.<id>`` block. With a single
        allowed provider — or, defensively, >2 — there is no unambiguous target → ``None``.
        Supports exactly the Claude/Codex pair (the canonical provider universe)."""
        others = [
            pid
            for pid in self._config.agents.allowed
            if pid != primary and pid in self._config.agents.providers
        ]
        return others[0] if len(others) == 1 else None

    def run_stage(
        self,
        request: AgentRunRequest,
        route: ResolvedRoute,
        *,
        snapshot: SnapshotHook | None = None,
    ) -> StageOutcome:
        """Run ``route.primary`` and, for infra failures only, fall back to ``route.fallback``.

        Counts ``stage_attempts`` across the fallback, bounded by ``agents.max_stage_attempts``. On
        an infra failure that changed files, the fallback receives the current diff — files
        are never rolled back. A quality ``status=failed`` is returned as-is, never retried.
        """
        max_attempts = self._config.agents.max_stage_attempts
        before = snapshot.capture() if snapshot is not None else None

        sequence: list[ProviderId] = [route.primary]
        if route.fallback is not None:
            sequence.append(route.fallback)

        log = bind(_LOG, task_id=request.task_id, node_id=route.node_id)
        log.info(
            "route resolved",
            extra={
                "primary": route.primary.value,
                "fallback": route.fallback.value if route.fallback else None,
                "source": route.source.value,
            },
        )

        attempts: list[ProviderAttempt] = []
        stage_attempts = 0
        # The audit attempt number increments on EVERY provider invocation (each hop, the
        # session-unavailable fresh retry, and each transient retry), so every provider_attempts row
        # gets a distinct, gap-free number. It is intentionally decoupled from ``stage_attempts``,
        # which counts only provider *hops* and bounds ``max_stage_attempts`` — transient retries
        # (Option A) must not consume that hop budget.
        audit_attempt = 0
        last_error: NormalizedError | None = None
        # The EARLIEST reset instant any attempt reported, tracked across the whole stage rather
        # than read off whichever attempt happened to settle. The settling attempt is normally
        # the fallback, and a fallback that failed for an unrelated reason reports no instant, so
        # taking the last one would discard the primary's known window in the case that matters
        # most. Earliest rather than latest because waking early costs one cheap re-park, while
        # waking late is the blind wait this exists to remove.
        earliest_reset: str | None = None
        partial: PartialChange | None = None

        for index, pid in enumerate(sequence):
            if stage_attempts >= max_attempts:
                break
            stage_attempts += 1
            audit_attempt += 1
            attempt_no = audit_attempt
            req = self._build_request(
                request,
                attempt_no,
                partial if index > 0 else None,
                from_provider=sequence[index - 1] if index > 0 else None,
                to_provider=pid,
            )
            attempt_started = self._monotonic()
            log.info(
                "provider attempt started",
                extra={
                    "provider": pid.value,
                    "attempt": attempt_no,
                    "timeout_seconds": req.timeout_seconds,
                },
            )
            try:
                result = self._providers[pid].run(req)
            except ProviderError as exc:
                duration = round(self._monotonic() - attempt_started, 3)
                earliest_reset = _earlier(earliest_reset, exc.resets_at)
                last_error = NormalizedError(
                    error_class=exc.error_class, message=str(exc), resets_at=exc.resets_at
                )
                attempts.append(
                    ProviderAttempt(
                        provider=pid,
                        attempt=attempt_no,
                        status=None,
                        error_class=exc.error_class,
                        result=None,
                    )
                )
                log.info(
                    "provider attempt failed",
                    extra={
                        "provider": pid.value,
                        "attempt": attempt_no,
                        "error_class": exc.error_class.value,
                        "duration_seconds": duration,
                    },
                )
                # Reliable stop: an operator stop killed this agent (SIGKILL → an abnormal exit
                # that classifies as PROCESS_CRASHED, which is fallback-eligible). A cancellation is
                # NOT a crash — refuse every retry/fallback here so no fresh agent is ever respawned
                # after a stop; the Core parks the task resumable. Checked before the fallback path.
                if self._is_cancelled():
                    last_error = NormalizedError(
                        error_class=ErrorClass.CANCELLED,
                        message="stop requested; agent cancelled, not falling back",
                        # Deliberately carries no reset instant: a stopped task resumes when the
                        # operator says so, not when some provider's window happens to reopen.
                    )
                    log.info("cancelled; not falling back", extra={"provider": pid.value})
                    break
                # Resume safety net (durable sessions): the requested session is gone
                # (``session_unavailable``) → retry the SAME provider once with a fresh session.
                # This is infrastructure, not a quality failure: it never falls back to another
                # provider and never charges a fix iteration (the fix loop is engine-owned; this
                # stays inside one node run). Differs from a quality ``status=failed`` → fixing.
                if (
                    exc.error_class is ErrorClass.SESSION_UNAVAILABLE
                    and req.session_id is not None
                    and stage_attempts < max_attempts
                ):
                    audit_attempt += 1
                    fresh_no = audit_attempt
                    fresh_req = replace(req, session_id=None, attempt=fresh_no)
                    stage_attempts += 1  # session retry still spends a hop (unchanged semantics)
                    log.info(
                        "session unavailable; retrying fresh (no resume)",
                        extra={"provider": pid.value, "attempt": fresh_no},
                    )
                    try:
                        result = self._providers[pid].run(fresh_req)
                    except ProviderError as fresh_exc:
                        earliest_reset = _earlier(earliest_reset, fresh_exc.resets_at)
                        last_error = NormalizedError(
                            error_class=fresh_exc.error_class,
                            message=str(fresh_exc),
                            resets_at=fresh_exc.resets_at,
                        )
                        attempts.append(
                            ProviderAttempt(
                                provider=pid,
                                attempt=fresh_no,
                                status=None,
                                error_class=fresh_exc.error_class,
                                result=None,
                            )
                        )
                        exc = fresh_exc  # the fallback decision below uses the fresh error
                    else:
                        attempts.append(
                            ProviderAttempt(
                                provider=pid,
                                attempt=fresh_no,
                                status=result.status,
                                error_class=result.error.error_class if result.error else None,
                                result=result,
                            )
                        )
                        return StageOutcome(
                            route=route,
                            result=result,
                            provider_used=pid,
                            stage_attempts=stage_attempts,
                            attempts=tuple(attempts),
                            terminal_error=None,
                            partial_change=partial,
                        )
                # Transient infra blip (Option A): retry the SAME provider with backoff before
                # falling back. A separate per-provider budget (``agents.retry.max_attempts``) that
                # does NOT consume ``stage_attempts`` — so it survives even when the hop budget is
                # spent — and a real sleep window (the CLI already exhausted its own retries).
                # Mutually exclusive with the session-unavailable branch (a non-transient class).
                elif (
                    exc.error_class in TRANSIENT_RETRYABLE
                    and self._config.agents.retry.max_attempts > 0
                ):
                    retried, last_class, audit_attempt = self._retry_transient(
                        pid,
                        req,
                        first_error=exc,
                        audit_attempt=audit_attempt,
                        attempts=attempts,
                        log=log,
                    )
                    if retried is not None:
                        return StageOutcome(
                            route=route,
                            result=retried,
                            provider_used=pid,
                            stage_attempts=stage_attempts,
                            attempts=tuple(attempts),
                            terminal_error=None,
                            partial_change=partial,
                        )
                    last_error = NormalizedError(
                        error_class=last_class,
                        message=f"transient retries exhausted on {pid.value}",
                        # Structurally unreachable today (a rate limit is not transient-retryable),
                        # carried anyway so no path here can silently drop a reported instant.
                        resets_at=exc.resets_at,
                    )
                    exc = ProviderError(last_class, str(last_error.message))  # fallback uses this
                has_next = index + 1 < len(sequence)
                if not has_next or stage_attempts >= max_attempts:
                    continue  # exhausted — the loop will exit and surface last_error
                next_pid = sequence[index + 1]
                if not fallback_allowed(
                    exc.error_class,
                    primary_profile=self._profile_of(pid),
                    fallback_profile=self._profile_of(next_pid),
                    fallback_can_isolate=self._can_isolate(next_pid),
                ):
                    log.info(
                        "fallback denied",
                        extra={
                            "from": pid.value,
                            "to": next_pid.value,
                            "error_class": exc.error_class.value,
                            "reason": "not infra-eligible or would relax the permission profile",
                        },
                    )
                    break  # non-fallback infra error → terminal for this stage
                log.info(
                    "falling back",
                    extra={
                        "from": pid.value,
                        "to": next_pid.value,
                        "error_class": exc.error_class.value,
                    },
                )
                if snapshot is not None and before is not None:
                    partial = snapshot.partial_change_since(before)
                continue

            attempts.append(
                ProviderAttempt(
                    provider=pid,
                    attempt=attempt_no,
                    status=result.status,
                    error_class=result.error.error_class if result.error else None,
                    result=result,
                )
            )
            log.info(
                "provider attempt completed",
                extra={
                    "provider": pid.value,
                    "attempt": attempt_no,
                    "status": result.status.value,
                    "exit_code": result.exit_code,
                    "duration_seconds": round(self._monotonic() - attempt_started, 3),
                },
            )
            # A returned result — success or a quality failure — is never a fallback trigger.
            return StageOutcome(
                route=route,
                result=result,
                provider_used=pid,
                stage_attempts=stage_attempts,
                attempts=tuple(attempts),
                terminal_error=None,
                partial_change=partial,
            )

        # A stop is the one park that must not inherit a provider window: it resumes when the
        # operator says so. Every other exhausted stage surfaces the earliest instant ANY attempt
        # reported, so a fallback failing for an unrelated reason cannot discard the primary's.
        if last_error is not None and last_error.error_class is not ErrorClass.CANCELLED:
            last_error = replace(last_error, resets_at=earliest_reset)
        return StageOutcome(
            route=route,
            result=None,
            provider_used=None,
            stage_attempts=stage_attempts,
            attempts=tuple(attempts),
            terminal_error=last_error,
            partial_change=partial,
        )

    def _retry_transient(
        self,
        pid: ProviderId,
        req: AgentRunRequest,
        *,
        first_error: ProviderError,
        audit_attempt: int,
        attempts: list[ProviderAttempt],
        log: logging.LoggerAdapter[logging.Logger],
    ) -> tuple[AgentRunResult | None, ErrorClass, int]:
        """Retry one provider up to ``agents.retry.max_attempts`` times for a transient infra blip.

        Resumes by reusing ``req`` (the durable session is preserved); if a retry itself raises
        ``SESSION_UNAVAILABLE`` the session is dropped for the remaining tries (resume → fresh
        degrade, same shape as the session-unavailable safety net). Sleeps a deterministic
        exponential backoff ``min(base * 2**k, max_delay_s)`` (no jitter) before each try and
        appends a ``provider_attempts`` audit row per try. Stops early if a retry is non-transient,
        non-session class (no point burning the window on a class that will not recover). Returns
        ``(result | None, last_error_class, audit_attempt)`` — the caller advances to fallback when
        the result is ``None``."""
        retry = self._config.agents.retry
        last_class = first_error.error_class
        resume_req = req  # reuse the built request → resume the session
        for k in range(retry.max_attempts):
            delay = min(retry.base_delay_s * (2**k), retry.max_delay_s)
            log.info(
                "transient retry backoff",
                extra={
                    "provider": pid.value,
                    "retry": k + 1,
                    "delay_seconds": delay,
                    "error_class": last_class.value,
                },
            )
            self._sleep(delay)
            audit_attempt += 1
            attempt_req = replace(resume_req, attempt=audit_attempt)
            started = self._monotonic()
            try:
                result = self._providers[pid].run(attempt_req)
            except ProviderError as exc:
                last_class = exc.error_class
                attempts.append(
                    ProviderAttempt(
                        provider=pid,
                        attempt=audit_attempt,
                        status=None,
                        error_class=exc.error_class,
                        result=None,
                    )
                )
                log.info(
                    "transient retry failed",
                    extra={
                        "provider": pid.value,
                        "attempt": audit_attempt,
                        "error_class": exc.error_class.value,
                        "duration_seconds": round(self._monotonic() - started, 3),
                    },
                )
                if exc.error_class is ErrorClass.SESSION_UNAVAILABLE:
                    if resume_req.session_id is not None:
                        resume_req = replace(resume_req, session_id=None)  # degrade for next tries
                elif exc.error_class not in TRANSIENT_RETRYABLE:
                    return None, last_class, audit_attempt  # non-transient → stop; fallback decides
                continue
            attempts.append(
                ProviderAttempt(
                    provider=pid,
                    attempt=audit_attempt,
                    status=result.status,
                    error_class=result.error.error_class if result.error else None,
                    result=result,
                )
            )
            log.info(
                "transient retry succeeded",
                extra={
                    "provider": pid.value,
                    "attempt": audit_attempt,
                    "status": result.status.value,
                    "duration_seconds": round(self._monotonic() - started, 3),
                },
            )
            return result, last_class, audit_attempt
        return None, last_class, audit_attempt

    def _build_request(
        self,
        request: AgentRunRequest,
        attempt: int,
        partial: PartialChange | None,
        *,
        from_provider: ProviderId | None = None,
        to_provider: ProviderId | None = None,
    ) -> AgentRunRequest:
        """Per-attempt request. The fallback gets the partial diff; the permission profile is never
        relaxed (``permission_profile`` is intentionally left untouched).

        A ``cross_provider`` fallback attempt (the substitute provider is a *different* CLI) drops
        provider-specific request fields: ``model``, most ``reasoning`` values, ``extra_args``,
        and ``session_id``. A model id is provider-specific (codex ``gpt-5.4`` is not a Claude
        model), provider CLI flags in ``extra_args`` are not portable, and a durable session id
        belongs to one provider. Cleared values make the substitute re-resolve its own
        config/defaults while preserving portable context (prompt, paths, schema, permissions,
        network flag). Codex ``minimal`` maps to Claude ``low`` because Claude has no ``minimal``
        level. The same-provider session-unavailable retry reuses the already-built ``req`` and is
        unaffected.
        """
        req = replace(request, attempt=attempt)
        if partial is not None:
            req = replace(req, diff_path=partial.diff_path)
        if from_provider is not None and to_provider is not None and from_provider != to_provider:
            req = replace(
                req,
                model=None,
                reasoning=map_reasoning_for_provider_switch(
                    from_provider, to_provider, request.reasoning
                ),
                extra_args=[],
                session_id=None,
            )
        return req

    def _profile_of(self, pid: ProviderId) -> str:
        return self._config.agents.providers[pid].permission_profile

    def _assert_available(
        self, node_id: str, primary: ProviderId, fallback: ProviderId | None
    ) -> None:
        """Defensively re-check the allowlist/config/instances for the resolved providers.

        The node's ``provider`` is validated against ``agents.allowed`` at preflight; this is the
        belt-and-braces check at run time (a node provider not in the allowlist, or with no
        configured block / instance, is a fatal config error, never a silent skip)."""
        allowed = frozenset(self._config.agents.allowed)
        configured = self._config.agents.providers
        issues: list[str] = []
        for role, pid in (("primary", primary), ("fallback", fallback)):
            if pid is None:
                continue
            where = f"flow node provider for node {node_id!r} ({role})"
            if pid not in allowed:
                issues.append(f"{where}: provider {pid.value!r} is not in agents.allowed")
            if pid not in configured:
                issues.append(f"{where}: provider {pid.value!r} has no agents.providers entry")
            if pid not in self._providers:
                issues.append(f"{where}: no provider instance for {pid.value!r}")
        if issues:
            raise ConfigError(issues)
