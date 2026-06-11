"""Agent Router: route resolution and infrastructure-only fallback (spec §4.2, §5, §7).

The layer between the (future) Orchestrator Core and the provider adapters. For each stage it:

* resolves the ``(primary, fallback)`` pair from ``agents.routing`` plus a validated task override
  and records the route source (§4.2, §5);
* runs the primary and, **only** for infrastructure ``ProviderError`` classes (plus the conditional
  auth/permission case), falls back to the secondary provider (§7.2, §7.3);
* counts ``stage_attempts`` across the fallback, bounded by ``agents.max_stage_attempts`` (§8.1);
* exposes the §7.4 partial-change diff to the fallback without ever rolling back.

Invariants (docs/rules/architecture.md): the Router depends **only** on the ``AgentProvider``
contract — no CLI syntax, no provider internals — and it changes no state-machine state. It is
stateless beyond the :class:`StageOutcome` it returns; persistence and transitions are the Core's
job (P5). A quality ``AgentRunResult(status=failed)`` is never a fallback trigger; only a raised
infrastructure ``ProviderError`` is.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from wastech_orchestrator.config.loader import ConfigError
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.config.validation import check_task_route_override
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
    Stage,
)
from wastech_orchestrator.routing.snapshots import PartialChange, SnapshotHook
from wastech_orchestrator.security.profiles import is_same_or_stricter

# authorization_failed / permission_denied fall back only when the fallback provider runs in the
# same or a stricter permission profile (§7.2) — decided here, not in providers.base.
CONDITIONAL_FALLBACK: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.AUTHORIZATION_FAILED,
        ErrorClass.PERMISSION_DENIED,
    }
)


def fallback_allowed(
    error_class: ErrorClass, *, primary_profile: str, fallback_profile: str
) -> bool:
    """Decide whether a raised ``ProviderError`` permits fallback (spec §7.2/§7.3).

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


class RouteSource(StrEnum):
    """Where a resolved route came from — recorded for the audit (§4.2, persisted in P5)."""

    CONFIG = "config"
    TASK_OVERRIDE = "task_override"


@dataclass(frozen=True)
class ResolvedRoute:
    """The chosen primary/fallback for a stage, with its route source (§4.2)."""

    stage: Stage
    primary: ProviderId
    fallback: ProviderId | None
    source: RouteSource


@dataclass(frozen=True)
class ProviderAttempt:
    """One provider invocation within a stage (a §9 provider_attempts audit row)."""

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
    ) -> None:
        self._config = config
        self._providers = providers

    def resolve_route(
        self,
        stage: Stage,
        override: Mapping[Stage, ProviderId] | None = None,
    ) -> ResolvedRoute:
        """Resolve ``(primary, fallback)`` for ``stage`` from config + a validated task override.

        The override (front-matter ``agents``) may only repoint the **primary** to an allowed,
        configured provider (re-validated defensively via the P1 helper). The fallback follows by
        swap-on-collision: if the new primary equals the configured fallback they swap, so the other
        configured provider becomes the fallback; a configured ``None`` fallback stays ``None``.
        Raises :class:`ConfigError` on an invalid override or an unavailable provider.
        """
        routing = self._config.agents.routing
        if stage not in routing:
            raise ConfigError([f"agents.routing: no route configured for stage {stage.value!r}"])
        base = routing[stage]

        override = override or {}
        if stage in override:
            issues = check_task_route_override({stage: override[stage]}, self._config)
            if issues:
                raise ConfigError(issues)
            primary = override[stage]
            source = RouteSource.TASK_OVERRIDE
        else:
            primary = base.primary
            source = RouteSource.CONFIG

        fallback: ProviderId | None
        if base.fallback is not None and primary == base.fallback:
            fallback = base.primary
        else:
            fallback = base.fallback

        self._assert_available(stage, primary, fallback)
        return ResolvedRoute(stage=stage, primary=primary, fallback=fallback, source=source)

    def run_stage(
        self,
        request: AgentRunRequest,
        route: ResolvedRoute,
        *,
        snapshot: SnapshotHook | None = None,
    ) -> StageOutcome:
        """Run ``route.primary`` and, for infra failures only, fall back to ``route.fallback``.

        Counts ``stage_attempts`` across the fallback, bounded by ``agents.max_stage_attempts``. On
        an infra failure that changed files, the fallback receives the current diff (§7.4) — files
        are never rolled back. A quality ``status=failed`` is returned as-is, never retried (§7.3).
        """
        max_attempts = self._config.agents.max_stage_attempts
        before = snapshot.capture() if snapshot is not None else None

        sequence: list[ProviderId] = [route.primary]
        if route.fallback is not None:
            sequence.append(route.fallback)

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
            try:
                result = self._providers[pid].run(req)
            except ProviderError as exc:
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
                has_next = index + 1 < len(sequence)
                if not has_next or stage_attempts >= max_attempts:
                    continue  # exhausted — the loop will exit and surface last_error
                if not fallback_allowed(
                    exc.error_class,
                    primary_profile=self._profile_of(pid),
                    fallback_profile=self._profile_of(sequence[index + 1]),
                ):
                    break  # non-fallback infra error → terminal for this stage
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
            # A returned result — success or a quality failure — is never a fallback trigger (§7.3).
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
        relaxed (``permission_profile`` is intentionally left untouched), per §7.2/§7.4."""
        if partial is not None:
            return replace(request, attempt=attempt, diff_path=partial.diff_path)
        return replace(request, attempt=attempt)

    def _profile_of(self, pid: ProviderId) -> str:
        return self._config.agents.providers[pid].permission_profile

    def _assert_available(
        self, stage: Stage, primary: ProviderId, fallback: ProviderId | None
    ) -> None:
        """Defensively re-check the allowlist/config/instances for the resolved providers (§4.2)."""
        allowed = frozenset(self._config.agents.allowed)
        configured = self._config.agents.providers
        issues: list[str] = []
        for role, pid in (("primary", primary), ("fallback", fallback)):
            if pid is None:
                continue
            where = f"agents.routing.{stage.value}.{role}"
            if pid not in allowed:
                issues.append(f"{where}: provider {pid.value!r} is not in agents.allowed")
            if pid not in configured:
                issues.append(f"{where}: provider {pid.value!r} has no agents.providers entry")
            if pid not in self._providers:
                issues.append(f"{where}: no provider instance for {pid.value!r}")
        if issues:
            raise ConfigError(issues)
