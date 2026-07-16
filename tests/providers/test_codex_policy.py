"""Codex deny-policy rendering and controlled-home tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.providers.codex_policy import (
    CodexPolicyError,
    command_prefixes,
    permission_config_values,
    prepare_controlled_home,
    render_exec_policy,
)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (("git commit",), (("git", "commit"),)),
        (("  git   push  ", "gh pr create"), (("git", "push"), ("gh", "pr", "create"))),
        (("git push", "git push"), (("git", "push"),)),
    ],
)
def test_command_prefixes_are_provider_neutral_and_deterministic(
    configured: tuple[str, ...], expected: tuple[tuple[str, ...], ...]
) -> None:
    assert command_prefixes(configured) == expected


def test_exec_policy_renders_default_and_custom_commands_as_forbidden() -> None:
    rendered = render_exec_policy(("git commit", "git push", "custom-tool deploy"))
    assert 'pattern = ["git", "commit"]' in rendered
    assert 'pattern = ["git", "push"]' in rendered
    assert 'pattern = ["custom-tool", "deploy"]' in rendered
    assert rendered.count('decision = "forbidden"') == 3


def test_permission_profile_denies_exact_subtree_and_glob_paths() -> None:
    values = permission_config_values(
        sandbox="workspace-write",
        network_access=False,
        denied_read_paths=(".env", "secrets/**", "**/*.pem"),
    )
    assert 'permissions.worc-deny-policy.extends=":workspace"' in values
    assert 'permissions.worc-deny-policy.filesystem.":root"="deny"' in values
    assert 'permissions.worc-deny-policy.filesystem.":minimal"="read"' in values
    assert "permissions.worc-deny-policy.network.enabled=false" in values
    assert 'permissions.worc-deny-policy.filesystem.":workspace_roots".".env"="deny"' in values
    assert 'permissions.worc-deny-policy.filesystem.":workspace_roots"."secrets"="deny"' in values
    assert 'permissions.worc-deny-policy.filesystem.":workspace_roots"."**/*.pem"="deny"' in values


def test_read_only_profile_keeps_denied_paths_and_can_enable_network() -> None:
    values = permission_config_values(
        sandbox="read-only", network_access=True, denied_read_paths=("private/**",)
    )
    assert 'permissions.worc-deny-policy.extends=":read-only"' in values
    assert "permissions.worc-deny-policy.network.enabled=true" in values
    assert any('"private"="deny"' in value for value in values)


def test_full_access_requires_strict_isolation_opt_out() -> None:
    with pytest.raises(CodexPolicyError, match="strict_isolation=false"):
        permission_config_values(
            sandbox="danger-full-access",
            network_access=True,
            denied_read_paths=(".env",),
        )


def test_full_access_opt_out_uses_builtin_profile_without_read_denials() -> None:
    values = permission_config_values(
        sandbox="danger-full-access",
        network_access=True,
        denied_read_paths=(".env", "secrets/**"),
        strict_isolation=False,
    )
    assert values == ('default_permissions=":danger-full-access"',)


def test_controlled_home_hardlinks_auth_and_writes_no_auth_into_policy(tmp_path: Path) -> None:
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    secret = "approved-auth-material-123456789"
    source_auth = user_home / "auth.json"
    source_auth.write_text(secret, encoding="utf-8")

    home, rules_path = prepare_controlled_home(
        user_home, tmp_path / "instance", ("git push", "custom deploy")
    )

    assert (home / "auth.json").samefile(source_auth)
    assert (home / "auth.json").stat().st_ino == source_auth.stat().st_ino
    rules = rules_path.read_text(encoding="utf-8")
    assert secret not in rules
    assert "git" in rules and "custom" in rules


def test_controlled_home_replaces_stale_auth_projection(tmp_path: Path) -> None:
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    source_auth = user_home / "auth.json"
    source_auth.write_text("first-auth-material", encoding="utf-8")
    home, _rules = prepare_controlled_home(user_home, tmp_path / "instance", ("git push",))
    projected = home / "auth.json"
    projected.unlink()
    projected.write_text("stale-auth-material", encoding="utf-8")

    prepare_controlled_home(user_home, tmp_path / "instance", ("git push",))

    assert projected.samefile(source_auth)


def test_controlled_home_removes_stale_projection_when_source_auth_disappears(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    source_auth = user_home / "auth.json"
    source_auth.write_text("first-auth-material", encoding="utf-8")
    home, _rules = prepare_controlled_home(user_home, tmp_path / "instance", ("git push",))
    projected = home / "auth.json"
    source_auth.unlink()

    prepare_controlled_home(user_home, tmp_path / "instance", ("git push",))

    assert not projected.exists()
