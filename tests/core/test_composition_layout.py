"""WRI-004 wiring proof: the one :class:`RuntimeLayout` reaches every consumer's correct surface.

The layout here uses **distinct** control/private/exchange directories (not the coincident
``.worc`` default) so each assertion proves the consumer read the field it owns, not that the paths
merely happen to be equal today.
"""

from __future__ import annotations

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
    # Exchange surface (explicit but unused by WRI-004).
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
    # WRI-010 (cluster-exit hook for WRI-002/003): the frozen-control-bundle root under private_home
    # is a named deny target so the provider projection denies it by name, not by coincidence of
    # location — and it survives WRI-005 relocating private_home.
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    policy = build_internal_deny_policy(config, layout, env_file=None)
    bundle_root = layout.private_home / "control-bundles"
    assert policy.frozen_control_bundle == bundle_root
    assert bundle_root in policy.denied_paths


def test_cli_layout_for_reproduces_default_paths(git_repo, make_git_config) -> None:
    # Path-for-path: the CLI composition boundary resolves the byte-identical pre-WRI-004 home.
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = cli.layout_for(config)
    assert layout.control_home == Path(config.repo.local_path) / ".worc"
    assert layout.private_home == Path(config.repo.local_path) / ".worc"
    assert layout.exchange_root == Path(config.repo.local_path) / ".worc-io"
