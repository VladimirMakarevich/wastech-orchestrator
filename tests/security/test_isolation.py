"""strict_isolation preflight — security/isolation.py + adapter isolation_reasons (§12.8, §6.1).

Proves the deterministic, offline check: a provider that may run must be able to enable its required
isolation; ``extra_args`` or a profile/sandbox that would weaken it is flagged. None of these launch
a CLI.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import AgentsConfig, OrchestratorConfig, ProviderConfig
from wastech_orchestrator.providers import claude as claude_mod
from wastech_orchestrator.providers import codex as codex_mod
from wastech_orchestrator.providers.base import ProviderId, Stage
from wastech_orchestrator.security.isolation import check_isolation


@pytest.fixture
def base_config(packaged_config_text: str) -> OrchestratorConfig:
    return loads_config(packaged_config_text).config


@pytest.fixture
def claude_config(base_config: OrchestratorConfig) -> ProviderConfig:
    return base_config.agents.providers[ProviderId.CLAUDE]


@pytest.fixture
def codex_config(base_config: OrchestratorConfig) -> ProviderConfig:
    return base_config.agents.providers[ProviderId.CODEX]


def _with_provider(
    config: OrchestratorConfig, pid: ProviderId, **overrides: object
) -> OrchestratorConfig:
    providers = dict(config.agents.providers)
    providers[pid] = replace(providers[pid], **overrides)
    return replace(config, agents=replace(config.agents, providers=providers))


# --- adapter-level isolation_reasons --------------------------------------------------------------


def test_claude_clean_config_has_no_reasons(claude_config: ProviderConfig) -> None:
    assert claude_mod.isolation_reasons(claude_config) == []


def test_codex_clean_config_has_no_reasons(codex_config: ProviderConfig) -> None:
    assert codex_mod.isolation_reasons(codex_config) == []


def test_claude_unknown_profile_is_flagged(claude_config: ProviderConfig) -> None:
    reasons = claude_mod.isolation_reasons(replace(claude_config, permission_profile="bogus"))
    assert reasons and "bogus" in reasons[0]


def test_claude_full_access_profile_is_flagged(claude_config: ProviderConfig) -> None:
    reasons = claude_mod.isolation_reasons(
        replace(claude_config, permission_profile="danger-full-access")
    )
    assert reasons


def test_claude_skip_permissions_extra_arg_is_flagged(claude_config: ProviderConfig) -> None:
    reasons = claude_mod.isolation_reasons(
        replace(claude_config, extra_args=("--dangerously-skip-permissions",))
    )
    assert reasons


def test_claude_bypass_permission_mode_extra_arg_is_flagged(claude_config: ProviderConfig) -> None:
    reasons = claude_mod.isolation_reasons(
        replace(claude_config, extra_args=("--permission-mode", "bypassPermissions"))
    )
    assert reasons


def test_codex_danger_full_access_sandbox_is_flagged(codex_config: ProviderConfig) -> None:
    reasons = codex_mod.isolation_reasons(replace(codex_config, sandbox="danger-full-access"))
    assert reasons and "danger-full-access" in reasons[0]


def test_codex_bypass_extra_arg_is_flagged(codex_config: ProviderConfig) -> None:
    reasons = codex_mod.isolation_reasons(
        replace(codex_config, extra_args=("--dangerously-bypass-approvals-and-sandbox",))
    )
    assert reasons


# --- config-level check_isolation -----------------------------------------------------------------


def test_default_config_passes(base_config: OrchestratorConfig) -> None:
    assert check_isolation(base_config) == []


def test_codex_full_access_fails_with_provider_prefix(base_config: OrchestratorConfig) -> None:
    cfg = _with_provider(base_config, ProviderId.CODEX, sandbox="danger-full-access")
    reasons = check_isolation(cfg)
    assert reasons and reasons[0].startswith("codex:")


def test_claude_full_access_fails_with_provider_prefix(base_config: OrchestratorConfig) -> None:
    cfg = _with_provider(base_config, ProviderId.CLAUDE, permission_profile="danger-full-access")
    reasons = check_isolation(cfg)
    assert any(r.startswith("claude:") for r in reasons)


def test_unrouted_unallowed_provider_is_not_checked(base_config: OrchestratorConfig) -> None:
    # codex has a forbidden sandbox, but it is neither allowed nor routed → must NOT brick the run.
    claude_only_routes = {
        stage: replace(route, primary=ProviderId.CLAUDE, fallback=None)
        for stage, route in base_config.agents.routing.items()
    }
    agents: AgentsConfig = replace(
        base_config.agents, allowed=(ProviderId.CLAUDE,), routing=claude_only_routes
    )
    cfg = _with_provider(
        replace(base_config, agents=agents), ProviderId.CODEX, sandbox="danger-full-access"
    )
    assert check_isolation(cfg) == []


def test_routed_fallback_provider_is_checked(base_config: OrchestratorConfig) -> None:
    # codex is the review fallback in the default routes, so its bad sandbox must still be flagged
    # even though no stage names it primary.
    review = base_config.agents.routing[Stage.REVIEW]
    assert review.fallback is ProviderId.CLAUDE or review.primary is ProviderId.CODEX
    cfg = _with_provider(base_config, ProviderId.CODEX, sandbox="danger-full-access")
    assert any(r.startswith("codex:") for r in check_isolation(cfg))
