"""Configuration validator (spec §11, §21.4) — the fail-closed gate.

Enforces every §11 / §21.4 semantic rule so an unsafe or contradictory config never reaches the
pipeline. This is the config-time half of the "security cannot be weakened" invariant
(docs/rules/security.md): ``extra_args`` that would disable the sandbox/approvals are rejected here.
The adversarial test matrix lives in P6.

All problems are collected and raised together via the typed :class:`ConfigError` from the loader.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from wastech_orchestrator.config.loader import ConfigError
from wastech_orchestrator.config.schema import (
    ROUTABLE_STAGES,
    FootprintLocation,
    FootprintTracking,
    OrchestratorConfig,
    RouteConfig,
)
from wastech_orchestrator.providers.base import ProviderId, Stage
from wastech_orchestrator.security.forbidden_args import find_forbidden_args


def _check_extra_args(pid: ProviderId, args: tuple[str, ...], issues: list[str]) -> None:
    """Reject any extra_args flag that disables the sandbox/permissions (spec §11).

    Delegates the detection to the shared
    :func:`~wastech_orchestrator.security.forbidden_args.find_forbidden_args` (also used at run time
    by the provider command builders) and frames each finding as a config issue.
    """
    where = f"agents.providers.{pid.value}.extra_args"
    for reason in find_forbidden_args(args):
        issues.append(f"{where}: {reason}")


def _check_route(
    stage: Stage,
    route: RouteConfig,
    allowed: frozenset[ProviderId],
    providers: frozenset[ProviderId],
    issues: list[str],
) -> None:
    where = f"agents.routing.{stage.value}"
    for role, provider in (("primary", route.primary), ("fallback", route.fallback)):
        if provider is None:
            continue
        if provider not in allowed:
            issues.append(f"{where}.{role}: provider {provider.value!r} is not in agents.allowed")
        if provider not in providers:
            issues.append(
                f"{where}.{role}: provider {provider.value!r} has no agents.providers entry"
            )


def validate_config(config: OrchestratorConfig) -> list[str]:
    """Validate a parsed config against §11/§21.4. Raises :class:`ConfigError` on any violation.

    Returns a list of non-fatal warnings (empty in v1 — every validator finding is a hard error).
    """
    issues: list[str] = []
    warnings: list[str] = []
    agents = config.agents
    allowed = frozenset(agents.allowed)
    provider_ids = frozenset(agents.providers)

    # Routes: only agent-routed stages, with primary/fallback known and configured.
    for stage, route in agents.routing.items():
        if stage not in ROUTABLE_STAGES:
            issues.append(
                f"agents.routing: {stage.value!r} is not an agent-routed stage "
                f"(allowed: {sorted(s.value for s in ROUTABLE_STAGES)})"
            )
            continue
        _check_route(stage, route, allowed, provider_ids, issues)

    # Watch poll interval (§8.3): negative is meaningless; 0 means single-pass (no loop).
    if config.orchestrator.poll_interval_seconds < 0:
        issues.append(
            "orchestrator.poll_interval_seconds must be >= 0 "
            f"(got {config.orchestrator.poll_interval_seconds})"
        )

    # Loop-control hard cap (§8.1): the global cap must be >= a single fix loop.
    if agents.max_total_fix_iterations < agents.max_fix_cycles:
        issues.append(
            "agents.max_total_fix_iterations must be >= agents.max_fix_cycles "
            f"({agents.max_total_fix_iterations} < {agents.max_fix_cycles})"
        )

    # Decomposition: a split must produce at least 2 subtasks.
    if agents.decomposition.max_subtasks < 2:
        issues.append(
            "agents.decomposition.max_subtasks must be >= 2 "
            f"(got {agents.decomposition.max_subtasks})"
        )

    # Security: extra_args must not weaken the sandbox/permissions.
    for pid, provider in agents.providers.items():
        _check_extra_args(pid, provider.extra_args, issues)

    _validate_footprint(config, issues)

    if issues:
        raise ConfigError(issues)
    return warnings


def _validate_footprint(config: OrchestratorConfig, issues: list[str]) -> None:
    footprint = config.git.footprint
    location = footprint.location
    tracking = footprint.tracking

    # Illegal pairings (§21.4).
    if location is FootprintLocation.EXTERNAL and tracking in (
        FootprintTracking.EXCLUDE_LOCAL,
        FootprintTracking.COMMIT,
    ):
        issues.append(
            f"git.footprint: location 'external' is incompatible with tracking {tracking.value!r}"
        )
    if location is FootprintLocation.IN_REPO and tracking is FootprintTracking.NONE:
        issues.append("git.footprint: location 'in_repo' requires tracking other than 'none'")

    # Anti-traversal: external artifacts must live outside the clone (§21.4).
    if location is FootprintLocation.EXTERNAL:
        external_root = Path(footprint.external_root).resolve()
        local_path = Path(config.repo.local_path).resolve()
        if external_root == local_path or external_root.is_relative_to(local_path):
            issues.append(
                "git.footprint.external_root must resolve outside repo.local_path "
                f"({footprint.external_root!r} is inside {config.repo.local_path!r})"
            )


def check_task_route_override(
    override: Mapping[Stage, ProviderId], config: OrchestratorConfig
) -> list[str]:
    """Validate a per-task route override (front-matter ``agents``) against the config.

    A task may only pick an allowed, configured provider for an agent-routed stage — it can never
    change a provider's command, ``extra_args``, or any security setting (that is structurally
    impossible from the task model, and enforced here). Pure: returns problems, raises nothing.
    Used by the Router in P4.
    """
    issues: list[str] = []
    allowed = frozenset(config.agents.allowed)
    provider_ids = frozenset(config.agents.providers)
    for stage, provider in override.items():
        where = f"task.agents.{stage.value}"
        if stage not in ROUTABLE_STAGES:
            issues.append(f"{where}: {stage.value!r} is not an agent-routed stage")
            continue
        if provider not in allowed:
            issues.append(f"{where}: provider {provider.value!r} is not in agents.allowed")
        if provider not in provider_ids:
            issues.append(f"{where}: provider {provider.value!r} has no agents.providers entry")
    return issues
