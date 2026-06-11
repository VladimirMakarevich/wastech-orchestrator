"""Validator: every §11/§21.4 reject path plus the task-override helper."""

from __future__ import annotations

from dataclasses import replace

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import (
    FootprintLocation,
    FootprintTracking,
    OrchestratorConfig,
    RouteConfig,
)
from wastech_orchestrator.config.validation import check_task_route_override, validate_config
from wastech_orchestrator.providers.base import ProviderId, Stage


@pytest.fixture
def base_config(packaged_config_text: str) -> OrchestratorConfig:
    return loads_config(packaged_config_text).config


def _with_agents(config: OrchestratorConfig, **changes: object) -> OrchestratorConfig:
    return replace(config, agents=replace(config.agents, **changes))


def _with_footprint(config: OrchestratorConfig, **changes: object) -> OrchestratorConfig:
    footprint = replace(config.git.footprint, **changes)
    return replace(config, git=replace(config.git, footprint=footprint))


def test_packaged_config_validates_clean(base_config: OrchestratorConfig) -> None:
    assert validate_config(base_config) == []


def test_route_primary_not_in_allowed_is_rejected(base_config: OrchestratorConfig) -> None:
    bad = _with_agents(base_config, allowed=(ProviderId.CODEX,))  # routes still name claude
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("agents.allowed" in issue for issue in exc.value.issues)


def test_route_provider_without_providers_entry_is_rejected(
    base_config: OrchestratorConfig,
) -> None:
    providers = {ProviderId.CODEX: base_config.agents.providers[ProviderId.CODEX]}
    bad = _with_agents(base_config, providers=providers)  # routes still name claude
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("agents.providers" in issue for issue in exc.value.issues)


def test_non_routable_stage_in_routing_is_rejected(base_config: OrchestratorConfig) -> None:
    routing = {**base_config.agents.routing, Stage.TESTING: RouteConfig(ProviderId.CODEX, None)}
    bad = _with_agents(base_config, routing=routing)
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("agent-routed" in issue for issue in exc.value.issues)


def test_max_total_below_max_fix_cycles_is_rejected(base_config: OrchestratorConfig) -> None:
    bad = _with_agents(base_config, max_fix_cycles=5, max_total_fix_iterations=3)
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("max_total_fix_iterations" in issue for issue in exc.value.issues)


def test_max_subtasks_below_two_is_rejected(base_config: OrchestratorConfig) -> None:
    decomposition = replace(base_config.agents.decomposition, max_subtasks=1)
    bad = _with_agents(base_config, decomposition=decomposition)
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("max_subtasks" in issue for issue in exc.value.issues)


@pytest.mark.parametrize(
    "flag",
    [
        "--dangerously-bypass-approvals-and-sandbox",
        "--sandbox=danger-full-access",
    ],
)
def test_sandbox_bypass_extra_arg_is_rejected(base_config: OrchestratorConfig, flag: str) -> None:
    codex = replace(base_config.agents.providers[ProviderId.CODEX], extra_args=(flag,))
    providers = {**base_config.agents.providers, ProviderId.CODEX: codex}
    bad = _with_agents(base_config, providers=providers)
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("extra_args" in issue for issue in exc.value.issues)


def test_sandbox_bypass_split_value_is_rejected(base_config: OrchestratorConfig) -> None:
    codex = replace(
        base_config.agents.providers[ProviderId.CODEX],
        extra_args=("--sandbox", "danger-full-access"),
    )
    providers = {**base_config.agents.providers, ProviderId.CODEX: codex}
    with pytest.raises(ConfigError):
        validate_config(_with_agents(base_config, providers=providers))


def test_claude_skip_permissions_extra_arg_is_rejected(base_config: OrchestratorConfig) -> None:
    claude = replace(
        base_config.agents.providers[ProviderId.CLAUDE],
        extra_args=("--dangerously-skip-permissions",),
    )
    providers = {**base_config.agents.providers, ProviderId.CLAUDE: claude}
    with pytest.raises(ConfigError):
        validate_config(_with_agents(base_config, providers=providers))


def test_external_with_commit_tracking_is_rejected(base_config: OrchestratorConfig) -> None:
    bad = _with_footprint(
        base_config, location=FootprintLocation.EXTERNAL, tracking=FootprintTracking.COMMIT
    )
    with pytest.raises(ConfigError):
        validate_config(bad)


def test_external_with_exclude_local_tracking_is_rejected(base_config: OrchestratorConfig) -> None:
    bad = _with_footprint(
        base_config, location=FootprintLocation.EXTERNAL, tracking=FootprintTracking.EXCLUDE_LOCAL
    )
    with pytest.raises(ConfigError):
        validate_config(bad)


def test_in_repo_with_none_tracking_is_rejected(base_config: OrchestratorConfig) -> None:
    bad = _with_footprint(
        base_config, location=FootprintLocation.IN_REPO, tracking=FootprintTracking.NONE
    )
    with pytest.raises(ConfigError):
        validate_config(bad)


def test_external_root_inside_local_path_is_rejected(base_config: OrchestratorConfig) -> None:
    # repo.local_path defaults to ./workspace/repo; an external_root inside it is a traversal.
    bad = _with_footprint(base_config, external_root="./workspace/repo/artifacts")
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("external_root" in issue for issue in exc.value.issues)


def test_in_repo_commit_is_accepted(base_config: OrchestratorConfig) -> None:
    ok = _with_footprint(
        base_config, location=FootprintLocation.IN_REPO, tracking=FootprintTracking.COMMIT
    )
    assert validate_config(ok) == []


def test_task_override_must_pick_allowed_routable_provider(
    base_config: OrchestratorConfig,
) -> None:
    assert check_task_route_override({Stage.PLANNING: ProviderId.CODEX}, base_config) == []
    # Non-routable stage and unknown providers surface as problems (not exceptions).
    only_codex = _with_agents(base_config, allowed=(ProviderId.CODEX,))
    problems = check_task_route_override({Stage.PLANNING: ProviderId.CLAUDE}, only_codex)
    assert problems
    assert check_task_route_override({Stage.TESTING: ProviderId.CODEX}, base_config)
