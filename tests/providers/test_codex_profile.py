"""Unit tests for the Codex permission-profile generator.

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


def _deny(root: Path) -> InternalDenyPolicy:
    return InternalDenyPolicy(
        control_home=root / ".worc",
        private_home=root / ".worc",
        env_file=root / ".worc" / ".env",
        runs_home=root / ".worc" / "runs",
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
    # The per-task runtime root is denied by NAME, not by sitting under the private home — so it
    # stays denied if the private home is ever relocated out of tree.
    assert fs[str(root / ".worc" / "runs")] == "deny"
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
    # Governance/instruction files are ordinary, editable content — no per-file guard entry.
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
    )
    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=deny,
        write_guard=_write_guard(root),
        denied_read_paths=(),
    )
    assert profile["filesystem"][str(root / ".worc-io")] == "deny"


def test_the_private_set_stays_denied_whatever_read_isolation_says(tmp_path: Path) -> None:
    """The private/control set is ``deny`` unconditionally — the profile has no read-isolation knob.

    Downgrading it to ``read`` when read-isolation is off — i.e. on the shipped default — would
    hand the sandboxed shell the private home and the resolved env-file. The env-file names are
    withheld from the child environment *because* the agent cannot read the file, and that reasoning
    only holds while this stays ``deny``. Native discovery loses nothing: the CLI reads its own user
    config and auth outside this profile, and ``--ignore-user-config`` is what gates them. The
    provider config home is not in this set at all.
    """
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=_write_guard(root),
        denied_read_paths=(".env", "secrets/**"),
    )
    fs = profile["filesystem"]
    assert fs[str(root / ".worc")] == "deny"  # private set unreadable and unwritable
    assert fs[str(root / ".worc" / ".env")] == "deny"  # the env-file the withholding rule needs
    assert fs[str(root / ".worc" / "runs")] == "deny"  # frozen bundles / seals / quarantine
    assert fs[str(root / ".env")] == "deny"  # public blacklist unchanged
    assert fs[str(root / "secrets")] == "deny"
    assert fs[str(root)] == "write"  # workspace still writable
    assert fs[str(root / ".worc-io")] == "read"  # write-guard still read-only


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
    # Every deny entry survives the Windows shape, including the per-task runtime root.
    assert to_win(root / ".worc" / "runs").replace("\\", "\\\\") in arg


def test_git_evidence_does_not_change_the_codex_profile(tmp_path: Path) -> None:
    """The grant is a Claude-side construct; this pins that Codex needs nothing from it, and why.

    Codex's profile has three keys — ``extends`` / ``filesystem`` / ``network`` — and no command or
    verb dimension at all. Under ``read-only`` it already permits command execution, so ``git log``
    works there today; what forbids mutation is not a list of verbs but the sandbox: the workspace
    is mounted ``read`` and, for a node its flow granted no network, the network is off — so
    ``git commit`` fails for want of a writable ``.git`` and ``git push`` for want of a network.
    The condition there is the GRANT, not ``strict_isolation``: a node granted network gets
    ``network.enabled = true`` in this profile at either value of the key. That is a stronger
    guarantee than an allowlist and nothing in a prompt, task or flow can argue with it, which is
    why the two providers are made to agree on the observable contract (history readable, repository
    unchangeable, nothing published) rather than on a symmetric list of verbs.

    The second half of that contract is a DEFAULT, not an invariant, and this is one of the places
    most easily misread as though it were. ``security.strict_isolation: false`` puts every node
    online (asserted below), so ``git push`` there has somewhere to go and credentials it picks up
    by itself; what keeps publication the orchestrator's is the product mandate plus detection on
    our own ``origin``, not this profile. The first half — the workspace mounted ``read`` —
    survives the mode, and that is asserted below too.
    """
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="read-only",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=None,
        denied_read_paths=(),
    )
    # The three keys, unchanged: there is no place in this shape for a verb allowlist to land.
    assert set(profile) == {"extends", "filesystem", "network"}
    # The mutation ban, stated as the sandbox states it.
    assert profile["filesystem"][str(root)] == "read"  # `git commit` has nothing to write to
    assert profile["network"] == {"enabled": False}  # `git push` has nowhere to go
    # And the same two claims in the advanced mode, where only the first one still holds.
    in_mode = build_codex_permission_profile(
        permission_profile="read-only",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=None,
        denied_read_paths=(),
        network_access=True,
        strict_isolation=False,
    )
    assert in_mode["filesystem"][str(root)] == "read"  # still nothing to write to
    assert in_mode["network"] == {"enabled": True}  # but `git push` now has somewhere to go


def test_the_advanced_mode_grants_the_volume_root_and_keeps_every_carve_out(tmp_path: Path) -> None:
    """Write extends to the whole volume, and the floor survives by being more specific.

    The carve-out set is asserted by NAME, one entry at a time, because the short form of the floor
    ("`.git` and `.worc`") does not show all of it: ``runs_home`` and the resolved env-file are the
    entries an implementer reading that short form drops. The provider config home is deliberately
    NOT a carve-out — a deny there stops Codex's own ``apply_patch`` sandbox helper from executing
    on standalone installs, where the ``codex`` binary lives inside that home.
    """
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=_write_guard(root),
        denied_read_paths=(),
        network_access=True,
        strict_isolation=False,
    )
    fs = profile["filesystem"]
    assert fs[str(Path(root.anchor))] == "write"  # `~/.nuget`, `/tmp`, a PATH directory
    assert fs[str(root)] == "write"
    # The floor: write-denied (as `read`) inside the volume-wide grant.
    for guarded in (root / ".worc-io", root / ".git", root / ".git" / "hooks", root / "tasks"):
        assert fs[str(guarded)] == "read", guarded
    # The private set: denied outright, including BOTH halves the short form hides.
    for private in (root / ".worc", root / ".worc" / ".env", root / ".worc" / "runs"):
        assert fs[str(private)] == "deny", private


def test_outside_the_mode_no_root_grant_appears(tmp_path: Path) -> None:
    # The counterweight: the shipped default's profile is what it always was, key for key. A root
    # `write` slipping in unconditionally is the one mistake in this phase that would remove the
    # floor everywhere at once, so it is pinned as an exact key set, not as an absence.
    root = tmp_path / "clone"
    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=_write_guard(root),
        denied_read_paths=(),
    )
    assert set(profile["filesystem"]) == {
        ":minimal",
        str(root),
        str(root / ".worc-io"),
        str(root / ".git"),
        str(root / ".git" / "hooks"),
        str(root / "tasks"),
        str(root / ".worc"),
        str(root / ".worc" / ".env"),
        str(root / ".worc" / "runs"),
    }


def test_the_root_grant_takes_the_windows_shape_through_the_native_seam(tmp_path: Path) -> None:
    """The volume root is the workspace path's anchor, so it is a drive root on native Windows.

    Codex is the provider that generates a profile on every host, native Windows included (Claude's
    sandbox file is never written there — no Bash sandbox exists), so this is the one place the
    Windows shape of the new grant can be proven at all from a POSIX host.
    """
    root = tmp_path / "clone"

    def to_win(p: Path) -> str:
        return str(PureWindowsPath("C:/") / PureWindowsPath(*p.parts[1:]))

    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory=str(root),
        deny_policy=_deny(root),
        write_guard=_write_guard(root),
        denied_read_paths=(),
        network_access=True,
        strict_isolation=False,
        to_native=to_win,
    )
    assert profile["filesystem"]["C:\\"] == "write"
    assert profile["filesystem"][to_win(root / ".git")] == "read"


def test_a_relative_workspace_gets_no_root_grant(tmp_path: Path) -> None:
    # A relative working directory is a unit harness, not an attempt, and its anchor names no
    # volume: granting `.` would be a rule about the process's cwd, which is not what this means.
    profile = build_codex_permission_profile(
        permission_profile="workspace-write",
        working_directory="clone",
        deny_policy=None,
        write_guard=None,
        denied_read_paths=(),
        strict_isolation=False,
    )
    assert set(profile["filesystem"]) == {":minimal", "clone"}
