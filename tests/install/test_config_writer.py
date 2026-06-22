"""Config generation DoD: selected-only providers, exactly one global primary, safe defaults,
absolute-path YAML round-trip (Windows/macOS), and a clean load+validate of the generated config."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import AuditBranch
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.install import config_writer
from wastech_orchestrator.install.config_writer import InstallSpec, build_and_validate
from wastech_orchestrator.providers.base import ProviderId


def _spec(
    tmp_path: Path,
    providers: tuple[ProviderId, ...],
    *,
    checks: tuple[str, ...] = (),
    create_pr: bool = True,
    auto_mode: bool = False,
    discovery_mode: str = "configured",
) -> InstallSpec:
    return InstallSpec(
        repo_url="git@github.com:me/my-repo.git",
        repo_local_path=tmp_path / "my-repo",
        base_branch="main",
        providers=providers,
        checks=checks,
        create_pull_request=create_pr,
        auto_mode=auto_mode,
        discovery_mode=discovery_mode,
    )


def test_codex_only_marks_codex_primary(tmp_path: Path) -> None:
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert cfg.agents.allowed == (ProviderId.CODEX,)
    assert set(cfg.agents.providers) == {ProviderId.CODEX}
    assert cfg.agents.providers[ProviderId.CODEX].primary is True


def test_claude_only_marks_claude_primary(tmp_path: Path) -> None:
    cfg = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CLAUDE,)))).config
    assert cfg.agents.allowed == (ProviderId.CLAUDE,)
    assert set(cfg.agents.providers) == {ProviderId.CLAUDE}
    assert ProviderId.CODEX not in cfg.agents.providers
    assert cfg.agents.providers[ProviderId.CLAUDE].primary is True


def test_both_mark_exactly_claude_as_primary(tmp_path: Path) -> None:
    cfg = loads_config(
        build_and_validate(_spec(tmp_path, (ProviderId.CODEX, ProviderId.CLAUDE)))
    ).config
    assert set(cfg.agents.allowed) == {ProviderId.CODEX, ProviderId.CLAUDE}
    # Exactly one global primary; Claude is preferred when both are selected.
    primaries = [pid for pid, p in cfg.agents.providers.items() if p.primary]
    assert primaries == [ProviderId.CLAUDE]


def test_generated_config_uses_worc_home_and_audit_trail(tmp_path: Path) -> None:
    spec = _spec(tmp_path, (ProviderId.CODEX,))
    cfg = loads_config(build_and_validate(spec)).config
    # The task + summary are audit-committed in the repo; the quarantine lives under .worc/ so
    # rejected tasks are never swept into that commit.
    assert cfg.git.footprint.audit_on_branch is AuditBranch.TASK
    expected_quarantine = tmp_path / "my-repo" / ".worc" / "tasks" / "rejected"
    assert cfg.validation.quarantine_folder == str(expected_quarantine)
    assert cfg.repo.local_path == str(tmp_path / "my-repo")
    assert cfg.orchestrator.poll_interval_seconds == 300
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
    assert "gh pr merge" in cfg.security.denied_commands
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


def test_discovery_block_is_rendered(tmp_path: Path) -> None:
    from wastech_orchestrator.config.schema import CheckDiscoveryMode

    configured = loads_config(build_and_validate(_spec(tmp_path, (ProviderId.CODEX,)))).config
    assert configured.checks.discovery.mode is CheckDiscoveryMode.CONFIGURED

    auto = loads_config(
        build_and_validate(_spec(tmp_path, (ProviderId.CODEX,), discovery_mode="auto"))
    ).config
    assert auto.checks.discovery.mode is CheckDiscoveryMode.AUTO


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
