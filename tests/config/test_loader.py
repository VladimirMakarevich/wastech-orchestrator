"""Loader: fail-closed structural parsing and legacy-key tolerance."""

from __future__ import annotations

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import (
    AuditBranch,
    BranchMode,
    MergeStrategy,
)
from wastech_orchestrator.providers.base import ProviderId

_LEGACY = """
repo:
  url: "git@example.com:o/r.git"
agents:
  allowed:
    - codex
  providers:
    codex:
      command: "codex"
"""


def test_non_mapping_root_is_rejected() -> None:
    with pytest.raises(ConfigError):
        loads_config("- a\n- b\n")


def test_empty_root_is_rejected() -> None:
    with pytest.raises(ConfigError):
        loads_config("")


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config("nonsense: 1\n")
    assert any("nonsense" in issue for issue in exc.value.issues)


def _agents(body: str) -> str:
    return (
        'repo:\n  url: "git@example.com:o/r.git"\n'
        'agents:\n  allowed: [codex]\n  providers:\n    codex:\n      command: "codex"\n' + body
    )


def test_paths_tasks_dir_defaults_when_block_absent() -> None:
    cfg = loads_config(_LEGACY).config
    assert cfg.paths.tasks_dir == "tasks"


def test_paths_tasks_dir_is_read_when_present() -> None:
    cfg = loads_config(_LEGACY + "paths:\n  tasks_dir: worktasks\n").config
    assert cfg.paths.tasks_dir == "worktasks"


def test_unknown_paths_subkey_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_LEGACY + "paths:\n  nonsense: 1\n")
    assert any("paths" in issue and "nonsense" in issue for issue in exc.value.issues)


def test_logging_defaults_when_block_absent() -> None:
    cfg = loads_config(_LEGACY).config
    assert cfg.logging.level == "info"
    assert cfg.logging.artifacts == "standard"


def test_logging_block_is_read_when_present() -> None:
    cfg = loads_config(_LEGACY + "logging:\n  level: warning\n  artifacts: minimal\n").config
    assert cfg.logging.level == "warning"
    assert cfg.logging.artifacts == "minimal"


def test_logging_invalid_level_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_LEGACY + "logging:\n  level: chatty\n")
    assert any("logging.level" in issue and "chatty" in issue for issue in exc.value.issues)


def test_logging_invalid_artifacts_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_LEGACY + "logging:\n  artifacts: everything\n")
    assert any("logging.artifacts" in issue and "everything" in issue for issue in exc.value.issues)


def test_unknown_logging_subkey_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_LEGACY + "logging:\n  nonsense: 1\n")
    assert any("logging" in issue and "nonsense" in issue for issue in exc.value.issues)


def test_legacy_skip_stages_tolerated_not_error() -> None:
    # ``agents.skip_stages`` was removed in config v10 (global stage-skip dropped for flexible
    # flows — drop the node from the flow instead). An old config still carrying it loads fail-open
    # (the key is ignored, not rejected); ``upgrade-config`` strips it.
    result = loads_config(_agents("  skip_stages: [testing, review]\n"))
    assert not hasattr(result.config.agents, "skip_stages")
    assert result.config.agents.allowed  # loaded cleanly, dead key ignored


def test_legacy_allow_review_skip_tolerated_not_error() -> None:
    # ``agents.allow_review_skip`` was removed in config v13 (per-task skip is by flow node id, and
    # the operator owns which nodes are safe to disable — no ``review``-special-case). An old config
    # still carrying it loads fail-open (key ignored, not rejected); ``upgrade-config`` strips it.
    result = loads_config(_agents("  allow_review_skip: true\n"))
    assert not hasattr(result.config.agents, "allow_review_skip")
    assert result.config.agents.allowed  # loaded cleanly, dead key ignored


def test_legacy_max_budget_usd_tolerated_not_error() -> None:
    # ``agents.providers.<p>.max_budget_usd`` was removed in config v14 (declared/parsed but read
    # nowhere). An old config still carrying it loads fail-open (the key is ignored, not rejected);
    # ``upgrade-config`` strips it.
    text = (
        'repo:\n  url: "git@example.com:o/r.git"\n'
        "agents:\n  allowed: [codex]\n  providers:\n"
        '    codex:\n      command: "codex"\n      max_budget_usd: 12.5\n'
    )
    result = loads_config(text)
    codex_cfg = result.config.agents.providers[ProviderId.CODEX]
    assert not hasattr(codex_cfg, "max_budget_usd")
    assert codex_cfg.command == "codex"  # loaded cleanly, dead key ignored


def _claude(body: str = "") -> str:
    return (
        'repo:\n  url: "git@example.com:o/r.git"\n'
        "agents:\n  allowed: [claude]\n  providers:\n"
        '    claude:\n      command: "claude"\n' + body
    )


def _claude_max_turns(text: str) -> int | None:
    return loads_config(text).config.agents.providers[ProviderId.CLAUDE].max_turns


def test_max_turns_defaults_to_400_when_absent() -> None:
    assert _claude_max_turns(_claude()) == 400


def test_max_turns_positive_integer_loads() -> None:
    assert _claude_max_turns(_claude("      max_turns: 50\n")) == 50


def test_max_turns_none_sentinel_means_no_cap() -> None:
    assert _claude_max_turns(_claude("      max_turns: none\n")) is None


def test_max_turns_max_sentinel_means_no_cap() -> None:
    assert _claude_max_turns(_claude("      max_turns: max\n")) is None


def test_max_turns_sentinel_is_case_insensitive() -> None:
    assert _claude_max_turns(_claude("      max_turns: NONE\n")) is None


def test_max_turns_yaml_null_means_no_cap() -> None:
    assert _claude_max_turns(_claude("      max_turns: null\n")) is None


def test_max_turns_zero_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_claude("      max_turns: 0\n"))
    assert any("max_turns" in issue for issue in exc.value.issues)


def test_max_turns_unknown_string_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_claude("      max_turns: forever\n"))
    assert any("max_turns" in issue for issue in exc.value.issues)


def test_auto_mode_defaults_to_false() -> None:
    result = loads_config(_LEGACY)
    assert result.config.orchestrator.auto_mode.enabled is False


def test_auto_mode_enabled_loads_true() -> None:
    text = """
orchestrator:
  auto_mode:
    enabled: true
repo:
  url: "git@example.com:o/r.git"
agents:
  allowed:
    - codex
  providers:
    codex:
      command: "codex"
"""
    result = loads_config(text)
    assert result.config.orchestrator.auto_mode.enabled is True


def test_poll_interval_defaults_to_300() -> None:
    result = loads_config(_LEGACY)
    assert result.config.orchestrator.poll_interval_seconds == 300


def test_queue_defaults_to_default() -> None:
    result = loads_config(_LEGACY)
    assert result.config.orchestrator.queue == "default"


def test_queue_loads_when_present() -> None:
    result = loads_config(_LEGACY + "orchestrator:\n  queue: backend\n")
    assert result.config.orchestrator.queue == "backend"


def test_queue_wrong_type_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_LEGACY + "orchestrator:\n  queue: 7\n")
    assert any("orchestrator.queue" in issue for issue in exc.value.issues)


def test_footprint_defaults_to_task_audit_branch() -> None:
    # The footprint now carries only the audit-trail policy: the task + summary are committed in the
    # repo on the task's own branch, while everything else lives under the gitignored .worc/.
    result = loads_config(_LEGACY)
    assert result.config.git.footprint.audit_on_branch is AuditBranch.TASK
    assert "{task_id}" in result.config.git.footprint.audit_commit_message


def test_auto_mode_enabled_must_be_boolean() -> None:
    text = """
orchestrator:
  auto_mode:
    enabled: "yes"
"""
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("orchestrator.auto_mode.enabled" in issue for issue in exc.value.issues)


def test_unknown_orchestrator_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config("orchestrator:\n  unexpected: true\n")
    assert any("unexpected" in issue for issue in exc.value.issues)


def test_checks_timeout_defaults_to_1800() -> None:
    result = loads_config(_LEGACY)
    assert result.config.checks.timeout_seconds == 7200


def test_checks_command_sets_and_timeout_load() -> None:
    text = (
        _LEGACY
        + "checks:\n"
        + "  command_sets:\n"
        + "    backend:\n"
        + '      paths: ["backend/**"]\n'
        + "      commands:\n"
        + "        - { name: bt, argv: [dotnet, test], cwd: backend/src }\n"
        + "  timeout_seconds: 60\n"
    )
    result = loads_config(text)
    sets = result.config.checks.command_sets
    assert set(sets) == {"backend"}
    assert sets["backend"].paths == ("backend/**",)
    assert sets["backend"].commands[0].argv == ("dotnet", "test")
    assert sets["backend"].commands[0].cwd == "backend/src"
    assert result.config.checks.timeout_seconds == 60


def test_checks_timeout_must_be_integer() -> None:
    text = _LEGACY + "checks:\n  timeout_seconds: fast\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("checks.timeout_seconds" in issue for issue in exc.value.issues)


def test_unknown_checks_key_is_rejected() -> None:
    text = _LEGACY + "checks:\n  retries: 3\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("retries" in issue for issue in exc.value.issues)


def test_trust_level_defaults_to_strict_and_protected_empty() -> None:
    result = loads_config(_LEGACY)
    assert result.config.security.trust_level == "strict"
    assert result.config.security.protected_paths == ()


def test_trust_level_loads() -> None:
    text = _LEGACY + "security:\n  trust_level: auto\n"
    result = loads_config(text)
    assert result.config.security.trust_level == "auto"


def test_trust_level_invalid_value_is_rejected() -> None:
    text = _LEGACY + "security:\n  trust_level: reckless\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("trust_level" in issue for issue in exc.value.issues)


def test_protected_paths_load_as_tuple() -> None:
    text = _LEGACY + 'security:\n  protected_paths: ["**/*.md", "docs/**"]\n'
    result = loads_config(text)
    assert result.config.security.protected_paths == ("**/*.md", "docs/**")


def test_protected_paths_non_string_item_is_rejected() -> None:
    text = _LEGACY + "security:\n  protected_paths: [123]\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("protected_paths" in issue for issue in exc.value.issues)


def test_legacy_deletion_exempt_paths_key_is_rejected() -> None:
    # Removed in v25 (no toleration): a config still carrying it fails as an unknown key.
    text = _LEGACY + 'security:\n  deletion_approval_exempt_paths: ["**/*.md"]\n'
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("deletion_approval_exempt_paths" in issue for issue in exc.value.issues)


def test_legacy_routing_block_is_tolerated() -> None:
    # ``agents.routing`` was removed in v11 (routing is node-based now — a node declares its own
    # ``provider``, else the global ``providers.<id>.primary``). An old config still carrying it
    # loads fail-open (the key is ignored, not rejected); ``upgrade-config`` strips it.
    result = loads_config(_agents("  routing:\n    planning: {primary: codex}\n"))
    assert not hasattr(result.config.agents, "routing")
    assert not any("migrat" in w.lower() for w in result.warnings)


def test_bad_enum_value_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config("git:\n  footprint:\n    audit_on_branch: weird\n")
    assert any("audit_on_branch" in issue for issue in exc.value.issues)


def test_all_issues_collected_not_just_first() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config("foo: 1\nbar: 2\n")
    assert len(exc.value.issues) >= 2


_WITH_CLAUDE = (
    _LEGACY
    + """
agents:
  allowed:
    - claude
  providers:
    claude:
      command: "claude"
"""
)

_PROVIDER_BASE = """
agents:
  allowed:
    - claude
    - codex
  providers:
    claude:
      command: "claude"
    codex:
      command: "codex"
"""


def test_reasoning_absent_defaults_to_none() -> None:
    result = loads_config(_PROVIDER_BASE)
    from wastech_orchestrator.providers.base import ProviderId

    assert result.config.agents.providers[ProviderId.CLAUDE].reasoning is None
    assert result.config.agents.providers[ProviderId.CODEX].reasoning is None


def test_reasoning_valid_levels_parse() -> None:
    for level in ("minimal", "low", "medium", "high", "xhigh", "max"):
        text2 = _PROVIDER_BASE.replace(
            '    claude:\n      command: "claude"',
            f'    claude:\n      command: "claude"\n      reasoning: {level}',
        )
        result = loads_config(text2)
        from wastech_orchestrator.providers.base import ProviderId

        assert result.config.agents.providers[ProviderId.CLAUDE].reasoning == level


def test_reasoning_invalid_value_is_rejected() -> None:
    text = _PROVIDER_BASE.replace(
        '    claude:\n      command: "claude"',
        '    claude:\n      command: "claude"\n      reasoning: ultra',
    )
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("reasoning" in issue for issue in exc.value.issues)
    assert any("ultra" in issue for issue in exc.value.issues)


def test_reasoning_null_parses_to_none() -> None:
    text = _PROVIDER_BASE.replace(
        '    claude:\n      command: "claude"',
        '    claude:\n      command: "claude"\n      reasoning: null',
    )
    result = loads_config(text)
    from wastech_orchestrator.providers.base import ProviderId

    assert result.config.agents.providers[ProviderId.CLAUDE].reasoning is None


# --- auto-merge bypass (git.auto_merge*) ---


def test_auto_merge_keys_default_to_safe_values() -> None:
    cfg = loads_config(_LEGACY).config
    assert cfg.git.auto_merge is False
    assert cfg.git.auto_merge_strategy is MergeStrategy.SQUASH
    assert cfg.git.auto_merge_wait_for_checks is False


def test_auto_merge_keys_parse() -> None:
    text = _LEGACY + (
        "git:\n"
        "  auto_merge: true\n"
        "  auto_merge_strategy: rebase\n"
        "  auto_merge_wait_for_checks: true\n"
    )
    cfg = loads_config(text).config
    assert cfg.git.auto_merge is True
    assert cfg.git.auto_merge_strategy is MergeStrategy.REBASE
    assert cfg.git.auto_merge_wait_for_checks is True


def test_legacy_auto_merge_allow_per_task_is_tolerated() -> None:
    # Removed in v11 (a per-task ``auto_merge`` now wins outright). An old config still carrying it
    # loads fail-open (ignored, not rejected); ``upgrade-config`` strips it.
    cfg = loads_config(_LEGACY + "git:\n  auto_merge_allow_per_task: true\n").config
    assert not hasattr(cfg.git, "auto_merge_allow_per_task")


def test_auto_merge_strategy_invalid_value_is_rejected() -> None:
    text = _LEGACY + "git:\n  auto_merge_strategy: fast-forward\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("auto_merge_strategy" in issue for issue in exc.value.issues)


# --- prompt audit (top-level prompt_audit) ---


def test_prompt_audit_defaults_to_false() -> None:
    assert loads_config(_LEGACY).config.prompt_audit is False


def test_prompt_audit_parses() -> None:
    assert loads_config(_LEGACY + "prompt_audit: true\n").config.prompt_audit is True


def test_prompt_audit_non_bool_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_LEGACY + "prompt_audit: 3\n")
    assert any("prompt_audit" in issue for issue in exc.value.issues)


def test_auto_merge_must_be_boolean() -> None:
    text = _LEGACY + "git:\n  auto_merge: 3\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("git.auto_merge" in issue for issue in exc.value.issues)


def test_denied_commands_default_blocks_gh_pr_merge() -> None:
    # The orchestrator owns merging; agents must not be able to run `gh pr merge` themselves.
    cfg = loads_config(_LEGACY).config
    assert "gh pr merge" in cfg.security.denied_commands


# --- legacy prompts block (removed in config v9) ---


def test_legacy_prompts_block_is_tolerated() -> None:
    # config v9 removed the `prompts` block (a flow node's prompt is its role_file). An old config
    # carrying one — including any sub-keys — still loads fail-open: the whole block is ignored and
    # never stored on the schema (`upgrade-config` strips it).
    text = _LEGACY + "prompts:\n  templates_dir: './tpl'\n  mode: append\n  preamble: 'hi'\n"
    cfg = loads_config(text).config
    assert not hasattr(cfg, "prompts")


# --- agents.retry (transient provider-failure recovery, config v20) ---


def test_retry_defaults_when_block_absent() -> None:
    # The whole `agents.retry` block is optional → safe defaults (back-compat for old configs).
    cfg = loads_config(_agents("")).config
    retry = cfg.agents.retry
    assert (retry.max_attempts, retry.base_delay_s, retry.max_delay_s, retry.max_blocked_s) == (
        2,
        2.0,
        30.0,
        3600.0,
    )


def test_retry_block_is_read_when_present() -> None:
    body = (
        "  retry:\n    max_attempts: 5\n    base_delay_s: 1.0\n"
        "    max_delay_s: 60.0\n    max_blocked_s: 120.0\n"
    )
    cfg = loads_config(_agents(body)).config
    retry = cfg.agents.retry
    assert (retry.max_attempts, retry.base_delay_s, retry.max_delay_s, retry.max_blocked_s) == (
        5,
        1.0,
        60.0,
        120.0,
    )


def test_retry_wrong_types_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_agents('  retry:\n    max_attempts: "x"\n    base_delay_s: "y"\n'))
    issues = exc.value.issues
    assert any("agents.retry.max_attempts" in i for i in issues)
    assert any("agents.retry.base_delay_s" in i for i in issues)


def test_unknown_retry_subkey_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_agents("  retry:\n    nonsense: 1\n"))
    assert any("agents.retry" in i and "nonsense" in i for i in exc.value.issues)


# --- repo.branch_mode (branch-mode ADR) ---

_REPO_WITH_MODE = (
    'repo:\n  url: "git@example.com:o/r.git"\n  branch_mode: {mode}\n'
    "agents:\n  allowed: [codex]\n  providers:\n    codex:\n      command: \"codex\"\n"
)


def test_repo_branch_mode_defaults_to_new() -> None:
    cfg = loads_config(_LEGACY).config
    assert cfg.repo.branch_mode is BranchMode.NEW


def test_repo_branch_mode_is_read_when_present() -> None:
    cfg = loads_config(_REPO_WITH_MODE.format(mode="current")).config
    assert cfg.repo.branch_mode is BranchMode.CURRENT


def test_repo_branch_mode_invalid_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_REPO_WITH_MODE.format(mode="sideways"))
    assert any("branch_mode" in issue for issue in exc.value.issues)
