"""Config generation DoD: selected-only providers, §5 routing, safe defaults, absolute-path YAML
round-trip (Windows/macOS), and a clean load+validate of the generated config."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import FootprintLocation, FootprintTracking
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.install import config_writer
from wastech_orchestrator.install.config_writer import InstallSpec, build_and_validate
from wastech_orchestrator.providers.base import ProviderId, Stage

_ROUTABLE = (
    Stage.REFINEMENT,
    Stage.PLANNING,
    Stage.IMPLEMENTATION,
    Stage.REVIEW,
    Stage.FIXING,
    Stage.SUMMARY,
)


def _spec(
    tmp_path: Path,
    providers: tuple[ProviderId, ...],
    *,
    checks: tuple[str, ...] = (),
    create_pr: bool = True,
    auto_mode: bool = False,
) -> InstallSpec:
    return InstallSpec(
        repo_url="git@github.com:me/my-repo.git",
        repo_local_path=tmp_path / "my-repo",
        base_branch="main",
        workspace=tmp_path / "my-repo-orchestrator",
        providers=providers,
        checks=checks,
        create_pull_request=create_pr,
        auto_mode=auto_mode,
    )


def test_codex_only_routes_to_codex_with_no_fallback(tmp_path: Path) -> None:
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert cfg.agents.allowed == (ProviderId.CODEX,)
    assert set(cfg.agents.providers) == {ProviderId.CODEX}
    for stage in _ROUTABLE:
        assert cfg.agents.routing[stage].primary is ProviderId.CODEX
        assert cfg.agents.routing[stage].fallback is None


def test_claude_only_routes_to_claude_with_no_fallback(tmp_path: Path) -> None:
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CLAUDE,)))).config
    assert cfg.agents.allowed == (ProviderId.CLAUDE,)
    assert set(cfg.agents.providers) == {ProviderId.CLAUDE}
    assert ProviderId.CODEX not in cfg.agents.providers
    for stage in _ROUTABLE:
        assert cfg.agents.routing[stage].primary is ProviderId.CLAUDE
        assert cfg.agents.routing[stage].fallback is None


def test_both_use_the_default_routing_table(tmp_path: Path) -> None:
    cfg = loads_config(
        build_and_validate(_spec(tmp_path, (ProviderId.CODEX, ProviderId.CLAUDE)))
    ).config
    assert set(cfg.agents.allowed) == {ProviderId.CODEX, ProviderId.CLAUDE}
    assert cfg.agents.routing[Stage.IMPLEMENTATION].primary is ProviderId.CLAUDE
    assert cfg.agents.routing[Stage.IMPLEMENTATION].fallback is ProviderId.CODEX
    assert cfg.agents.routing[Stage.REVIEW].primary is ProviderId.CODEX
    assert cfg.agents.routing[Stage.REVIEW].fallback is ProviderId.CLAUDE


def test_generated_config_is_external_footprint_with_absolute_paths(tmp_path: Path) -> None:
    spec = _spec(tmp_path, (ProviderId.CODEX,))
    cfg = loads_config(build_and_validate(spec)).config
    assert cfg.git.footprint.location is FootprintLocation.EXTERNAL
    assert cfg.git.footprint.tracking is FootprintTracking.NONE
    assert cfg.git.footprint.external_root == str(tmp_path / "my-repo-orchestrator")
    assert cfg.repo.local_path == str(tmp_path / "my-repo")
    # Anti-traversal: the `-orchestrator` sibling must not count as inside `my-repo`.
    assert validate_config(cfg) == []


def test_generated_config_is_stamped_with_schema_version(tmp_path: Path) -> None:
    from wastech_orchestrator.config.schema import CONFIG_SCHEMA_VERSION

    text = build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))
    assert f"schema_version: {CONFIG_SCHEMA_VERSION}" in text


def test_safe_security_defaults_are_written(tmp_path: Path) -> None:
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert cfg.security.strict_isolation is True
    assert "git push" in cfg.security.denied_commands
    assert "gh pr create" in cfg.security.denied_commands
    assert cfg.agents.providers[ProviderId.CODEX].permission_profile == "workspace-write"
    assert cfg.agents.providers[ProviderId.CODEX].sandbox == "workspace-write"
    assert cfg.agents.providers[ProviderId.CODEX].extra_args == ()


def test_create_pr_and_auto_mode_are_reflected(tmp_path: Path) -> None:
    cfg = loads_config(
        build_and_validate(_spec(tmp_path, (ProviderId.CODEX,), create_pr=False, auto_mode=True))
    ).config
    assert cfg.git.create_pull_request is False
    assert cfg.orchestrator.auto_mode.enabled is True


def test_checks_are_reflected_including_empty(tmp_path: Path) -> None:
    with_checks = loads_config(
        build_and_validate(_spec(tmp_path, (ProviderId.CODEX,), checks=("pytest", "ruff check .")))
    ).config
    assert with_checks.checks.commands == ("pytest", "ruff check .")

    empty = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert empty.checks.commands == ()


@pytest.mark.parametrize(
    "path",
    [
        r"C:\projects\my-repo",
        r"C:\Users\tom\new-repo-orchestrator",  # \U \t \n would all corrupt if double-quoted
        "/Users/vlad/projects/my-repo-orchestrator",
    ],
)
def test_absolute_path_strings_survive_yaml_roundtrip(path: str) -> None:
    text = config_writer.render({"repo": {"local_path": path}})
    assert yaml.safe_load(text)["repo"]["local_path"] == path
