"""Unit tests for the Codex permission-profile generator (WRI-003).

These lock the profile *shape* the adapter injects. Real OS enforcement of the rendered profile is
proven separately by the no-model ``codex sandbox`` host smoke (test_codex_canary_smoke.py); here we
assert the generated mapping and its inline-``-c`` rendering are correct and portable.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

from wastech_orchestrator.providers.base import ProviderError
from wastech_orchestrator.providers.codex_profile import (
    PROFILE_NAME,
    build_codex_permission_profile,
    render_permission_profile_arg,
)
from wastech_orchestrator.runtime_layout import InternalDenyPolicy, ProviderWriteGuardPolicy

# The fixture provider home, plus the profile key it renders to. The generator normalizes every
# path through the `to_native=str` seam, so a POSIX fixture literal becomes `\opt\codexhome` on
# native Windows — assert the normalized form, not the literal, or these tests only pass on POSIX.
PROVIDER_HOME = Path("/opt/codexhome")
PROVIDER_HOME_KEY = str(PROVIDER_HOME)


def _deny(root: Path) -> InternalDenyPolicy:
    return InternalDenyPolicy(
        control_home=root / ".worc",
        private_home=root / ".worc",
        env_file=root / ".worc" / ".env",
        provider_homes=(PROVIDER_HOME,),
        frozen_control_bundle=root / ".worc" / "control-bundles",
        frozen_instruction_bundle=root / ".worc" / "instruction-bundles",
    )


def _write_guard(root: Path) -> ProviderWriteGuardPolicy:
    return ProviderWriteGuardPolicy(
        exchange_root=root / ".worc-io",
        git_dir=root / ".git",
        git_common_dir=root / ".git",
        hooks_dir=root / ".git" / "hooks",
        tasks_dir=root / "tasks",
    )


def test_read_only_extends_readonly_and_grants_workspace_read(tmp_path: Path) -> None:
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="read-only",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=None,
        denied_read_paths=(),
    )
    fs = profile["filesystem"]
    assert profile["extends"] == ":read-only"
    assert fs[str(root)] == "read"
    assert fs[str(root / ".worc")] == "deny"
    assert profile["network"] == {"enabled": False}


def test_workspace_write_grants_write_and_readonly_guard(tmp_path: Path) -> None:
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=_write_guard(root),
        denied_read_paths=(),
    )
    fs = profile["filesystem"]
    assert profile["extends"] == ":workspace"
    assert fs[str(root)] == "write"
    # write-guard roots stay readable but write-denied (a more-specific "read" rule)
    assert fs[str(root / ".worc-io")] == "read"
    assert fs[str(root / ".git")] == "read"
    assert fs[str(root / "tasks")] == "read"
    # deny set still wins
    assert fs[str(root / ".worc")] == "deny"
    assert fs[PROVIDER_HOME_KEY] == "deny"
    # VF-20: governance/instruction files are ordinary, editable content — no per-file guard entry.
    for name in ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md"):
        assert not any(name in key for key in fs)


def test_deny_applied_last_wins_over_read_guard(tmp_path: Path) -> None:
    """If a deny path coincides with a write-guard read path, deny must win (applied last)."""
    root = tmp_path / "clone"
    # Contrive a collision: env_file sits exactly on the exchange root.
    deny = InternalDenyPolicy(
        control_home=root / ".worc",
        private_home=root / ".worc",
        env_file=root / ".worc-io",
        provider_homes=(),
    )
    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=deny,
        write_guard=_write_guard(root),
        denied_read_paths=(),
    )
    assert profile["filesystem"][str(root / ".worc-io")] == "deny"


def test_read_isolation_off_downgrades_deny_to_read_keeps_blacklist(tmp_path: Path) -> None:
    # VF-6: the private set is downgraded deny→read (readable, still write-denied) while the public
    # denied_read_paths blacklist stays fully denied and the write-guard stays read-only.
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=_write_guard(root),
        denied_read_paths=(".env", "secrets/**"),
        read_isolation_off=True,
    )
    fs = profile["filesystem"]
    assert fs[str(root / ".worc")] == "read"  # private set now readable, still not writable
    assert fs[PROVIDER_HOME_KEY] == "read"  # provider home readable for native discovery
    assert fs[str(root / ".env")] == "deny"  # public blacklist unchanged
    assert fs[str(root / "secrets")] == "deny"
    assert fs[str(root)] == "write"  # workspace still writable
    assert fs[str(root / ".worc-io")] == "read"  # write-guard still read-only


def test_read_isolation_default_denies_private_set(tmp_path: Path) -> None:
    # Regression: default (read_isolation_off=False) keeps the private set fully denied.
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=_write_guard(root),
        denied_read_paths=(),
    )
    assert profile["filesystem"][str(root / ".worc")] == "deny"
    assert profile["filesystem"][PROVIDER_HOME_KEY] == "deny"


def test_denied_read_dir_glob_reduced_to_subtree_no_scan(tmp_path: Path) -> None:
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=None,
        write_guard=None,
        denied_read_paths=(".env", "secrets/**"),
    )
    fs = profile["filesystem"]
    assert fs[str(root / ".env")] == "deny"
    assert fs[str(root / "secrets")] == "deny"  # dir deny covers the subtree, no wildcard
    assert "glob_scan_max_depth" not in fs


def test_single_level_glob_sets_scan_depth(tmp_path: Path) -> None:
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="read-only",
        working_directory=str(root),
        deny_policy=None,
        write_guard=None,
        denied_read_paths=("*.pem",),
    )
    assert profile["filesystem"]["glob_scan_max_depth"] == 8


def test_unbounded_glob_rejected_under_strict_but_allowed_when_off(tmp_path: Path) -> None:
    root = tmp_path / "clone"
    kwargs = {
        "permission_profile": "read-only",
        "working_directory": str(root),
        "deny_policy": None,
        "write_guard": None,
        "denied_read_paths": ("**/*.env",),
    }
    with pytest.raises(ProviderError):
        build_codex_permission_profile(strict_isolation=True, **kwargs)  # type: ignore[arg-type]
    profile = build_codex_permission_profile(strict_isolation=False, **kwargs)  # type: ignore[arg-type]
    assert profile["filesystem"]["glob_scan_max_depth"] == 8


def test_unknown_permission_profile_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProviderError):
        build_codex_permission_profile(
            permission_profile="danger-full-access",
            working_directory=str(tmp_path),
            deny_policy=None,
            write_guard=None,
            denied_read_paths=(),
        )


def test_render_arg_is_single_permissions_inline_table(tmp_path: Path) -> None:
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="read-only",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=None,
        denied_read_paths=(),
    )
    arg = render_permission_profile_arg(profile)
    assert arg.startswith(f"permissions.{PROFILE_NAME}={{")
    assert '":minimal" = "read"' in arg
    assert '"network" = { "enabled" = false }' in arg


def test_windows_paths_escaped_via_native_seam(tmp_path: Path) -> None:
    """The injected path seam renders Windows paths with escaped backslashes."""
    root = tmp_path / "clone"

    def to_win(p: Path) -> str:
        # Map a POSIX fixture path onto a drive-letter Windows path for rendering-on-macOS coverage.
        return str(PureWindowsPath("C:/") / PureWindowsPath(*p.parts[1:]))

    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=None,
        denied_read_paths=(),
        to_native=to_win,
    )
    arg = render_permission_profile_arg(profile)
    assert "\\\\" in arg  # backslashes doubled for a valid TOML basic string
    assert "C:\\\\" in arg
