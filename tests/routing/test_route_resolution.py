"""Route resolution: node-based provider selection + global-primary fallback (PRE.1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from wastech_orchestrator.config.loader import ConfigError
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.routing.router import AgentRouter, RouteSource

# Every node routes the same way now — routing is by the node's ``provider``, not the node id.
_NODE_IDS = ["refinement", "planning", "implementation", "review", "fixing"]


@pytest.fixture
def router(config: OrchestratorConfig, make_fake_provider: Callable[..., object]) -> AgentRouter:
    providers = {pid: make_fake_provider(pid) for pid in (ProviderId.CODEX, ProviderId.CLAUDE)}
    return AgentRouter(config, providers)


@pytest.mark.parametrize("node_id", _NODE_IDS, ids=lambda s: s)
def test_no_provider_defaults_to_global_primary(router: AgentRouter, node_id: str) -> None:
    # The packaged config marks claude the global primary. A node with no provider → claude, and
    # since the primary already *is* the global primary there is no fallback target.
    route = router.resolve_route(node_id)
    assert (route.primary, route.fallback) == (ProviderId.CLAUDE, None)
    assert route.source is RouteSource.CONFIG
    assert route.node_id == node_id


def test_node_provider_falls_back_to_global_primary(router: AgentRouter) -> None:
    # A node pinned to codex (not the global primary) falls back to the global primary on infra.
    route = router.resolve_route("review", ProviderId.CODEX)
    assert (route.primary, route.fallback) == (ProviderId.CODEX, ProviderId.CLAUDE)
    assert route.source is RouteSource.FLOW_NODE


def test_node_provider_equal_to_primary_has_no_fallback(router: AgentRouter) -> None:
    # A node pinned to the global primary has no fallback (a primary infra failure is terminal).
    route = router.resolve_route("implementation", ProviderId.CLAUDE)
    assert (route.primary, route.fallback) == (ProviderId.CLAUDE, None)
    assert route.source is RouteSource.FLOW_NODE


def test_node_provider_not_allowlisted_is_rejected(
    config: OrchestratorConfig, make_fake_provider: Callable[..., object]
) -> None:
    # Shrink the allowlist to claude-only; a node pinned to codex must fail closed (PRE.1a).
    cfg = replace(config, agents=replace(config.agents, allowed=(ProviderId.CLAUDE,)))
    providers = {pid: make_fake_provider(pid) for pid in (ProviderId.CODEX, ProviderId.CLAUDE)}
    router = AgentRouter(cfg, providers)
    with pytest.raises(ConfigError):
        router.resolve_route("planning", ProviderId.CODEX)


def test_missing_provider_instance_is_rejected(
    config: OrchestratorConfig, make_fake_provider: Callable[..., object]
) -> None:
    # The global primary (claude) needs an instance; constructing without one must fail closed.
    router = AgentRouter(config, {ProviderId.CODEX: make_fake_provider(ProviderId.CODEX)})
    with pytest.raises(ConfigError):
        router.resolve_route("review")


def test_router_requires_exactly_one_global_primary(
    config: OrchestratorConfig, make_fake_provider: Callable[..., object]
) -> None:
    # Both providers flagged primary → the invariant is violated → construction fails loud.
    providers_cfg = {
        pid: replace(cfg, primary=True) for pid, cfg in config.agents.providers.items()
    }
    cfg = replace(config, agents=replace(config.agents, providers=providers_cfg))
    instances = {pid: make_fake_provider(pid) for pid in (ProviderId.CODEX, ProviderId.CLAUDE)}
    with pytest.raises(ConfigError):
        AgentRouter(cfg, instances)


def test_route_carries_provider_command_and_args(
    router: AgentRouter, config: OrchestratorConfig
) -> None:
    # The route carries only a ProviderId; command/extra_args/security still come from the config.
    route = router.resolve_route("review", ProviderId.CLAUDE)
    assert config.agents.providers[route.primary].command == "claude"
    assert config.agents.providers[route.primary].extra_args == ()
