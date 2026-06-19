"""Loader: fail-closed structural parsing and legacy-key tolerance (spec §11)."""

from __future__ import annotations

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import (
    AuditBranch,
    MergeStrategy,
)

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


def test_allow_review_skip_default_false() -> None:
    result = loads_config(_LEGACY)
    assert result.config.agents.allow_review_skip is False


def test_allow_review_skip_parsed() -> None:
    # ``allow_review_skip`` survives global-skip removal: it now gates the per-task
    # ``stages.review.enabled: false`` override (validated by the task gate).
    result = loads_config(_agents("  allow_review_skip: true\n"))
    assert result.config.agents.allow_review_skip is True


def test_legacy_skip_stages_tolerated_not_error() -> None:
    # ``agents.skip_stages`` was removed in config v10 (global stage-skip dropped for flexible
    # flows — drop the node from the flow instead). An old config still carrying it loads fail-open
    # (the key is ignored, not rejected); ``upgrade-config`` strips it.
    result = loads_config(_agents("  skip_stages: [testing, review]\n"))
    assert not hasattr(result.config.agents, "skip_stages")
    assert result.config.agents.allow_review_skip is False  # loaded cleanly, dead key ignored


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


def test_footprint_defaults_to_task_audit_branch() -> None:
    # The footprint now carries only the audit-trail policy: the task + summary are committed in the
    # repo on the task's own branch, while everything else lives under the gitignored .worc/ (§21).
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


def test_checks_timeout_loads() -> None:
    text = _LEGACY + "checks:\n  commands: ['pytest']\n  timeout_seconds: 60\n"
    result = loads_config(text)
    assert result.config.checks.commands == ("pytest",)
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
    for level in ("low", "medium", "high", "xhigh", "max"):
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


# --- auto-merge bypass (§ git.auto_merge*) ---


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


# --- prompt audit (§ top-level prompt_audit) ---


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
