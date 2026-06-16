"""Loader: fail-closed structural parsing and the legacy-config migration (spec §11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.config.loader import ConfigError, load_config, loads_config
from wastech_orchestrator.config.schema import (
    AuditBranch,
    MergeStrategy,
)
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.providers.base import ProviderId, Stage

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


def test_skip_stages_default_empty() -> None:
    result = loads_config(_LEGACY)
    assert result.config.agents.skip_stages == ()
    assert result.config.agents.allow_review_skip is False


def test_skip_stages_parsed() -> None:
    result = loads_config(_agents("  skip_stages: [testing, summary]\n"))
    assert result.config.agents.skip_stages == (Stage.TESTING, Stage.SUMMARY)


def test_skip_stages_unknown_name_is_issue() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_agents("  skip_stages: [nonsense]\n"))
    assert any("skip_stages" in issue and "nonsense" in issue for issue in exc.value.issues)


def test_skip_stages_non_skippable_is_issue() -> None:
    # ``implementation`` is a real stage but cannot be skipped.
    with pytest.raises(ConfigError) as exc:
        loads_config(_agents("  skip_stages: [implementation]\n"))
    assert any("not skippable" in issue for issue in exc.value.issues)


def test_skip_stages_review_requires_opt_in() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(_agents("  skip_stages: [review]\n"))
    assert any("allow_review_skip" in issue for issue in exc.value.issues)


def test_skip_stages_review_allowed_with_opt_in() -> None:
    result = loads_config(_agents("  skip_stages: [review]\n  allow_review_skip: true\n"))
    assert result.config.agents.skip_stages == (Stage.REVIEW,)
    assert result.config.agents.allow_review_skip is True


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


def test_unknown_route_key_is_rejected() -> None:
    text = """
agents:
  routing:
    deployment:
      primary: codex
"""
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("deployment" in issue for issue in exc.value.issues)


def test_bad_enum_value_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config("git:\n  footprint:\n    audit_on_branch: weird\n")
    assert any("audit_on_branch" in issue for issue in exc.value.issues)


def test_unknown_provider_in_routing_is_rejected() -> None:
    text = """
agents:
  routing:
    planning:
      primary: gpt
"""
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("gpt" in issue for issue in exc.value.issues)


def test_all_issues_collected_not_just_first() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config("foo: 1\nbar: 2\n")
    assert len(exc.value.issues) >= 2


def test_legacy_codex_only_config_migrates_with_warning() -> None:
    result = loads_config(_LEGACY)
    assert any("migrat" in w.lower() for w in result.warnings)
    # Every agent-routed stage now has a Codex primary; the migrated config validates clean.
    assert result.config.agents.routing[Stage.PLANNING].primary is ProviderId.CODEX
    assert result.config.agents.routing[Stage.REVIEW].primary is ProviderId.CODEX
    assert validate_config(result.config) == []


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
    assert cfg.git.auto_merge_allow_per_task is False
    assert cfg.git.auto_merge_wait_for_checks is False


def test_auto_merge_keys_parse() -> None:
    text = _LEGACY + (
        "git:\n"
        "  auto_merge: true\n"
        "  auto_merge_strategy: rebase\n"
        "  auto_merge_allow_per_task: true\n"
        "  auto_merge_wait_for_checks: true\n"
    )
    cfg = loads_config(text).config
    assert cfg.git.auto_merge is True
    assert cfg.git.auto_merge_strategy is MergeStrategy.REBASE
    assert cfg.git.auto_merge_allow_per_task is True
    assert cfg.git.auto_merge_wait_for_checks is True


def test_auto_merge_strategy_invalid_value_is_rejected() -> None:
    text = _LEGACY + "git:\n  auto_merge_strategy: fast-forward\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("auto_merge_strategy" in issue for issue in exc.value.issues)


def test_auto_merge_must_be_boolean() -> None:
    text = _LEGACY + "git:\n  auto_merge: 3\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("git.auto_merge" in issue for issue in exc.value.issues)


def test_denied_commands_default_blocks_gh_pr_merge() -> None:
    # The orchestrator owns merging; agents must not be able to run `gh pr merge` themselves.
    cfg = loads_config(_LEGACY).config
    assert "gh pr merge" in cfg.security.denied_commands


# --- prompts block (backlog: prompt_template_customization) ---


def test_prompts_block_absent_defaults_are_safe() -> None:
    from wastech_orchestrator.config.schema import PromptMode

    cfg = loads_config(_LEGACY).config
    assert cfg.prompts.templates_dir == "./templates/prompts"
    assert cfg.prompts.mode is PromptMode.REPLACE  # schema v6: replace is the default


def test_prompts_block_parses() -> None:
    from wastech_orchestrator.config.schema import PromptMode

    text = _LEGACY + ("prompts:\n  templates_dir: './tpl'\n  mode: append\n")
    cfg = loads_config(text).config
    assert cfg.prompts.templates_dir == "./tpl"
    assert cfg.prompts.mode is PromptMode.APPEND


def test_prompts_legacy_overrides_and_strict_are_tolerated_with_warning() -> None:
    # Schema v6 removed overrides/strict. An old config carrying them still loads fail-open; the
    # keys are ignored and a deprecation warning is surfaced.
    text = _LEGACY + (
        "prompts:\n"
        "  templates_dir: './tpl'\n"
        "  strict: true\n"
        "  overrides:\n"
        "    implementation: 'implementation.md'\n"
    )
    result = loads_config(text)
    assert result.config.prompts.templates_dir == "./tpl"
    assert not hasattr(result.config.prompts, "overrides")
    assert not hasattr(result.config.prompts, "strict")
    assert any("prompts.overrides" in w for w in result.warnings)
    assert any("prompts.strict" in w for w in result.warnings)


def test_prompts_invalid_mode_is_rejected() -> None:
    text = _LEGACY + "prompts:\n  mode: merge\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("prompts.mode" in issue for issue in exc.value.issues)


def test_relative_templates_dir_resolves_against_config_dir(tmp_path: Path) -> None:
    # load_config anchors a relative templates_dir to the config file's directory (not the CWD).
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_LEGACY + "prompts:\n  templates_dir: './templates/prompts'\n", encoding="utf-8")
    resolved = load_config(cfg).config.prompts.templates_dir
    assert resolved == str((tmp_path / "templates" / "prompts").resolve())


def test_absolute_and_empty_templates_dir_pass_through(tmp_path: Path) -> None:
    abs_dir = str((tmp_path / "abs").resolve())
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_LEGACY + f"prompts:\n  templates_dir: '{abs_dir}'\n", encoding="utf-8")
    assert load_config(cfg).config.prompts.templates_dir == abs_dir

    cfg2 = tmp_path / "config2.yaml"
    cfg2.write_text(_LEGACY + "prompts:\n  templates_dir: ''\n", encoding="utf-8")
    assert load_config(cfg2).config.prompts.templates_dir == ""


def test_prompts_unknown_key_is_rejected() -> None:
    text = _LEGACY + "prompts:\n  preamble: 'hi'\n"
    with pytest.raises(ConfigError) as exc:
        loads_config(text)
    assert any("preamble" in issue for issue in exc.value.issues)
