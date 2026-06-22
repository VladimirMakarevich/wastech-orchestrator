"""Unit tests for the config upgrade merge (config/upgrade.py)."""

from __future__ import annotations

from wastech_orchestrator.config.schema import CONFIG_SCHEMA_VERSION
from wastech_orchestrator.config.upgrade import (
    packaged_template_mapping,
    parse_mapping,
    render,
    upgrade_config_mapping,
)


def test_adds_missing_top_level_key() -> None:
    template = {"schema_version": CONFIG_SCHEMA_VERSION, "a": 1, "b": 2}
    operator = {"schema_version": CONFIG_SCHEMA_VERSION, "a": 1}
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["b"] == 2
    assert added == ["b"]


def test_adds_missing_subkey_under_existing_block() -> None:
    # The real shape: a new key added under an already-present agents block.
    template = {"agents": {"max_fix_cycles": 15, "max_total_fix_iterations": 30}}
    operator = {"agents": {"max_fix_cycles": 15}}
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["agents"] == {
        "max_fix_cycles": 15,
        "max_total_fix_iterations": 30,
    }
    assert added == ["agents.max_total_fix_iterations"]


def test_never_overwrites_existing_leaf() -> None:
    template = {"agents": {"max_fix_cycles": 15}}
    operator = {"agents": {"max_fix_cycles": 99}}  # operator tuned it
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["agents"]["max_fix_cycles"] == 99
    assert added == []


def test_preserves_operator_only_keys() -> None:
    template = {"agents": {"providers": {"claude": {"command": "claude"}}}}
    operator = {
        "agents": {"providers": {"claude": {"command": "claude"}, "codex": {"command": "codex"}}}
    }
    merged, _, _ = upgrade_config_mapping(template, operator)
    # The operator's extra provider (absent from the template) survives.
    assert merged["agents"]["providers"]["codex"] == {"command": "codex"}


def test_list_values_kept_verbatim() -> None:
    template = {"checks": {"commands": []}}
    operator = {"checks": {"commands": ["pytest", "ruff check ."]}}
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["checks"]["commands"] == ["pytest", "ruff check ."]
    assert added == []  # the list is a present leaf, not merged element-wise


def test_schema_version_forced_to_current() -> None:
    merged, _, _ = upgrade_config_mapping({"schema_version": CONFIG_SCHEMA_VERSION}, {"x": 1})
    assert merged["schema_version"] == CONFIG_SCHEMA_VERSION


def test_schema_version_absent_in_operator_is_added_and_stamped() -> None:
    template = {"schema_version": CONFIG_SCHEMA_VERSION, "x": 1}
    operator = {"x": 1}  # legacy config with no schema_version
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["schema_version"] == CONFIG_SCHEMA_VERSION
    assert "schema_version" in added


def test_idempotent_when_already_current() -> None:
    template = {"schema_version": CONFIG_SCHEMA_VERSION, "a": 1, "nested": {"b": 2}}
    # An operator config already carrying everything the template has → no additions, equal result.
    merged, added, _ = upgrade_config_mapping(template, dict(template))
    assert added == []
    assert merged == template


def test_packaged_template_is_complete_and_self_idempotent() -> None:
    # Upgrading the packaged template against itself adds nothing — it is the current shape.
    template = packaged_template_mapping()
    assert template  # non-empty
    merged, added, _ = upgrade_config_mapping(template, dict(template))
    assert added == []
    assert merged == template
    assert merged["schema_version"] == CONFIG_SCHEMA_VERSION


def test_strips_legacy_prompts_block() -> None:
    # config v9 removed the whole `prompts` block; upgrade-config drops it from an operator config.
    template = {"schema_version": CONFIG_SCHEMA_VERSION}
    operator = {
        "schema_version": 8,
        "prompts": {"templates_dir": "./tpl", "mode": "append"},
    }
    merged, _added, removed = upgrade_config_mapping(template, operator)
    assert "prompts" not in merged
    assert removed == ["prompts"]
    assert merged["schema_version"] == CONFIG_SCHEMA_VERSION


def test_strips_legacy_skip_stages() -> None:
    # config v10 removed the global `agents.skip_stages` list; upgrade-config drops it (per-task
    # `nodes.enabled` is the surviving, task-level disable — not in config).
    template = {"schema_version": CONFIG_SCHEMA_VERSION, "agents": {"max_fix_cycles": 15}}
    operator = {
        "schema_version": 9,
        "agents": {"skip_stages": ["testing", "review"], "max_fix_cycles": 99},
    }
    merged, _added, removed = upgrade_config_mapping(template, operator)
    assert "skip_stages" not in merged["agents"]
    assert removed == ["agents.skip_stages"]
    assert merged["agents"]["max_fix_cycles"] == 99  # operator value preserved


def test_strips_legacy_allow_review_skip() -> None:
    # config v13 removed `agents.allow_review_skip` (per-task skip is by flow node id, operator owns
    # which nodes are safe to disable); upgrade-config drops it from an operator config.
    template = {"schema_version": CONFIG_SCHEMA_VERSION, "agents": {"max_fix_cycles": 15}}
    operator = {
        "schema_version": 12,
        "agents": {"allow_review_skip": True, "max_fix_cycles": 99},
    }
    merged, _added, removed = upgrade_config_mapping(template, operator)
    assert "allow_review_skip" not in merged["agents"]
    assert removed == ["agents.allow_review_skip"]
    assert merged["agents"]["max_fix_cycles"] == 99  # operator value preserved


def test_strips_legacy_max_budget_usd() -> None:
    # config v14 removed `agents.providers.<p>.max_budget_usd` (declared/parsed but read nowhere);
    # upgrade-config drops it from both provider blocks, preserving every other operator value.
    template = {"schema_version": CONFIG_SCHEMA_VERSION, "agents": {"providers": {"claude": {}}}}
    operator = {
        "schema_version": 13,
        "agents": {
            "providers": {
                "claude": {"command": "claude", "max_budget_usd": 12.5, "max_turns": 99},
                "codex": {"command": "codex", "max_budget_usd": None},
            }
        },
    }
    merged, _added, removed = upgrade_config_mapping(template, operator)
    assert "max_budget_usd" not in merged["agents"]["providers"]["claude"]
    assert "max_budget_usd" not in merged["agents"]["providers"]["codex"]
    assert set(removed) >= {
        "agents.providers.claude.max_budget_usd",
        "agents.providers.codex.max_budget_usd",
    }
    assert merged["agents"]["providers"]["claude"]["max_turns"] == 99  # operator value preserved


def test_render_round_trips_through_parse() -> None:
    mapping = {"schema_version": CONFIG_SCHEMA_VERSION, "agents": {"max_fix_cycles": 15}}
    text = render(mapping)
    assert text.startswith("# Regenerated by")
    assert parse_mapping(text) == mapping
