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

Invariants (.agents/rules/architecture.md): the Router depends **only** on the ``AgentProvider``
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
from enum import StrEnum

from wastech_orchestrator.config.loader import ConfigError
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.providers.base import (
    FALLBACK_ELIGIBLE,
    AgentProvider,
    AgentRunRequest,
    AgentRunResult,
    ErrorClass,
    NormalizedError,
    ProviderError,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.routing.snapshots import PartialChange, SnapshotHook
from wastech_orchestrator.security.profiles import is_same_or_stricter

_LOG = logging.getLogger(__name__)

# authorization_failed / permission_denied fall back only when the fallback provider runs in the
# same or a stricter permission profile — decided here, not in providers.base.
CONDITIONAL_FALLBACK: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.AUTHORIZATION_FAILED,
        ErrorClass.PERMISSION_DENIED,
    }
)


def fallback_allowed(
    error_class: ErrorClass, *, primary_profile: str, fallback_profile: str
) -> bool:
    """Decide whether a raised ``ProviderError`` permits fallback.

    Unconditional for the infrastructure classes in
    :data:`~wastech_orchestrator.providers.base.FALLBACK_ELIGIBLE`; conditional for
    ``authorization_failed`` / ``permission_denied`` (only when the fallback profile is the same or
    stricter — never relaxing the policy); never for quality (``task_failure``) or configuration
    errors. Pure and directly unit-tested as a decision table.
    """
    if error_class in FALLBACK_ELIGIBLE:
        return True
    if error_class in CONDITIONAL_FALLBACK:
        return is_same_or_stricter(fallback_profile, primary_profile)
    return False


def _resolve_global_primary(config: OrchestratorConfig) -> ProviderId:
    """The single ``agents.providers.<id>.primary: true`` provider (PRE.1).

    ``validate_config`` already guarantees exactly one; this is defensive so a router built from an
    unvalidated config fails loud (``ConfigError``) rather than silently picking a provider.
    """
    primaries = [pid for pid, provider in config.agents.providers.items() if provider.primary]
    if len(primaries) != 1:
        raise ConfigError(
            [
                "agents.providers: exactly one provider must set primary: true "
                f"(found {len(primaries)}: {sorted(p.value for p in primaries)})"
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
    ) -> None:
        self._config = config
        self._providers = providers
        self._monotonic = monotonic
        self._global_primary = _resolve_global_primary(config)

    def resolve_route(
        self,
        node_id: str,
        provider: ProviderId | None = None,
    ) -> ResolvedRoute:
        """Resolve ``(primary, fallback)`` for a node from its declared ``provider`` (PRE.1).

        A non-``None`` ``provider`` (the flow node's declared executor) runs the node; ``None``
        defaults to the config's global primary. The fallback is always that global primary — the
        single infrastructure-fallback target — unless the resolved primary already *is* the global
        primary, in which case there is no fallback (a primary infra failure is terminal).
        ``node_id`` is carried for audit/logging only; it no longer selects the provider. Raises
        :class:`ConfigError` on an unknown or unavailable provider.
        """
        primary = provider if provider is not None else self._global_primary
        source = RouteSource.FLOW_NODE if provider is not None else RouteSource.CONFIG
        fallback = self._global_primary if primary != self._global_primary else None
        self._assert_available(node_id, primary, fallback)
        return ResolvedRoute(node_id=node_id, primary=primary, fallback=fallback, source=source)

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
        last_error: NormalizedError | None = None
        partial: PartialChange | None = None

        for index, pid in enumerate(sequence):
            if stage_attempts >= max_attempts:
                break
            attempt_no = stage_attempts + 1
            req = self._build_request(request, attempt_no, partial if index > 0 else None)
            stage_attempts += 1
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
                last_error = NormalizedError(error_class=exc.error_class, message=str(exc))
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
                # Resume safety net (durable sessions, P2.2): the requested session is gone
                # (``session_unavailable``) → retry the SAME provider once with a fresh session.
                # This is infrastructure, not a quality failure: it never falls back to another
                # provider and never charges a fix iteration (the fix loop is engine-owned; this
                # stays inside one node run). Differs from a quality ``status=failed`` → fixing.
                if (
                    exc.error_class is ErrorClass.SESSION_UNAVAILABLE
                    and req.session_id is not None
                    and stage_attempts < max_attempts
                ):
                    fresh_no = stage_attempts + 1
                    fresh_req = replace(req, session_id=None, attempt=fresh_no)
                    stage_attempts += 1
                    log.info(
                        "session unavailable; retrying fresh (no resume)",
                        extra={"provider": pid.value, "attempt": fresh_no},
                    )
                    try:
                        result = self._providers[pid].run(fresh_req)
                    except ProviderError as fresh_exc:
                        last_error = NormalizedError(
                            error_class=fresh_exc.error_class, message=str(fresh_exc)
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
                has_next = index + 1 < len(sequence)
                if not has_next or stage_attempts >= max_attempts:
                    continue  # exhausted — the loop will exit and surface last_error
                next_pid = sequence[index + 1]
                if not fallback_allowed(
                    exc.error_class,
                    primary_profile=self._profile_of(pid),
                    fallback_profile=self._profile_of(next_pid),
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

        return StageOutcome(
            route=route,
            result=None,
            provider_used=None,
            stage_attempts=stage_attempts,
            attempts=tuple(attempts),
            terminal_error=last_error,
            partial_change=partial,
        )

    def _build_request(
        self, request: AgentRunRequest, attempt: int, partial: PartialChange | None
    ) -> AgentRunRequest:
        """Per-attempt request. The fallback gets the partial diff; the permission profile is never
        relaxed (``permission_profile`` is intentionally left untouched), ."""
        if partial is not None:
            return replace(request, attempt=attempt, diff_path=partial.diff_path)
        return replace(request, attempt=attempt)

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
