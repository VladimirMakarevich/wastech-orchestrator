"""Route resolution: defaults, validated task overrides, defensive rejections (§4.2, §5)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from wastech_orchestrator.config.loader import ConfigError
from wastech_orchestrator.config.schema import OrchestratorConfig, RouteConfig
from wastech_orchestrator.providers.base import ProviderId, Stage
from wastech_orchestrator.routing.router import AgentRouter, RouteSource

# The canonical §5 default routing table.
DEFAULT_ROUTES: dict[Stage, tuple[ProviderId, ProviderId]] = {
    Stage.REFINEMENT: (ProviderId.CLAUDE, ProviderId.CODEX),
    Stage.PLANNING: (ProviderId.CLAUDE, ProviderId.CODEX),
    Stage.IMPLEMENTATION: (ProviderId.CLAUDE, ProviderId.CODEX),
    Stage.REVIEW: (ProviderId.CODEX, ProviderId.CLAUDE),
    Stage.FIXING: (ProviderId.CLAUDE, ProviderId.CODEX),
    Stage.SUMMARY: (ProviderId.CLAUDE, ProviderId.CODEX),
}


@pytest.fixture
def router(config: OrchestratorConfig, make_fake_provider: Callable[..., object]) -> AgentRouter:
    providers = {pid: make_fake_provider(pid) for pid in (ProviderId.CODEX, ProviderId.CLAUDE)}
    return AgentRouter(config, providers)


@pytest.mark.parametrize("stage", list(DEFAULT_ROUTES), ids=lambda s: s.value)
def test_default_route_per_stage(router: AgentRouter, stage: Stage) -> None:
    route = router.resolve_route(stage)
    assert (route.primary, route.fallback) == DEFAULT_ROUTES[stage]
    assert route.source is RouteSource.CONFIG
    assert route.stage is stage


def test_override_swaps_fallback_on_collision(router: AgentRouter) -> None:
    # implementation default = (claude, codex); override → codex collides with the fallback → swap.
    route = router.resolve_route(Stage.IMPLEMENTATION, {Stage.IMPLEMENTATION: ProviderId.CODEX})
    assert (route.primary, route.fallback) == (ProviderId.CODEX, ProviderId.CLAUDE)
    assert route.source is RouteSource.TASK_OVERRIDE


def test_override_review_swaps(router: AgentRouter) -> None:
    # review default = (codex, claude); override → claude collides → swap → (claude, codex).
    route = router.resolve_route(Stage.REVIEW, {Stage.REVIEW: ProviderId.CLAUDE})
    assert (route.primary, route.fallback) == (ProviderId.CLAUDE, ProviderId.CODEX)
    assert route.source is RouteSource.TASK_OVERRIDE


def test_noop_override_keeps_pair_but_records_source(router: AgentRouter) -> None:
    # planning default = (claude, codex); override → claude == primary → no swap, but source flips.
    route = router.resolve_route(Stage.PLANNING, {Stage.PLANNING: ProviderId.CLAUDE})
    assert (route.primary, route.fallback) == (ProviderId.CLAUDE, ProviderId.CODEX)
    assert route.source is RouteSource.TASK_OVERRIDE


def test_override_for_a_different_stage_is_ignored(router: AgentRouter) -> None:
    route = router.resolve_route(Stage.PLANNING, {Stage.REVIEW: ProviderId.CLAUDE})
    assert route.source is RouteSource.CONFIG


def test_non_routable_stage_is_rejected(router: AgentRouter) -> None:
    # testing/publishing are not agent-routed (Check Runner / Git Manager) — no route to resolve.
    with pytest.raises(ConfigError):
        router.resolve_route(Stage.TESTING)


def test_override_with_non_allowlisted_provider_is_rejected(
    config: OrchestratorConfig, make_fake_provider: Callable[..., object]
) -> None:
    # Shrink the allowlist to claude-only and repoint planning to a claude-only route, then a task
    # tries to force codex — the P1 override validator must reject it (§5).
    agents = replace(
        config.agents,
        allowed=(ProviderId.CLAUDE,),
        routing={**config.agents.routing, Stage.PLANNING: RouteConfig(ProviderId.CLAUDE, None)},
    )
    cfg = replace(config, agents=agents)
    providers = {pid: make_fake_provider(pid) for pid in (ProviderId.CODEX, ProviderId.CLAUDE)}
    router = AgentRouter(cfg, providers)
    with pytest.raises(ConfigError):
        router.resolve_route(Stage.PLANNING, {Stage.PLANNING: ProviderId.CODEX})


def test_missing_provider_instance_is_rejected(
    config: OrchestratorConfig, make_fake_provider: Callable[..., object]
) -> None:
    # review needs a claude fallback instance; constructing without one must fail closed (§4.2).
    router = AgentRouter(config, {ProviderId.CODEX: make_fake_provider(ProviderId.CODEX)})
    with pytest.raises(ConfigError):
        router.resolve_route(Stage.REVIEW)


def test_override_cannot_change_provider_command_or_args(
    router: AgentRouter, config: OrchestratorConfig
) -> None:
    # A route override carries only a ProviderId; command/extra_args/security still come from
    # config.providers, so a task can never change a provider's command or security settings (§5).
    route = router.resolve_route(Stage.REVIEW, {Stage.REVIEW: ProviderId.CLAUDE})
    assert config.agents.providers[route.primary].command == "claude"
    assert config.agents.providers[route.primary].extra_args == ()
