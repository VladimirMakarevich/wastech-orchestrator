"""Validator: every reject path, including the global-primary rule (PRE.1)."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import (
    OrchestratorConfig,
    PathsConfig,
    SupervisorObserveConfig,
    SupervisorTurnConfig,
)
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.security import env_paths


@pytest.fixture
def base_config(packaged_config_text: str) -> OrchestratorConfig:
    return loads_config(packaged_config_text).config


def _with_agents(config: OrchestratorConfig, **changes: object) -> OrchestratorConfig:
    return replace(config, agents=replace(config.agents, **changes))


def _with_security(config: OrchestratorConfig, **changes: object) -> OrchestratorConfig:
    return replace(config, security=replace(config.security, **changes))


def _with_codex(config: OrchestratorConfig, **changes: object) -> OrchestratorConfig:
    providers = dict(config.agents.providers)
    providers[ProviderId.CODEX] = replace(providers[ProviderId.CODEX], **changes)
    return _with_agents(config, providers=providers)


def test_packaged_config_validates_clean(base_config: OrchestratorConfig) -> None:
    assert validate_config(base_config) == []


@pytest.mark.parametrize("value", ["read-only", "workspace-write"])
def test_legacy_codex_sandbox_value_is_rejected(
    base_config: OrchestratorConfig, value: str
) -> None:
    # The access level lives in permission_profile; a safe legacy `sandbox` is rejected.
    cfg = _with_codex(base_config, sandbox=value)
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any(".sandbox" in issue and "permission_profile" in issue for issue in exc.value.issues)


def test_danger_full_access_sandbox_passes_config_validation(
    base_config: OrchestratorConfig,
) -> None:
    # The full-access escape still loads at config time — strict_isolation gates it at preflight.
    cfg = _with_codex(base_config, sandbox="danger-full-access")
    assert validate_config(cfg) == []


def test_protected_paths_globs_validate_clean(base_config: OrchestratorConfig) -> None:
    cfg = _with_security(base_config, protected_paths=("**/*.md", "docs/**"))
    assert validate_config(cfg) == []


@pytest.mark.parametrize(
    ("strict", "disable", "expected_off"),
    [(True, False, False), (True, True, True), (False, False, True), (False, True, True)],
)
def test_read_isolation_off_formula(
    base_config: OrchestratorConfig, strict: bool, disable: bool, expected_off: bool
) -> None:
    # Effective read-isolation = disable_read_isolation OR NOT strict_isolation, defined once
    # on SecurityConfig.read_isolation_off (strict_isolation always wins toward relaxation).
    cfg = _with_security(base_config, strict_isolation=strict, disable_read_isolation=disable)
    assert cfg.security.read_isolation_off is expected_off


def test_disable_read_isolation_default_and_validates(
    base_config: OrchestratorConfig,
) -> None:
    # The packaged/shipped default is now True (read-isolation OFF out of the box); both the
    # default and an explicit False validate cleanly.
    assert base_config.security.disable_read_isolation is True
    assert validate_config(_with_security(base_config, disable_read_isolation=False)) == []


def test_loader_parses_disable_read_isolation(packaged_config_text: str) -> None:
    # Operator-config key parsed from the security block: the packaged config ships `true`
    # (read-isolation OFF), and an explicit `false` is honored (keeps read-isolation ON).
    assert loads_config(packaged_config_text).config.security.disable_read_isolation is True
    text = packaged_config_text.replace(
        "disable_read_isolation: true", "disable_read_isolation: false"
    )
    assert "disable_read_isolation: false" in text  # guard: the packaged key still exists
    cfg = loads_config(text).config
    assert cfg.security.disable_read_isolation is False
    assert cfg.security.read_isolation_off is False


def test_protected_paths_path_traversal_is_rejected(base_config: OrchestratorConfig) -> None:
    cfg = _with_security(base_config, protected_paths=("../escape",))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("protected_paths" in issue for issue in exc.value.issues)


def test_protected_paths_absolute_path_is_rejected(base_config: OrchestratorConfig) -> None:
    cfg = _with_security(base_config, protected_paths=("/etc/passwd",))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("protected_paths" in issue for issue in exc.value.issues)


def test_allowed_environment_without_path_is_rejected(base_config: OrchestratorConfig) -> None:
    # The list replaces the OS-aware default wholesale, so an operator who edits it can drop the one
    # name every child needs. Today that surfaces as "CLI did not succeed" at run time.
    cfg = _with_security(base_config, allowed_environment=("HOME", "TMPDIR"))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any(
        "security.allowed_environment" in issue and "PATH" in issue for issue in exc.value.issues
    )


def test_allowed_environment_with_path_validates_clean(base_config: OrchestratorConfig) -> None:
    # `PATH` alone is enough for the validator on every host: the Windows-only `SystemRoot` half of
    # the rule belongs to preflight, so that one config file gets the same verdict everywhere.
    assert validate_config(_with_security(base_config, allowed_environment=("PATH",))) == []


def test_allowed_environment_path_match_is_exact(base_config: OrchestratorConfig) -> None:
    # A host-independent verdict has to be the strictest reading: `Path` is forwarded on Windows
    # (its environment is case-insensitive) and silently dropped on POSIX, so it is not `PATH`.
    cfg = _with_security(base_config, allowed_environment=("Path", "HOME"))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("security.allowed_environment" in issue for issue in exc.value.issues)


@pytest.mark.parametrize("entry", ["DOTNET_*", "npm_config_*", "_*", "PATH*"])
def test_allowed_environment_accepts_a_prefix_pattern(
    base_config: OrchestratorConfig, entry: str
) -> None:
    # One trailing `*` after a valid variable name. `PATH*` is in the list deliberately: it is
    # legitimate, and the phase-0.1 "PATH is mandatory" gate used to reject it.
    assert validate_config(_with_security(base_config, allowed_environment=("PATH", entry))) == []


def test_allowed_environment_pattern_can_satisfy_the_path_requirement(
    base_config: OrchestratorConfig,
) -> None:
    # The seam with phase 0.1 in its sharpest form: `PATH*` is the ONLY entry, and it covers PATH.
    assert validate_config(_with_security(base_config, allowed_environment=("PATH*",))) == []


def test_allowed_environment_lone_star_is_rejected(base_config: OrchestratorConfig) -> None:
    # И-1/Н0.1: a lone `*` is the inversion of the mechanism (everything minus a deny-list), which
    # the environment gate deliberately does not offer — a value leaks by being present at all. The
    # message has to say that, not just "invalid syntax".
    cfg = _with_security(base_config, allowed_environment=("PATH", "*"))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("allow-list by name" in issue for issue in exc.value.issues)


@pytest.mark.parametrize("entry", ["A*B", "**", "*SUFFIX", "DOT*NET_*", "*"])
def test_allowed_environment_misplaced_star_is_rejected(
    base_config: OrchestratorConfig, entry: str
) -> None:
    # AC0.3.3. One wildcard, one position: anything else would read like a glob and silently match
    # nothing.
    cfg = _with_security(base_config, allowed_environment=("PATH", entry))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("security.allowed_environment[1]" in issue for issue in exc.value.issues)


@pytest.mark.parametrize("entry", ["SECRET_*", "TOKEN_*", "AWS_SECRET_*"])
def test_allowed_environment_secret_prefix_pattern_is_rejected(
    base_config: OrchestratorConfig, entry: str
) -> None:
    # AC0.3.2a / Т0.3.7. Such a pattern is not dangerous — the secret filter runs after expansion,
    # so it can only ever forward the empty set. It is refused because accepting it would leave the
    # operator certain the variables went through, and the message explains that mechanism.
    cfg = _with_security(base_config, allowed_environment=("PATH", entry))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any(
        "secret-bearing" in issue and "after expansion" in issue for issue in exc.value.issues
    )


def test_allowed_environment_plain_name_grammar_is_left_alone(
    base_config: OrchestratorConfig,
) -> None:
    # Deliberately NOT validated: a `*`-free entry that matches nothing has always been inert, and
    # turning that into a load error would reject configs that work today (И-5).
    cfg = _with_security(base_config, allowed_environment=("PATH", "not a name"))
    assert validate_config(cfg) == []


# The shipped template's `local_path` is the relative placeholder `./workspace/repo`, against which
# no absolute value can be compared without resolving it (which would make the verdict depend on the
# process's working directory). `worc install` writes the resolved git root, so an absolute clone
# path is what every real config holds — and what these tests use.
_CLONE = "/srv/clones/target"


def _assigning(config: OrchestratorConfig, **assigned: str) -> OrchestratorConfig:
    config = replace(config, repo=replace(config.repo, local_path=_CLONE))
    return _with_security(config, extra_environment=dict(assigned))


@pytest.mark.parametrize(
    "suffix",
    [".worc", ".worc/cache", ".git", ".git/objects", ".worc-io", "tasks", "tasks/pending"],
)
def test_assigned_value_inside_a_protected_path_is_rejected(
    base_config: OrchestratorConfig, suffix: str
) -> None:
    # The key exists to point a toolchain cache into the clone, so a wrong path is a plausible typo
    # rather than an exotic case — and the damage is what it lands on: a build filling `.worc/`
    # corrupts the run that launched it, one filling `.git/` corrupts the repository.
    clone = _CLONE
    cfg = _assigning(base_config, NUGET_PACKAGES=f"{clone}/{suffix}")
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("security.extra_environment.NUGET_PACKAGES" in issue for issue in exc.value.issues)


def test_assigned_value_that_is_a_protected_path_parent_is_rejected(
    base_config: OrchestratorConfig,
) -> None:
    # Symmetry matters: naming the clone root redirects the cache onto every protected directory
    # inside it just as effectively as naming one of them.
    cfg = _assigning(base_config, CARGO_HOME=_CLONE)
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("security.extra_environment.CARGO_HOME" in issue for issue in exc.value.issues)


def test_a_list_value_is_rejected_by_its_offending_element(
    base_config: OrchestratorConfig,
) -> None:
    # A list-shaped variable is the one that slips through an unsplit comparison: as a single string
    # it matches no protected root at all, while the element in the middle of it lands on one.
    clone = _CLONE
    cfg = _assigning(base_config, PYTHONPATH=f"{clone}/.worc{os.pathsep}{clone}/src")
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("security.extra_environment.PYTHONPATH" in issue for issue in exc.value.issues)


def test_the_rejection_names_what_was_hit_and_never_the_value(
    base_config: OrchestratorConfig,
) -> None:
    # The operator reads the value in their own config; repeating it here would give a secret that
    # landed in it against the guide's advice a second surface (a terminal, a CI log) to leak from.
    clone = _CLONE
    cfg = _assigning(base_config, NUGET_PACKAGES=f"{clone}/.worc/secret-looking-cache")
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    issue = next(i for i in exc.value.issues if "NUGET_PACKAGES" in i)
    assert "control home" in issue or "runtime home" in issue
    assert "secret-looking-cache" not in issue


@pytest.mark.parametrize("value", ["1", "C.UTF-8", "true", ""])
def test_a_value_that_is_not_a_path_is_not_examined(
    base_config: OrchestratorConfig, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Non-path values are the common case (`DOTNET_NOLOGO: "1"`), and the validator must neither
    # guess at them nor resolve them: canonicalizing here would make the verdict host-dependent.
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a non-path value was canonicalized")

    monkeypatch.setattr(env_paths, "_canonical", explode)
    assert validate_config(_assigning(base_config, DOTNET_NOLOGO=value)) == []


@pytest.mark.parametrize(
    "value",
    [
        "{clone}/.toolcache/../.worc/cache",
        "{clone}/a/b/../../.git/objects",
        "{clone}/./.worc/cache",
    ],
)
def test_a_traversal_cannot_walk_into_a_protected_path(
    base_config: OrchestratorConfig, value: str
) -> None:
    # `..` shares no component prefix with the protected path, so without collapsing it this level
    # is bypassed by one token and only the canonical check in preflight is left — which is exactly
    # the host-dependent verdict the two-level split exists to avoid.
    cfg = _assigning(base_config, NUGET_PACKAGES=value.format(clone=_CLONE))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("security.extra_environment.NUGET_PACKAGES" in issue for issue in exc.value.issues)


def test_a_traversal_that_stays_clear_is_not_refused(base_config: OrchestratorConfig) -> None:
    # Collapsing `..` must not turn every indirect path into a refusal: this one resolves to a
    # perfectly ordinary cache directory.
    cfg = _assigning(base_config, NUGET_PACKAGES=f"{_CLONE}/build/../.toolcache/nuget")
    assert validate_config(cfg) == []


def test_a_cache_beside_the_protected_paths_validates_clean(
    base_config: OrchestratorConfig,
) -> None:
    # The recipe itself has to pass: a directory of its own inside the clone is exactly right.
    clone = _CLONE
    assert (
        validate_config(_assigning(base_config, NUGET_PACKAGES=f"{clone}/.toolcache/nuget")) == []
    )


@pytest.mark.parametrize("name", ["PATH", "path", "Path"])
def test_extra_environment_cannot_assign_path(base_config: OrchestratorConfig, name: str) -> None:
    # И-3: reassigning PATH substitutes every binary the child resolves. Case-insensitive because a
    # Windows child honors `Path` exactly as it honors `PATH`.
    cfg = _with_security(base_config, extra_environment={name: "/tmp/evil"})
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("PATH cannot be assigned" in issue for issue in exc.value.issues)


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "MY_API_KEY", "npm_password"])
def test_extra_environment_rejects_secret_names(base_config: OrchestratorConfig, name: str) -> None:
    # И-2: one definition of "secret name" (is_sensitive_key), no second list of masks.
    cfg = _with_security(base_config, extra_environment={name: "x"})
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("secret-bearing" in issue for issue in exc.value.issues)


@pytest.mark.parametrize("name", ["HAS SPACE", "WITH=EQUALS", "1LEADING_DIGIT", "", "with-dash"])
def test_extra_environment_rejects_names_outside_the_grammar(
    base_config: OrchestratorConfig, name: str
) -> None:
    cfg = _with_security(base_config, extra_environment={name: "x"})
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("not a valid environment variable name" in issue for issue in exc.value.issues)


def test_extra_environment_rejects_names_differing_only_in_case(
    base_config: OrchestratorConfig,
) -> None:
    # On Windows the environment is case-insensitive, so this config has no defined meaning: which
    # value the child saw would depend on the order the mapping was read in.
    cfg = _with_security(base_config, extra_environment={"DOTNET_ROOT": "/a", "dotnet_root": "/b"})
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("only in case" in issue for issue in exc.value.issues)


def test_extra_environment_accepts_a_toolchain_recipe(base_config: OrchestratorConfig) -> None:
    # The shape the guide recommends, plus an empty value — a real assignment that forwarding cannot
    # express at all, and therefore NOT an error.
    cfg = _with_security(
        base_config,
        extra_environment={
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "NUGET_PACKAGES": "/repo/.toolcache/nuget",
            "npm_config_cache": "/repo/.toolcache/npm",
            "DOTNET_NOLOGO": "",
        },
    )
    assert validate_config(cfg) == []


def _config_text(security_block: str) -> str:
    return f"""
repo:
  url: "git@example.com:o/r.git"
agents:
  allowed: [claude]
  providers:
    claude:
      command: "claude"
      primary: true
security:
{security_block}
"""


@pytest.mark.parametrize("value", ["1", "1.10", "true", "null"])
def test_extra_environment_non_string_value_is_a_loader_error(value: str) -> None:
    # Fail-closed with a hint instead of coercing: `1` is a YAML int, `true` a bool, `1.10` a float
    # that would lose its trailing zero, `null` a None. Quoting is the fix and the message says so.
    with pytest.raises(ConfigError) as exc:
        loads_config(_config_text(f"  extra_environment:\n    DOTNET_NOLOGO: {value}"))
    assert any(
        "expected a string" in issue and "quote the value" in issue for issue in exc.value.issues
    )


def test_extra_environment_must_be_a_mapping() -> None:
    # A scalar or a list where a mapping belongs is a structural error, not something to interpret.
    with pytest.raises(ConfigError) as exc:
        loads_config(_config_text('  extra_environment: "DOTNET_NOLOGO=1"'))
    assert any("expected a mapping" in issue for issue in exc.value.issues)


def test_extra_environment_absent_key_is_an_empty_mapping(packaged_config_text: str) -> None:
    # И-5: the key is optional and its absence is not a special case — it is the empty mapping, so
    # the child environment stays exactly what forwarding alone produces.
    text = packaged_config_text.replace("  extra_environment: {}", "", 1)
    assert "  extra_environment: {}" not in text  # guard: the packaged key really was removed
    assert loads_config(text).config.security.extra_environment == {}


def test_global_primary_not_in_allowed_is_rejected(base_config: OrchestratorConfig) -> None:
    # claude is the global primary in the packaged config; shrinking allowed to codex breaks it.
    bad = _with_agents(base_config, allowed=(ProviderId.CODEX,))
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("agents.allowed" in issue for issue in exc.value.issues)


def test_no_global_primary_is_rejected(base_config: OrchestratorConfig) -> None:
    providers = {
        pid: replace(cfg, primary=False) for pid, cfg in base_config.agents.providers.items()
    }
    bad = _with_agents(base_config, providers=providers)
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("exactly one provider must set primary" in issue for issue in exc.value.issues)


def test_multiple_global_primaries_are_rejected(base_config: OrchestratorConfig) -> None:
    providers = {
        pid: replace(cfg, primary=True) for pid, cfg in base_config.agents.providers.items()
    }
    bad = _with_agents(base_config, providers=providers)
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("exactly one provider must set primary" in issue for issue in exc.value.issues)


def _codex_primary(config: OrchestratorConfig) -> OrchestratorConfig:
    """Flip the packaged global primary claude->codex (allowed stays [claude, codex])."""
    providers = {
        ProviderId.CODEX: replace(config.agents.providers[ProviderId.CODEX], primary=True),
        ProviderId.CLAUDE: replace(config.agents.providers[ProviderId.CLAUDE], primary=False),
    }
    return _with_agents(config, providers=providers)


def test_supervisor_provider_not_in_allowed_is_rejected(base_config: OrchestratorConfig) -> None:
    # An explicit supervisor.provider is validated ∈ agents.allowed, symmetric with flow nodes.
    cfg = replace(
        _with_agents(base_config, allowed=(ProviderId.CLAUDE,)),
        supervisor=replace(base_config.supervisor, provider=ProviderId.CODEX),
    )
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("supervisor.provider" in issue for issue in exc.value.issues)


def test_supervisor_reasoning_valid_for_pinned_provider(base_config: OrchestratorConfig) -> None:
    # Reasoning is checked against the pinned provider (codex), not the primary (claude).
    # `minimal` is codex-only, so it is valid here even though the primary is claude.
    cfg = replace(
        base_config,
        supervisor=replace(
            base_config.supervisor,
            provider=ProviderId.CODEX,
            finalize=SupervisorTurnConfig(reasoning="minimal"),
        ),
    )
    assert validate_config(cfg) == []


def test_supervisor_reasoning_rejected_against_pinned_provider(
    base_config: OrchestratorConfig,
) -> None:
    # Primary=codex but the supervisor is pinned to claude; `minimal` is codex-only, so it is
    # rejected against the RESOLVED supervisor provider (claude) — proving reasoning no longer
    # resolves through the global primary (which would have accepted it).
    cfg = replace(
        _codex_primary(base_config),
        supervisor=replace(
            base_config.supervisor,
            provider=ProviderId.CLAUDE,
            finalize=SupervisorTurnConfig(reasoning="minimal"),
        ),
    )
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("supervisor.finalize.reasoning" in issue for issue in exc.value.issues)


def test_inherited_supervisor_model_vendor_mismatch_warns(base_config: OrchestratorConfig) -> None:
    # A claude-looking supervisor.model with provider unset under a codex primary 400s at
    # runtime (masked by fallback). validate_config WARNS (fallback exists → not fatal), catching
    # the silent mismatch that a `ready` preflight otherwise missed.
    cfg = replace(
        _codex_primary(base_config),
        supervisor=replace(
            base_config.supervisor,
            provider=None,
            observe=SupervisorObserveConfig(),
            finalize=SupervisorTurnConfig(model="claude-opus-5"),
        ),
    )
    warnings = validate_config(cfg)
    # Named per phase: with three models to check, "supervisor.model" alone would not say which.
    assert any("supervisor.finalize.model" in w and "codex" in w for w in warnings)


def test_inherited_supervisor_unknown_model_does_not_warn(base_config: OrchestratorConfig) -> None:
    # A model with no recognized vendor prefix stays silent — no false positive (models are
    # otherwise passed through unverified).
    cfg = replace(
        _codex_primary(base_config),
        supervisor=replace(
            base_config.supervisor,
            provider=None,
            # All three phases: the vendor check runs per phase, so leaving one at a claude model
            # would (correctly) warn about that phase and mask what this test is about.
            observe=SupervisorObserveConfig(model="custom-inhouse-1"),
            finalize=SupervisorTurnConfig(model="custom-inhouse-1"),
            handoff=SupervisorTurnConfig(model="custom-inhouse-1"),
        ),
    )
    assert validate_config(cfg) == []


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
        "--yolo",
        "--ignore-rules",
    ],
)
def test_sandbox_bypass_extra_arg_is_rejected(base_config: OrchestratorConfig, flag: str) -> None:
    codex = replace(base_config.agents.providers[ProviderId.CODEX], extra_args=(flag,))
    providers = {**base_config.agents.providers, ProviderId.CODEX: codex}
    bad = _with_agents(base_config, providers=providers)
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("extra_args" in issue for issue in exc.value.issues)


@pytest.mark.parametrize(
    "extra_args",
    [
        ("--sandbox=danger-full-access",),
        ("--sandbox", "danger-full-access"),
    ],
)
def test_full_access_sandbox_extra_arg_is_not_a_config_error(
    base_config: OrchestratorConfig, extra_args: tuple[str, ...]
) -> None:
    # provider-config-cleanup #1: a full-access sandbox is no longer an absolute config-validation
    # error — it is operator-selectable and gated by the strict_isolation preflight (the absolute
    # ban is reserved for --dangerously*/--yolo/--ignore-rules). See test_isolation.py for the gate.
    codex = replace(base_config.agents.providers[ProviderId.CODEX], extra_args=extra_args)
    providers = {**base_config.agents.providers, ProviderId.CODEX: codex}
    assert validate_config(_with_agents(base_config, providers=providers)) == []


def test_claude_skip_permissions_extra_arg_is_rejected(base_config: OrchestratorConfig) -> None:
    claude = replace(
        base_config.agents.providers[ProviderId.CLAUDE],
        extra_args=("--dangerously-skip-permissions",),
    )
    providers = {**base_config.agents.providers, ProviderId.CLAUDE: claude}
    with pytest.raises(ConfigError):
        validate_config(_with_agents(base_config, providers=providers))


def test_codex_minimal_reasoning_is_valid(base_config: OrchestratorConfig) -> None:
    codex = replace(base_config.agents.providers[ProviderId.CODEX], reasoning="minimal")
    providers = {**base_config.agents.providers, ProviderId.CODEX: codex}
    assert validate_config(_with_agents(base_config, providers=providers)) == []


def test_claude_minimal_reasoning_is_rejected(base_config: OrchestratorConfig) -> None:
    claude = replace(base_config.agents.providers[ProviderId.CLAUDE], reasoning="minimal")
    providers = {**base_config.agents.providers, ProviderId.CLAUDE: claude}
    with pytest.raises(ConfigError) as exc:
        validate_config(_with_agents(base_config, providers=providers))
    assert any("provider 'claude'" in issue and "minimal" in issue for issue in exc.value.issues)


def test_supervisor_reasoning_uses_global_primary_provider(
    base_config: OrchestratorConfig,
) -> None:
    bad = replace(
        base_config,
        supervisor=replace(
            base_config.supervisor, observe=SupervisorObserveConfig(reasoning="minimal")
        ),
    )
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any(
        "supervisor.observe.reasoning" in issue and "claude" in issue for issue in exc.value.issues
    )


def test_negative_poll_interval_is_rejected(base_config: OrchestratorConfig) -> None:
    runtime = replace(base_config.orchestrator, poll_interval_seconds=-1)
    bad = replace(base_config, orchestrator=runtime)
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("poll_interval_seconds" in issue for issue in exc.value.issues)


def test_default_queue_validates_clean(base_config: OrchestratorConfig) -> None:
    assert base_config.orchestrator.queue == "default"
    assert validate_config(base_config) == []


@pytest.mark.parametrize("queue", ["", "   "])
def test_empty_or_whitespace_queue_is_rejected(base_config: OrchestratorConfig, queue: str) -> None:
    bad = replace(base_config, orchestrator=replace(base_config.orchestrator, queue=queue))
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("orchestrator.queue" in issue for issue in exc.value.issues)


def test_non_positive_telegram_timeout_is_rejected(base_config: OrchestratorConfig) -> None:
    bad = replace(
        base_config,
        telegram=replace(base_config.telegram, ask_timeout_s=0),
    )
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("telegram.ask_timeout_s" in issue for issue in exc.value.issues)


@pytest.mark.parametrize("field", ["bot_token_env", "chat_id_env"])
def test_invalid_telegram_env_name_is_rejected(base_config: OrchestratorConfig, field: str) -> None:
    bad = replace(
        base_config,
        telegram=replace(base_config.telegram, **{field: "NOT VALID"}),
    )
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any(f"telegram.{field}" in issue for issue in exc.value.issues)


def _with_tasks_dir(config: OrchestratorConfig, tasks_dir: str) -> OrchestratorConfig:
    return replace(config, paths=PathsConfig(tasks_dir=tasks_dir))


@pytest.mark.parametrize("tasks_dir", ["tasks", ".tasks", "worktasks", "config/tasks", "a/b/c"])
def test_repo_relative_tasks_dir_validates_clean(
    base_config: OrchestratorConfig, tasks_dir: str
) -> None:
    assert validate_config(_with_tasks_dir(base_config, tasks_dir)) == []


@pytest.mark.parametrize("tasks_dir", ["../escape", "/abs/tasks", "~/tasks", "a/../b", ""])
def test_unsafe_tasks_dir_is_rejected(base_config: OrchestratorConfig, tasks_dir: str) -> None:
    with pytest.raises(ConfigError) as exc:
        validate_config(_with_tasks_dir(base_config, tasks_dir))
    assert any("paths.tasks_dir" in issue for issue in exc.value.issues)


@pytest.mark.parametrize("tasks_dir", [".worc", ".worc/tasks"])
def test_tasks_dir_under_worc_home_is_rejected(
    base_config: OrchestratorConfig, tasks_dir: str
) -> None:
    with pytest.raises(ConfigError) as exc:
        validate_config(_with_tasks_dir(base_config, tasks_dir))
    assert any("paths.tasks_dir" in issue and ".worc" in issue for issue in exc.value.issues)


# --- agents.retry bounds (transient provider-failure recovery) ---


def test_retry_negative_max_attempts_is_rejected(base_config: OrchestratorConfig) -> None:
    cfg = _with_agents(base_config, retry=replace(base_config.agents.retry, max_attempts=-1))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("agents.retry.max_attempts" in issue for issue in exc.value.issues)


def test_retry_max_delay_below_base_is_rejected(base_config: OrchestratorConfig) -> None:
    cfg = _with_agents(
        base_config, retry=replace(base_config.agents.retry, base_delay_s=10.0, max_delay_s=5.0)
    )
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("agents.retry.max_delay_s" in issue for issue in exc.value.issues)


def test_retry_negative_max_blocked_is_rejected(base_config: OrchestratorConfig) -> None:
    cfg = _with_agents(base_config, retry=replace(base_config.agents.retry, max_blocked_s=-1.0))
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("agents.retry.max_blocked_s" in issue for issue in exc.value.issues)


def test_retry_disable_via_zero_attempts_validates_clean(base_config: OrchestratorConfig) -> None:
    # max_attempts=0 is the legitimate "disable transient retry" value, not a bounds violation.
    cfg = _with_agents(base_config, retry=replace(base_config.agents.retry, max_attempts=0))
    assert validate_config(cfg) == []


# --- operator confirmation gates: on requires telegram (fail-closed) -------------------------


def _with_auto_mode(config: OrchestratorConfig, **changes: object) -> OrchestratorConfig:
    return replace(
        config,
        orchestrator=replace(
            config.orchestrator, auto_mode=replace(config.orchestrator.auto_mode, **changes)
        ),
    )


def _with_claude_gate(config: OrchestratorConfig, on: bool) -> OrchestratorConfig:
    providers = dict(config.agents.providers)
    providers[ProviderId.CLAUDE] = replace(providers[ProviderId.CLAUDE], max_turns_gate=on)
    return _with_agents(config, providers=providers)


def test_confirm_next_task_without_telegram_is_rejected(base_config: OrchestratorConfig) -> None:
    # The packaged config has telegram disabled — an enabled next-task gate then has no transport.
    cfg = _with_auto_mode(base_config, confirm_next_task=True)
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("confirm_next_task" in issue for issue in exc.value.issues)


def test_max_turns_gate_without_telegram_is_rejected(base_config: OrchestratorConfig) -> None:
    cfg = _with_claude_gate(base_config, on=True)
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("max_turns_gate" in issue for issue in exc.value.issues)


def test_confirmation_gates_with_telegram_validate_clean(base_config: OrchestratorConfig) -> None:
    cfg = replace(base_config, telegram=replace(base_config.telegram, enabled=True))
    cfg = _with_auto_mode(cfg, confirm_next_task=True)
    cfg = _with_claude_gate(cfg, on=True)
    assert validate_config(cfg) == []


def test_allow_git_evidence_defaults_off_and_validates(base_config: OrchestratorConfig) -> None:
    # The git-evidence grant is opt-in: absent the key, a flow that declares git_evidence gets
    # nothing. Both the default and an explicit True validate cleanly.
    assert base_config.security.allow_git_evidence is False
    assert validate_config(_with_security(base_config, allow_git_evidence=True)) == []


def test_loader_parses_allow_git_evidence(packaged_config_text: str) -> None:
    # A key the loader does not know is a key that is silently ignored, so parse it from the
    # packaged security block explicitly: shipped `false`, and an explicit `true` honored.
    assert loads_config(packaged_config_text).config.security.allow_git_evidence is False
    text = packaged_config_text.replace("allow_git_evidence: false", "allow_git_evidence: true")
    assert "allow_git_evidence: true" in text  # guard: the packaged key still exists
    assert loads_config(text).config.security.allow_git_evidence is True


# --- supervisor.enabled: false (P3) ------------------------------------------


def test_disabled_layer_reports_its_inert_keys_as_one_warning(
    base_config: OrchestratorConfig,
) -> None:
    config = replace(base_config, supervisor=replace(base_config.supervisor, enabled=False))
    warnings = validate_config(config)
    inert = [w for w in warnings if "supervisor.enabled: false" in w]
    assert len(inert) == 1
    assert "role_file / provider / observe / finalize / handoff" in inert[0]
    assert "not validated" in inert[0]


def test_disabled_layer_warns_instead_of_refusing_a_config_it_would_reject(
    base_config: OrchestratorConfig,
) -> None:
    # The whole contract of the early return, in one pair. This config is fatal three ways with the
    # layer on — a provider outside `agents.allowed`, a traversing `role_file`, and a reasoning
    # level the resolved provider rejects. With the layer off none of the three values is ever read,
    # so refusing would only punish an operator who left the block behind.
    broken = replace(
        base_config.supervisor,
        provider=ProviderId.CODEX,
        role_file="../escape.md",
        finalize=replace(base_config.supervisor.finalize, reasoning="nonsense"),
    )
    on = replace(
        base_config,
        agents=replace(base_config.agents, allowed=(ProviderId.CLAUDE,)),
        supervisor=replace(broken, enabled=True),
    )
    with pytest.raises(ConfigError) as exc:
        validate_config(on)
    assert any("supervisor.provider" in i for i in exc.value.issues)
    assert any("role_file" in i for i in exc.value.issues)
    assert any("finalize.reasoning" in i for i in exc.value.issues)

    off = replace(on, supervisor=replace(broken, enabled=False))
    assert any("supervisor.enabled: false" in w for w in validate_config(off))  # never raises


def test_dynamic_skills_without_the_layer_warns(base_config: OrchestratorConfig) -> None:
    # Fail-open, not fatal: "only the operator's flow pins" is a correct degradation. But it is
    # silent, which is what earns the warning.
    config = replace(
        base_config,
        skills=replace(base_config.skills, dynamic=True),
        supervisor=replace(base_config.supervisor, enabled=False),
    )
    warnings = [w for w in validate_config(config) if "skills.dynamic" in w]
    assert len(warnings) == 1
    assert "supervisor.enabled is false" in warnings[0]
    assert "pinned" in warnings[0]


def test_dynamic_skills_with_the_layer_on_is_silent(base_config: OrchestratorConfig) -> None:
    config = replace(base_config, skills=replace(base_config.skills, dynamic=True))
    assert not [w for w in validate_config(config) if "skills.dynamic" in w]


def test_a_bare_skills_block_also_triggers_the_dynamic_warning() -> None:
    # `dynamic` resolves to TRUE for a present block without the key (the loader's documented
    # default), so this warning can fire for an operator who never typed the word. Pinned here so a
    # future change to that default fails loudly rather than silently muting the warning.
    text = (
        'repo:\n  url: "git@example.com:o/r.git"\n'
        "agents:\n  allowed: [claude]\n  providers:\n"
        '    claude:\n      command: "claude"\n      primary: true\n'
        "skills:\n  strict: false\n"
        "supervisor:\n  enabled: false\n"
    )
    config = loads_config(text).config
    assert config.skills.dynamic is True
    assert any("skills.dynamic" in w for w in validate_config(config))
