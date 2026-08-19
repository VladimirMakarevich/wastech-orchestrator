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
    template = {"security": {"denied_commands": []}}
    operator = {"security": {"denied_commands": ["git commit", "git push"]}}
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["security"]["denied_commands"] == ["git commit", "git push"]
    assert added == []  # the list is a present leaf, not merged element-wise


def test_removed_checks_keys_are_stripped() -> None:
    # v15: the whole `checks.discovery` block and the flat `checks.commands` list are removed —
    # `upgrade-config` strips both (the operator authors `checks.command_sets` by hand).
    template = {"checks": {"command_sets": {}, "timeout_seconds": 7200}}
    operator = {
        "checks": {
            "discovery": {"mode": "auto"},
            "commands": ["pytest", "ruff check ."],
            "timeout_seconds": 60,
        }
    }
    merged, _, _ = upgrade_config_mapping(template, operator)
    assert "discovery" not in merged["checks"]
    assert "commands" not in merged["checks"]
    assert merged["checks"]["timeout_seconds"] == 60  # operator's value preserved


def test_removed_validation_keys_are_stripped() -> None:
    # v35: `validation.required_fields` / `reject_unknown_fields` are removed (both were read by
    # nothing). `upgrade-config` strips them and leaves every live sibling value untouched.
    template = {"validation": {"max_task_lines": 5000}}
    operator = {
        "validation": {
            "required_fields": ["id", "title"],
            "reject_unknown_fields": True,
            "max_task_lines": 4000,
        }
    }
    merged, _, removed = upgrade_config_mapping(template, operator)
    assert "required_fields" not in merged["validation"]
    assert "reject_unknown_fields" not in merged["validation"]
    assert merged["validation"]["max_task_lines"] == 4000  # operator's value preserved
    assert "validation.required_fields" in removed
    assert "validation.reject_unknown_fields" in removed


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


def test_upgrade_swaps_deletion_exempt_paths_for_trust_level() -> None:
    # v25: an operator config carrying the removed `deletion_approval_exempt_paths` allowlist has it
    # stripped and gains `trust_level` + `protected_paths` from the packaged template, while keeping
    # its own security customizations.
    template = packaged_template_mapping()
    operator = {
        "schema_version": 24,
        "security": {"strict_isolation": False, "deletion_approval_exempt_paths": ["**/*.md"]},
    }
    merged, added, removed = upgrade_config_mapping(template, operator)
    assert "deletion_approval_exempt_paths" not in merged["security"]
    assert "security.deletion_approval_exempt_paths" in removed
    assert merged["security"]["trust_level"] == "auto"
    assert merged["security"]["protected_paths"] == []
    assert merged["security"]["strict_isolation"] is False  # operator value preserved
    assert "security.trust_level" in added
    assert "security.protected_paths" in added


def test_adds_paths_block_from_packaged_template() -> None:
    # v17 add: an operator config predating `paths` gains the block (default tasks_dir) from the
    # packaged template, while keeping its own repo customizations.
    template = packaged_template_mapping()
    operator = {"schema_version": 16, "repo": {"branch_prefix": "feat"}}
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["paths"]["tasks_dir"] == "tasks"
    assert merged["repo"]["branch_prefix"] == "feat"  # operator value preserved
    # A config predating v17 has no `paths` block at all, so the whole block is added.
    assert "paths" in added


def test_adds_orchestrator_queue_from_packaged_template() -> None:
    # v18 add: an operator config predating the queue tag gains `orchestrator.queue` (default
    # "default") from the packaged template, while keeping its own orchestrator customizations.
    template = packaged_template_mapping()
    operator = {"schema_version": 17, "orchestrator": {"poll_interval_seconds": 60}}
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["orchestrator"]["queue"] == "default"
    assert merged["orchestrator"]["poll_interval_seconds"] == 60  # operator value preserved
    assert "orchestrator.queue" in added


def test_adds_logging_block_from_packaged_template() -> None:
    # v23 add: an operator config predating the `logging` block gains it (default info/standard)
    # from the packaged template, while keeping its own customizations.
    template = packaged_template_mapping()
    operator = {"schema_version": 22, "prompt_audit": True}
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["logging"] == {
        "level": "info",
        "artifacts": "standard",
        "clean_runs_on_success": True,
    }
    assert merged["prompt_audit"] is True  # operator value preserved
    assert "logging" in added


def test_adds_run_cleanup_key_into_an_existing_logging_block() -> None:
    # v32 add: the new sub-key must reach a config that already HAS a `logging` block, or the switch
    # is undiscoverable for every existing install (the loader defaults it either way, so this is
    # about the operator being able to see and flip it).
    template = packaged_template_mapping()
    operator = {"schema_version": 31, "logging": {"level": "debug", "artifacts": "full"}}
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["logging"]["clean_runs_on_success"] is True
    assert merged["logging"]["level"] == "debug"  # operator values untouched
    assert merged["logging"]["artifacts"] == "full"
    assert "logging.clean_runs_on_success" in added


def test_adds_memory_block_from_packaged_template() -> None:
    # v24 add: an operator config predating the `memory` block gains it (enabled + bounded knobs)
    # from the packaged template, while keeping its own customizations.
    template = packaged_template_mapping()
    operator = {"schema_version": 23, "prompt_audit": True}
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["memory"]["enabled"] is True
    assert merged["memory"]["promote_min_tasks"] == 2
    assert merged["prompt_audit"] is True  # operator value preserved
    assert "memory" in added


def test_adds_supervisor_provider_from_packaged_template() -> None:
    # v27 add: an operator supervisor block predating `provider` gains it from the packaged template
    # (added under the existing block), while keeping its own role_file.
    template = packaged_template_mapping()
    operator = {"schema_version": 26, "supervisor": {"role_file": "roles/mine.md"}}
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert (
        merged["supervisor"]["provider"] == "claude"
    )  # from template (packaged primary is claude)
    assert merged["supervisor"]["role_file"] == "roles/mine.md"  # operator value preserved
    assert "supervisor.provider" in added


def test_v33_strips_flat_supervisor_model_and_adds_the_phase_blocks() -> None:
    # v33 split one model/reasoning pair into three phase blocks. The flat keys are stripped (not
    # migrated: one value, two plausible homes) and reported, while the new blocks arrive from the
    # template — so the operator sees exactly what they have to re-declare.
    template = packaged_template_mapping()
    operator = {
        "schema_version": 32,
        "supervisor": {"role_file": "roles/supervisor.md", "model": "opus", "reasoning": "xhigh"},
    }
    merged, added, removed = upgrade_config_mapping(template, operator)
    assert "model" not in merged["supervisor"] and "reasoning" not in merged["supervisor"]
    assert removed == ["supervisor.model", "supervisor.reasoning"]
    assert merged["supervisor"]["observe"]["mode"] == "events"  # from template
    assert merged["supervisor"]["finalize"]["reasoning"] == "medium"
    assert {"supervisor.observe", "supervisor.finalize", "supervisor.handoff"} <= set(added)
    assert merged["supervisor"]["role_file"] == "roles/supervisor.md"  # untouched top-level key


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


def test_new_supervisor_enabled_key_is_topped_up_from_the_template() -> None:
    # A purely additive sub-key under an existing block: the merge recurses, so the key arrives with
    # its template value and the operator's siblings are untouched.
    template = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "supervisor": {"enabled": True, "role_file": "roles/supervisor.md", "provider": "claude"},
    }
    operator = {"schema_version": 33, "supervisor": {"role_file": "roles/mine.md"}}
    merged, added, removed = upgrade_config_mapping(template, operator)
    assert "supervisor.enabled" in added
    assert merged["supervisor"]["enabled"] is True
    assert merged["supervisor"]["role_file"] == "roles/mine.md"  # operator value wins
    assert not any(key.startswith("supervisor.") for key in removed)  # nothing removed with it


def test_an_operator_who_already_switched_the_layer_off_keeps_it_off() -> None:
    template = {"schema_version": CONFIG_SCHEMA_VERSION, "supervisor": {"enabled": True}}
    operator = {"schema_version": 33, "supervisor": {"enabled": False}}
    merged, added, _removed = upgrade_config_mapping(template, operator)
    assert merged["supervisor"]["enabled"] is False
    assert "supervisor.enabled" not in added


def test_adds_extra_environment_from_packaged_template() -> None:
    # v36 add (AC0.2.6): a pre-v36 config gains `security.extra_environment` from the template with
    # nothing else in the block disturbed — the operator's trimmed allowlist above all stays theirs.
    template = packaged_template_mapping()
    operator = {
        "schema_version": 35,
        "security": {"allowed_environment": ["PATH", "HOME"], "strict_isolation": False},
    }
    merged, added, _ = upgrade_config_mapping(template, operator)
    assert merged["security"]["extra_environment"] == {}
    assert "security.extra_environment" in added
    assert merged["security"]["allowed_environment"] == ["PATH", "HOME"]  # untouched
    assert merged["security"]["strict_isolation"] is False  # untouched
    assert merged["schema_version"] == CONFIG_SCHEMA_VERSION
