"""Build the agent discovery component from config + providers (automatic check discovery §6).

Selects which provider runs discovery (the explicit ``checks.discovery.provider`` or the first
available allowed provider) and constructs the :class:`AgentCheckDiscovery`. Returns ``None`` when
agent fallback is disabled, no cheap model is configured, or no provider is available — in which
case resolution stays deterministic.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.checks.agent import AgentCheckDiscovery
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.base import AgentProvider, ProviderId


def select_discovery_provider(
    config: OrchestratorConfig, providers: dict[ProviderId, AgentProvider]
) -> AgentProvider | None:
    """The configured discovery provider, else the first allowed provider whose CLI is present."""
    cfg = config.checks.discovery
    if cfg.provider is not None:
        return providers.get(cfg.provider)
    for pid in config.agents.allowed:
        provider = providers.get(pid)
        if provider is not None and provider.preflight().executable_found:
            return provider
    return None


def build_discovery(
    config: OrchestratorConfig,
    providers: dict[ProviderId, AgentProvider],
    artifacts_root: str | Path,
) -> AgentCheckDiscovery | None:
    """Construct :class:`AgentCheckDiscovery`, or ``None`` when agent fallback should not run."""
    cfg = config.checks.discovery
    if not cfg.agent_fallback or not cfg.model:
        return None  # fallback disabled or no cheap discovery model configured (opt-in)
    provider = select_discovery_provider(config, providers)
    if provider is None:
        return None
    return AgentCheckDiscovery(provider, discovery_cfg=cfg, artifacts_root=artifacts_root)
