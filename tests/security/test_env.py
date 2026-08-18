"""Tests for the environment allowlist and the assigned-variable half of the policy."""

from __future__ import annotations

import pytest

from wastech_orchestrator.config.schema import SecurityConfig
from wastech_orchestrator.security import env as env_mod
from wastech_orchestrator.security.env import (
    build_child_env,
    default_allowed_environment,
    describe_expansions,
    env_name_is_covered,
    expand_allowed_environment,
    launch_critical_env_issue,
    os_essential_env,
)


def _security(*forwarded: str, assigned: dict[str, str] | None = None) -> SecurityConfig:
    """A policy carrying only the two fields the env builder reads."""
    return SecurityConfig(
        strict_isolation=True,
        allowed_environment=forwarded,
        denied_read_paths=(),
        denied_commands=(),
        extra_environment=dict(assigned or {}),
    )


def test_only_allowlisted_keys_survive() -> None:
    parent = {"PATH": "/usr/bin", "HOME": "/home/u", "SECRET_TOKEN": "shh", "AWS_KEY": "x"}
    child = build_child_env(_security("PATH", "HOME"), parent)
    assert child == {"PATH": "/usr/bin", "HOME": "/home/u"}


def test_secrets_in_parent_never_forwarded() -> None:
    parent = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-secret", "GITHUB_TOKEN": "ghp_x"}
    child = build_child_env(_security("PATH", "HOME", "CODEX_HOME"), parent)
    assert "OPENAI_API_KEY" not in child
    assert "GITHUB_TOKEN" not in child


def test_missing_keys_are_skipped_not_blanked() -> None:
    parent = {"PATH": "/usr/bin"}
    child = build_child_env(_security("PATH", "HOME", "CODEX_HOME"), parent)
    assert child == {"PATH": "/usr/bin"}
    assert "HOME" not in child


def test_empty_allowlist_yields_empty_env() -> None:
    assert build_child_env(_security(), {"PATH": "/usr/bin"}) == {}


def test_allowlist_order_is_preserved() -> None:
    parent = {"HOME": "/home/u", "PATH": "/usr/bin", "CODEX_HOME": "/c"}
    child = build_child_env(_security("PATH", "CODEX_HOME", "HOME"), parent)
    assert list(child) == ["PATH", "CODEX_HOME", "HOME"]


def test_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WASTECH_ENV_SENTINEL", "present")
    monkeypatch.delenv("WASTECH_ENV_ABSENT", raising=False)
    child = build_child_env(_security("WASTECH_ENV_SENTINEL", "WASTECH_ENV_ABSENT"))
    assert child == {"WASTECH_ENV_SENTINEL": "present"}


def test_input_parent_mapping_not_mutated() -> None:
    parent = {"PATH": "/usr/bin", "SECRET": "x"}
    build_child_env(_security("PATH"), parent)
    assert parent == {"PATH": "/usr/bin", "SECRET": "x"}


# --- assigned variables (security.extra_environment) --------------------------------------------


def test_assigned_variable_reaches_the_child_without_being_in_the_parent() -> None:
    # The whole point of the key: forwarding can only pass on a value the parent already has.
    child = build_child_env(
        _security("PATH", assigned={"NUGET_PACKAGES": "/repo/.toolcache/nuget"}),
        {"PATH": "/usr/bin"},
    )
    assert child == {"PATH": "/usr/bin", "NUGET_PACKAGES": "/repo/.toolcache/nuget"}


def test_assigned_value_wins_over_the_forwarded_one() -> None:
    child = build_child_env(
        _security("PATH", "LANG", assigned={"LANG": "C.UTF-8"}),
        {"PATH": "/usr/bin", "LANG": "ru_RU.UTF-8"},
    )
    assert child["LANG"] == "C.UTF-8"
    # The name keeps its forwarded position: assignment overrides a value, it does not re-order.
    assert list(child) == ["PATH", "LANG"]


def test_key_order_is_forwarded_then_assigned() -> None:
    # Compared as a list, not a set: a run's environment has to be reproducible between runs, and
    # `os.environ` iteration order is not.
    child = build_child_env(
        _security("PATH", "HOME", assigned={"B_VAR": "2", "A_VAR": "1"}),
        {"HOME": "/home/u", "PATH": "/usr/bin"},
    )
    assert list(child) == ["PATH", "HOME", "B_VAR", "A_VAR"]


def test_empty_assigned_value_is_a_real_assignment() -> None:
    # Not the same as absent, and something forwarding cannot express at all.
    child = build_child_env(_security("PATH", assigned={"DOTNET_NOLOGO": ""}), {"PATH": "/usr/bin"})
    assert child["DOTNET_NOLOGO"] == ""


def test_no_assigned_variables_reproduces_the_forward_only_environment() -> None:
    # И-5: absent key => today's behavior byte for byte, including key order.
    parent = {"PATH": "/usr/bin", "HOME": "/home/u", "CODEX_HOME": "/c"}
    forwarded = _security("PATH", "HOME", "CODEX_HOME")
    child = build_child_env(forwarded, parent)
    assert child == parent
    assert list(child) == list(parent)


def test_windows_essentials_include_systemroot() -> None:
    # SystemRoot is load-bearing: the Node-based claude.exe crashes at startup without it.
    win = os_essential_env("Windows")
    assert "SystemRoot" in win
    assert "PATHEXT" in win
    # The Windows set must not leak into the POSIX one, and vice versa.
    assert "SystemRoot" not in os_essential_env("Linux")


def test_posix_essentials_for_linux_and_darwin() -> None:
    # WSL reports as "Linux", so the POSIX set covers it too.
    posix = os_essential_env("Linux")
    assert "LD_LIBRARY_PATH" in posix
    assert os_essential_env("Darwin") == posix
    assert "APPDATA" not in posix


def test_default_allowed_environment_is_base_plus_os_essentials() -> None:
    base = ("PATH", "HOME", "USER", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
    win = default_allowed_environment("Windows")
    assert win[: len(base)] == base  # base first, OS essentials appended
    assert win == base + os_essential_env("Windows")
    assert "SystemRoot" in win
    assert "SystemRoot" not in default_allowed_environment("Linux")


def test_default_allowed_environment_detects_current_os() -> None:
    # No argument => current OS; must at least carry the cross-platform base.
    current = default_allowed_environment()
    assert {"PATH", "HOME", "CODEX_HOME"} <= set(current)


def test_default_sizes_are_pinned() -> None:
    # The guide distinguishes the shipped template (the cross-platform union) from what `install`
    # writes (the host OS default alone). Pin all three counts so that documented distinction
    # cannot drift away from the code that decides it.
    assert len(default_allowed_environment("Linux")) == 9
    assert len(default_allowed_environment("Darwin")) == 9
    assert len(default_allowed_environment("Windows")) == 19
    union = (
        set(default_allowed_environment("Linux"))
        | set(default_allowed_environment("Darwin"))
        | set(default_allowed_environment("Windows"))
    )
    assert len(union) == 22


def test_windows_allowlist_without_systemroot_is_a_launch_failure() -> None:
    # The symptom this replaces is silence: claude.exe aborts before printing anything, so the
    # operator sees only "CLI did not succeed". The reason must name the name and the exit code.
    issue = launch_critical_env_issue(("PATH", "HOME"), "Windows")
    assert issue is not None
    assert "SystemRoot" in issue
    assert "0xC0000409" in issue
    assert "security.allowed_environment" in issue


@pytest.mark.parametrize("spelling", ["SystemRoot", "SYSTEMROOT", "systemroot"])
def test_windows_allowlist_with_systemroot_passes_in_any_case(spelling: str) -> None:
    # Windows environment names are case-insensitive, so any spelling really is forwarded — failing
    # a config that spells it differently would be a false alarm.
    assert launch_critical_env_issue(("PATH", spelling), "Windows") is None


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_posix_never_requires_systemroot(system: str) -> None:
    # The name is not needed off Windows and must not even be mentioned there.
    assert launch_critical_env_issue(("PATH", "HOME"), system) is None


def test_launch_critical_env_issue_detects_current_os(monkeypatch: pytest.MonkeyPatch) -> None:
    # No argument => the current OS, the way preflight calls it.
    monkeypatch.setattr(env_mod.platform, "system", lambda: "Windows")
    assert launch_critical_env_issue(("PATH",)) is not None
    monkeypatch.setattr(env_mod.platform, "system", lambda: "Linux")
    assert launch_critical_env_issue(("PATH",)) is None


# --- prefix patterns (security.allowed_environment) ---------------------------------------------

# A parent environment holding one toolchain's ordinary variables, a credential under a NEIGHBORING
# prefix, and something unrelated — the shape the pattern grammar exists for.
_TOOLCHAIN_PARENT = {
    "PATH": "/usr/bin",
    "DOTNET_ROOT": "/usr/share/dotnet",
    "DOTNET_NOLOGO": "1",
    "NUGET_PACKAGES": "/repo/.toolcache/nuget",
    "NUGET_API_KEY": "oy2-secret",
    "UNRELATED": "x",
}


def test_pattern_forwards_every_matching_name_and_nothing_else() -> None:
    # AC0.3.1. Strict equality, not a subset: the point of a pattern is coverage, and its risk is
    # over-coverage, so both halves are asserted at once.
    child = build_child_env(_security("PATH", "DOTNET_*"), _TOOLCHAIN_PARENT)
    assert child == {
        "PATH": "/usr/bin",
        "DOTNET_NOLOGO": "1",
        "DOTNET_ROOT": "/usr/share/dotnet",
    }


def test_secret_named_match_is_dropped_after_expansion() -> None:
    # AC0.3.2, and the reason the filter runs after expansion rather than over the config: the
    # operator wrote `NUGET_*` knowing four cache paths, not knowing the publish key shares the
    # prefix. `NUGET_PACKAGES` goes through, the key does not.
    child = build_child_env(_security("PATH", "NUGET_*"), _TOOLCHAIN_PARENT)
    assert child == {"PATH": "/usr/bin", "NUGET_PACKAGES": "/repo/.toolcache/nuget"}
    assert "NUGET_API_KEY" not in child


def test_expansion_attributes_a_dropped_name_to_its_pattern() -> None:
    # The diagnostic contract: the operator has to see WHICH pattern reached a credential, or the
    # refusal reads as "the pattern did not work" with no way to tell why.
    _, expansions = expand_allowed_environment(
        ("PATH", "NUGET_*"), _TOOLCHAIN_PARENT, system="Linux"
    )
    assert len(expansions) == 1  # a plain name produces no record
    assert expansions[0].pattern == "NUGET_*"
    assert expansions[0].kept == ("NUGET_PACKAGES",)
    assert expansions[0].dropped == ("NUGET_API_KEY",)


def test_pattern_matching_nothing_is_still_reported() -> None:
    # AC0.3.2b. The case an operator cannot otherwise distinguish from one that worked: a typo
    # (`DOTNET*` for `DOTNET_*` would match, `GO_*` here does not) or an uninstalled toolchain.
    names, expansions = expand_allowed_environment(
        ("PATH", "GO_*"), _TOOLCHAIN_PARENT, system="Linux"
    )
    assert names == ("PATH",)
    assert expansions[0] == expansions[0].__class__(pattern="GO_*", kept=(), dropped=())
    assert "nothing in this environment" in describe_expansions(expansions)[0]


def test_a_config_without_patterns_is_returned_untouched() -> None:
    # И-5: the entries pass through in place, and no expansion record is produced — so neither the
    # child environment nor the diagnostics change for a config that has no pattern.
    names, expansions = expand_allowed_environment(
        ("PATH", "HOME", "CODEX_HOME"), _TOOLCHAIN_PARENT, system="Linux"
    )
    assert names == ("PATH", "HOME", "CODEX_HOME")  # including a name absent from the parent
    assert expansions == ()


def test_windows_matches_a_pattern_case_insensitively() -> None:
    # AC0.3.4, Windows half: the parent environment is case-insensitive there, so a pattern spelled
    # in the other case really does reach the variable, and the forwarded name keeps the PARENT's
    # spelling (that is the key the lookup then has to hit).
    names, _ = expand_allowed_environment(("dotnet_*",), {"DOTNET_ROOT": "/a"}, system="Windows")
    assert names == ("DOTNET_ROOT",)


def test_posix_matches_a_pattern_case_sensitively() -> None:
    # AC0.3.4, POSIX half: the same config on Linux/macOS matches nothing, because there
    # `dotnet_root` and `DOTNET_ROOT` are two different variables.
    names, _ = expand_allowed_environment(("dotnet_*",), {"DOTNET_ROOT": "/a"}, system="Linux")
    assert names == ()


def test_expansion_detects_the_current_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    # No `system=` => the host OS, the way build_child_env and preflight call it.
    monkeypatch.setattr(env_mod.platform, "system", lambda: "Windows")
    assert expand_allowed_environment(("dotnet_*",), {"DOTNET_ROOT": "/a"})[0] == ("DOTNET_ROOT",)
    monkeypatch.setattr(env_mod.platform, "system", lambda: "Linux")
    assert expand_allowed_environment(("dotnet_*",), {"DOTNET_ROOT": "/a"})[0] == ()


def test_matched_names_are_sorted_and_the_result_is_stable() -> None:
    # `os.environ` iteration order is not reproducible, and this result is both compared in tests
    # and printed in preflight — so the order is sorted by construction, per pattern, in place.
    scrambled = {"DOTNET_ROOT": "a", "DOTNET_CLI_HOME": "b", "DOTNET_NOLOGO": "c", "PATH": "/p"}
    first, _ = expand_allowed_environment(("PATH", "DOTNET_*"), scrambled, system="Linux")
    assert first == ("PATH", "DOTNET_CLI_HOME", "DOTNET_NOLOGO", "DOTNET_ROOT")
    assert expand_allowed_environment(("PATH", "DOTNET_*"), scrambled, system="Linux")[0] == first


def test_a_name_covered_twice_is_forwarded_once() -> None:
    # An exact name plus a pattern that also matches it, and two overlapping patterns: the child
    # environment is a dict either way, but the diagnostics and the forwarded order must not
    # stutter.
    names, _ = expand_allowed_environment(
        ("DOTNET_ROOT", "DOTNET_*", "DOTNET_N*"), _TOOLCHAIN_PARENT, system="Linux"
    )
    assert names == ("DOTNET_ROOT", "DOTNET_NOLOGO")


def test_describe_expansions_prints_names_never_values() -> None:
    # The values are what a leak costs; the names are already in the operator's own config.
    _, expansions = expand_allowed_environment(
        ("DOTNET_*", "NUGET_*"), _TOOLCHAIN_PARENT, system="Linux"
    )
    described = "\n".join(describe_expansions(expansions))
    assert "DOTNET_ROOT" in described and "NUGET_API_KEY" in described
    for value in ("/usr/share/dotnet", "oy2-secret", "/repo/.toolcache/nuget"):
        assert value not in described


# --- the two "this name is missing" gates, reconciled with patterns (phase 0.1 seam) ------------


@pytest.mark.parametrize("entry", ["PATH", "PATH*", "P*"])
def test_path_counts_as_covered_when_a_pattern_can_yield_it(entry: str) -> None:
    # Coverage is decided on the config alone, so the validator's verdict stays host-independent:
    # a pattern counts because it CAN yield PATH, not because this host currently exports it.
    assert env_name_is_covered((entry,), "PATH")


@pytest.mark.parametrize("entry", ["HOME", "PATHEXT", "ATH*", "Path"])
def test_path_is_not_covered_by_a_near_miss(entry: str) -> None:
    # Case-sensitive and prefix-anchored: `PATHEXT` contains PATH as a prefix of itself, not the
    # other way round, and `Path` is forwarded on Windows but dropped on POSIX.
    assert not env_name_is_covered((entry,), "PATH")


@pytest.mark.parametrize("entry", ["SYSTEM*", "SystemRoot*", "S*"])
def test_systemroot_via_a_pattern_is_not_a_launch_failure(entry: str) -> None:
    # The phase-0.1 gate compared entries literally, so before this seam a legitimate `SYSTEM*`
    # forwarded the variable and preflight still reported it missing — a FAIL that does not exist
    # today and must not be introduced by adding patterns.
    assert launch_critical_env_issue(("PATH", entry), "Windows") is None


def test_a_pattern_that_cannot_yield_systemroot_still_fails() -> None:
    # The reconciliation must not turn the gate off: an unrelated pattern is not coverage.
    assert launch_critical_env_issue(("PATH", "DOTNET_*"), "Windows") is not None
