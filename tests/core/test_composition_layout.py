"""Wiring proof: the one :class:`RuntimeLayout` reaches every consumer's correct surface.

The layout here uses **distinct** control/private/exchange directories (not the coincident
``.worc`` default) so each assertion proves the consumer read the field it owns, not that the paths
merely happen to be equal today.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from wastech_orchestrator import cli
from wastech_orchestrator.composition import (
    build_internal_deny_policy,
    build_orchestrator,
    build_providers,
)
from wastech_orchestrator.runtime_layout import RuntimeLayout


def _distinct_layout(repo_root: str, tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout(
        repo_root=Path(repo_root),
        control_home=tmp_path / "ctrl",
        private_home=tmp_path / "priv",
        exchange_root=tmp_path / "xchg",
    )


def test_providers_are_rooted_at_private_home(git_repo, make_git_config, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    providers = build_providers(config, layout=layout)
    assert providers, "expected at least one configured provider"
    for provider in providers.values():
        assert provider._artifacts_root == tmp_path / "priv"


def test_providers_receive_the_internal_deny_policy(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # Wiring guard: build_providers must project the internal deny policy into EVERY
    # provider
    # so a wiring bug can never silently disable the read/write-deny projection. The provider's own
    # ``_build_argv`` reads ``self._deny_policy`` — an absent one would emit no internal denies.
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    providers = build_providers(config, layout=layout)
    assert providers, "expected at least one configured provider"
    for provider in providers.values():
        assert provider._deny_policy is not None
        assert provider._deny_policy.control_home == tmp_path / "ctrl"
        assert provider._deny_policy.private_home == tmp_path / "priv"


def test_providers_receive_read_isolation_off_flag(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # Wiring guard: the operator's security config (incl. disable_read_isolation) flows into
    # EVERY provider, so each adapter's _build_argv reads the effective read_isolation_off (the
    # formula lives once on SecurityConfig; the adapter never recomputes it).
    base = make_git_config(git_repo.clone, checks=["pytest"])
    config = replace(base, security=replace(base.security, disable_read_isolation=True))
    layout = _distinct_layout(git_repo.clone, tmp_path)
    providers = build_providers(config, layout=layout)
    assert providers, "expected at least one configured provider"
    for provider in providers.values():
        assert provider._security.read_isolation_off is True


def test_router_receives_isolation_checks(git_repo, make_git_config, tmp_path: Path) -> None:
    # Wiring guard: the router's CAPABILITY_UNAVAILABLE host-verified fallback gate needs
    # the
    # offline isolation-check table; build_orchestrator must inject it (else _can_isolate fails
    # closed
    # for every provider and a legitimate cross-provider recovery would be wrongly refused).
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    orch = build_orchestrator(config, layout=layout)
    assert orch._router._isolation_checks  # non-empty
    for pid in config.agents.providers:
        assert pid in orch._router._isolation_checks


def test_orchestrator_consumers_receive_the_right_field(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    env_file = Path("/etc/secrets/prod.env")
    orch = build_orchestrator(config, layout=layout, env_file=env_file)

    # Private surface: artifacts root.
    assert orch._layout is layout
    assert orch._artifacts_root == tmp_path / "priv"
    # Exchange surface (named by the layout, resolved by its consumers).
    assert orch._exchange_root == tmp_path / "xchg"
    # Control surface: flows/tools live under control_home, never private_home.
    assert orch._flow_registry._operator_dir == tmp_path / "ctrl" / "flows"
    assert orch._tool_registry._dir == tmp_path / "ctrl" / "tools"
    # Internal deny policy threaded through with the resolved env-file.
    assert orch._deny_policy is not None
    assert orch._deny_policy.control_home == tmp_path / "ctrl"
    assert orch._deny_policy.private_home == tmp_path / "priv"
    assert env_file in orch._deny_policy.denied_paths


def test_deny_policy_includes_configured_provider_homes(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    policy = build_internal_deny_policy(config, layout, env_file=None)
    # Every configured provider contributes its auth/config home to the deny set (defense in depth,
    # kept out of the public security.denied_read_paths list).
    assert len(policy.provider_homes) == len(config.agents.providers)
    for home in policy.provider_homes:
        assert home in policy.denied_paths
    # Provider homes are not silently folded into the public redaction/skill-scan config.
    assert set(config.security.denied_read_paths).isdisjoint(
        {h.as_posix() for h in policy.provider_homes}
    )


def test_deny_policy_includes_frozen_control_bundle_root(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # The frozen-control-bundle root under private_home
    # is a named deny target so the provider projection denies it by name, not by coincidence of
    # location — and it survives a later relocation of private_home.
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    policy = build_internal_deny_policy(config, layout, env_file=None)
    bundle_root = layout.private_home / "control-bundles"
    assert policy.frozen_control_bundle == bundle_root
    assert bundle_root in policy.denied_paths


def test_cli_layout_for_reproduces_default_paths(git_repo, make_git_config) -> None:
    # Path-for-path: the CLI composition boundary resolves the same home.
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = cli.layout_for(config)
    assert layout.control_home == Path(config.repo.local_path) / ".worc"
    assert layout.private_home == Path(config.repo.local_path) / ".worc"
    assert layout.exchange_root == Path(config.repo.local_path) / ".worc-io"
