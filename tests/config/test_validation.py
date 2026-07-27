"""Validator: every reject path, including the global-primary rule (PRE.1)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig, PathsConfig
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.providers.base import ProviderId


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
        supervisor=replace(base_config.supervisor, provider=ProviderId.CODEX, reasoning="minimal"),
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
        supervisor=replace(base_config.supervisor, provider=ProviderId.CLAUDE, reasoning="minimal"),
    )
    with pytest.raises(ConfigError) as exc:
        validate_config(cfg)
    assert any("supervisor.reasoning" in issue for issue in exc.value.issues)


def test_inherited_supervisor_model_vendor_mismatch_warns(base_config: OrchestratorConfig) -> None:
    # A claude-looking supervisor.model with provider unset under a codex primary 400s at
    # runtime (masked by fallback). validate_config WARNS (fallback exists → not fatal), catching
    # the silent mismatch that a `ready` preflight otherwise missed.
    cfg = replace(
        _codex_primary(base_config),
        supervisor=replace(
            base_config.supervisor, provider=None, model="claude-opus-5", reasoning=None
        ),
    )
    warnings = validate_config(cfg)
    assert any("supervisor.model" in w and "codex" in w for w in warnings)


def test_inherited_supervisor_unknown_model_does_not_warn(base_config: OrchestratorConfig) -> None:
    # A model with no recognized vendor prefix stays silent — no false positive (models are
    # otherwise passed through unverified).
    cfg = replace(
        _codex_primary(base_config),
        supervisor=replace(
            base_config.supervisor, provider=None, model="custom-inhouse-1", reasoning=None
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
    bad = replace(base_config, supervisor=replace(base_config.supervisor, reasoning="minimal"))
    with pytest.raises(ConfigError) as exc:
        validate_config(bad)
    assert any("supervisor.reasoning" in issue and "claude" in issue for issue in exc.value.issues)


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
