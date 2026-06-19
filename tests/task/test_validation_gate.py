"""Unit tests for the §19 validation gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.base import Stage
from wastech_orchestrator.task.model import StageParams
from wastech_orchestrator.task.parser import ParsedSource
from wastech_orchestrator.task.validation_gate import (
    Completeness,
    ValidationGate,
    ValidationReason,
    write_validation_report,
)


def _gate(
    config: OrchestratorConfig,
    *,
    store_ids: set[str] | None = None,
    ledger_ids: set[str] | None = None,
    recovery_ids: set[str] | None = None,
) -> ValidationGate:
    store_ids = store_ids or set()
    ledger_ids = ledger_ids or set()
    recovery_ids = recovery_ids or set()
    return ValidationGate(
        config,
        store_has_task_id=lambda i: i in store_ids,
        ledger_has_task_id=lambda i: i in ledger_ids,
        is_recovery_rerun=lambda i: i in recovery_ids,
    )


def _src(text: str, suffix: str = ".md") -> ParsedSource:
    return ParsedSource(path=f"task{suffix}", suffix=suffix, raw_bytes=text.encode("utf-8"))


_GOOD = """---
id: task-001
title: "Add a thing"
---

## Description

Do the thing properly.

## Acceptance criteria

- [ ] it works
"""


def test_valid_task_passes(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_GOOD))
    assert result.passed is True
    assert result.reason is None
    assert result.normalized is not None
    assert result.normalized.id == "task-001"
    assert result.completeness is Completeness.COMPLETE


def test_file_too_large(config: OrchestratorConfig) -> None:
    big = "x" * (config.validation.max_task_bytes + 1)
    result = _gate(config).validate(_src(big))
    assert result.reason is ValidationReason.FILE_TOO_LARGE


def test_not_utf8(config: OrchestratorConfig) -> None:
    src = ParsedSource(path="t.md", suffix=".md", raw_bytes=b"\xff\xfe bad bytes")
    result = _gate(config).validate(src)
    assert result.reason is ValidationReason.NOT_UTF8


def test_nul_byte_rejected(config: OrchestratorConfig) -> None:
    src = ParsedSource(path="t.md", suffix=".md", raw_bytes=b"---\nid: a\n\x00\n---\n")
    result = _gate(config).validate(src)
    assert result.reason is ValidationReason.BINARY_OR_CONTROL_CHARS


def test_too_long_lines(config: OrchestratorConfig) -> None:
    text = "\n".join(["line"] * (config.validation.max_task_lines + 1))
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.TOO_LONG


def test_long_single_line(config: OrchestratorConfig) -> None:
    text = "x" * (config.validation.max_line_bytes + 1)
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.TOO_LONG


def test_frontmatter_missing(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src("no frontmatter here\n"))
    assert result.reason is ValidationReason.FRONTMATTER_MISSING


def test_frontmatter_malformed_duplicate_key(config: OrchestratorConfig) -> None:
    text = "---\nid: a\nid: b\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.FRONTMATTER_MALFORMED


def test_unknown_top_level_field(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\npriority: high\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.UNKNOWN_TOP_LEVEL_FIELD


def test_missing_required_title(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.MISSING_REQUIRED_FIELD
    assert result.detail == "title"


def test_missing_description(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\n---\n\n## Description\n\n\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.MISSING_REQUIRED_FIELD
    assert result.detail == "description"


def test_refined_is_now_an_unknown_field(config: OrchestratorConfig) -> None:
    # PRE.3: the clean task dropped ``refined`` (refinement-skip is completeness-driven). The key
    # is no longer in the allowlist → fail-closed UNKNOWN_TOP_LEVEL_FIELD.
    text = "---\nid: task-001\ntitle: T\nrefined: true\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.UNKNOWN_TOP_LEVEL_FIELD


def test_invalid_field_type_contacts(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\ncontacts: not-a-list\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


def test_invalid_task_id(config: OrchestratorConfig) -> None:
    text = "---\nid: 'Bad Id!'\ntitle: T\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_TASK_ID


def test_duplicate_task_id_in_store(config: OrchestratorConfig) -> None:
    result = _gate(config, store_ids={"task-001"}).validate(_src(_GOOD))
    assert result.reason is ValidationReason.DUPLICATE_TASK_ID


def test_duplicate_task_id_in_ledger(config: OrchestratorConfig) -> None:
    result = _gate(config, ledger_ids={"task-001"}).validate(_src(_GOOD))
    assert result.reason is ValidationReason.DUPLICATE_TASK_ID


def test_recovery_rerun_is_not_duplicate(config: OrchestratorConfig) -> None:
    result = _gate(config, store_ids={"task-001"}, recovery_ids={"task-001"}).validate(_src(_GOOD))
    assert result.passed is True


def test_recovery_rerun_allowance_is_scoped_to_the_named_id(config: OrchestratorConfig) -> None:
    # A re-run set for a *different* id does not exempt this duplicate (the ``rerun`` CLI scopes
    # ``is_recovery_rerun`` to exactly the one id it is re-running).
    result = _gate(config, store_ids={"task-001"}, recovery_ids={"task-002"}).validate(_src(_GOOD))
    assert result.reason is ValidationReason.DUPLICATE_TASK_ID


def test_agents_route_override_is_now_an_unknown_field(config: OrchestratorConfig) -> None:
    # PRE.3: per-task provider routing is gone — a node declares its own ``provider``. The
    # front-matter ``agents`` key is no longer in the allowlist → fail-closed.
    text = "---\nid: task-001\ntitle: T\nagents:\n  review: codex\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.UNKNOWN_TOP_LEVEL_FIELD


def test_injection_value_starts_with_dash(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: "--dangerously-skip"\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INJECTION_SUSPECTED


def test_injection_value_with_shell_token(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: "do it; rm -rf /"\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INJECTION_SUSPECTED


def test_injection_in_contacts_list(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\ncontacts: ["-rf"]\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INJECTION_SUSPECTED


def test_phase_b_needs_enrichment_without_acceptance(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\n---\n\n## Description\n\nVague request.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.completeness is Completeness.NEEDS_ENRICHMENT


def test_phase_b_complete_with_acceptance_criteria(config: OrchestratorConfig) -> None:
    # PRE.3: completeness is the only input to the refinement-skip — a description + acceptance
    # criteria classifies COMPLETE (no ``refined`` flag).
    text = (
        "---\nid: task-001\ntitle: T\n---\n\n"
        "## Description\n\nDo it.\n\n## Acceptance criteria\n\n- works\n"
    )
    result = _gate(config).validate(_src(text))
    assert result.completeness is Completeness.COMPLETE


def test_json_task_passes(config: OrchestratorConfig) -> None:
    text = json.dumps({"id": "task-json", "title": "T", "description": "Do it. Acceptance: works."})
    result = _gate(config).validate(_src(text, ".json"))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.description.startswith("Do it")


def test_write_validation_report(config: OrchestratorConfig, tmp_path: Path) -> None:
    result = _gate(config, store_ids={"task-001"}).validate(_src(_GOOD))
    path = write_validation_report(result, "task-001", tmp_path)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["passed"] is False
    assert data["reason"] == "duplicate_task_id"
    assert path.endswith("validation_report.json")


def test_auto_merge_true_passes_and_is_stored(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nauto_merge: true\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.auto_merge is True


def test_auto_merge_false_is_stored(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nauto_merge: false\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.auto_merge is False


def test_auto_merge_absent_normalizes_to_none(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.auto_merge is None


def test_auto_merge_non_boolean_is_rejected(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nauto_merge: 3\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE
    assert "auto_merge" in result.detail


def test_prompt_audit_true_passes_and_is_stored(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nprompt_audit: true\n---\n\n## Description\n\nx.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.prompt_audit is True


def test_prompt_audit_false_is_stored(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nprompt_audit: false\n---\n\n## Description\n\nx.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.prompt_audit is False


def test_prompt_audit_absent_normalizes_to_none(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.prompt_audit is None


def test_prompt_audit_non_boolean_is_rejected(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nprompt_audit: yes-please\n---\n\n## Description\n\nx.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE
    assert "prompt_audit" in result.detail


@pytest.mark.parametrize("field", ["model", "reasoning"])
def test_model_and_reasoning_are_now_unknown_fields(
    config: OrchestratorConfig, field: str
) -> None:
    # PRE.3: model/reasoning live on the flow node, never the task → unknown top-level fields now.
    text = f"---\nid: task-001\ntitle: T\n{field}: x\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.UNKNOWN_TOP_LEVEL_FIELD


def _stages_task(stages_block: str) -> str:
    """Build a minimal valid task whose front matter carries the given ``stages:`` block."""
    return f"---\nid: task-001\ntitle: T\n{stages_block}---\n\n## Description\n\nDo it.\n"


def test_stages_absent_is_empty(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.stage_params == {}


def test_stages_null_is_empty(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_stages_task("stages: null\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.stage_params == {}


def test_stages_empty_mapping_is_empty(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_stages_task("stages: {}\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.stage_params == {}


def test_stages_enabled_passes_and_is_stored(config: OrchestratorConfig) -> None:
    block = "stages:\n  planning:\n    enabled: false\n"
    result = _gate(config).validate(_src(_stages_task(block)))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.stage_params == {Stage.PLANNING: StageParams(enabled=False)}


def test_stages_stage_null_inherits(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_stages_task("stages:\n  planning: null\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.stage_params == {Stage.PLANNING: StageParams()}


def test_stages_empty_block_inherits(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_stages_task("stages:\n  planning: {}\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.stage_params == {Stage.PLANNING: StageParams()}


def test_stages_unknown_stage_rejected(config: OrchestratorConfig) -> None:
    block = "stages:\n  nonsense:\n    enabled: false\n"
    result = _gate(config).validate(_src(_stages_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_STAGE_OVERRIDE


def test_stages_publishing_rejected(config: OrchestratorConfig) -> None:
    # ``publishing`` is the output — never skippable, so it is not a valid ``stages`` key.
    block = "stages:\n  publishing:\n    enabled: false\n"
    result = _gate(config).validate(_src(_stages_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_STAGE_OVERRIDE


def test_stages_unknown_subkey_rejected(config: OrchestratorConfig) -> None:
    block = "stages:\n  planning:\n    temperature: 1\n"
    result = _gate(config).validate(_src(_stages_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_STAGE_OVERRIDE
    assert "temperature" in result.detail


def test_stages_non_mapping_value_rejected(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_stages_task("stages:\n  planning: opus\n")))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_STAGE_OVERRIDE


def test_stages_non_mapping_top_level_rejected(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_stages_task("stages: opus\n")))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_STAGE_OVERRIDE


@pytest.mark.parametrize("subkey", ["model", "reasoning"])
def test_stages_model_reasoning_subkeys_now_unknown(
    config: OrchestratorConfig, subkey: str
) -> None:
    # PRE.3: ``enabled`` is the only valid per-stage sub-key now — model/reasoning live on the node.
    block = f"stages:\n  planning:\n    {subkey}: x\n"
    result = _gate(config).validate(_src(_stages_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_STAGE_OVERRIDE
    assert subkey in result.detail


# --- stage-skip control (stages.<stage>.enabled) -----------------------------------------


def _allow_review_skip(config: OrchestratorConfig) -> OrchestratorConfig:
    from dataclasses import replace

    return replace(config, agents=replace(config.agents, allow_review_skip=True))


def test_stages_testing_enabled_false_accepted(config: OrchestratorConfig) -> None:
    # ``testing`` is skippable (no agent) — ``enabled`` is its only valid sub-key.
    result = _gate(config).validate(_src(_stages_task("stages:\n  testing:\n    enabled: false\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.stage_params[Stage.TESTING] == StageParams(enabled=False)
    assert result.normalized.disabled_stages() == frozenset({Stage.TESTING})


def test_stages_enabled_true_is_not_a_skip(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_stages_task("stages:\n  summary:\n    enabled: true\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.disabled_stages() == frozenset()


def test_stages_implementation_enabled_rejected(config: OrchestratorConfig) -> None:
    # ``implementation`` is the core work — not skippable, so it is not a valid ``stages`` key.
    block = "stages:\n  implementation:\n    enabled: false\n"
    result = _gate(config).validate(_src(_stages_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_STAGE_OVERRIDE
    assert "implementation" in result.detail


def test_stages_enabled_non_bool_rejected(config: OrchestratorConfig) -> None:
    block = "stages:\n  testing:\n    enabled: nope\n"
    result = _gate(config).validate(_src(_stages_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_STAGE_OVERRIDE
    assert "enabled" in result.detail


def test_stages_review_skip_rejected_without_opt_in(config: OrchestratorConfig) -> None:
    # The default config has allow_review_skip: false → disabling review is rejected (fail-closed).
    result = _gate(config).validate(_src(_stages_task("stages:\n  review:\n    enabled: false\n")))
    assert result.passed is False
    assert result.reason is ValidationReason.REVIEW_SKIP_NOT_ALLOWED


def test_stages_review_skip_allowed_with_opt_in(config: OrchestratorConfig) -> None:
    result = _gate(_allow_review_skip(config)).validate(
        _src(_stages_task("stages:\n  review:\n    enabled: false\n"))
    )
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.disabled_stages() == frozenset({Stage.REVIEW})


def test_stages_review_enabled_true_never_gated(config: OrchestratorConfig) -> None:
    # Only ``enabled: false`` on review needs the opt-in; an explicit enable is always fine.
    result = _gate(config).validate(_src(_stages_task("stages:\n  review:\n    enabled: true\n")))
    assert result.passed is True


# ---------------------------------------------------------------------------
# pr_title field
# ---------------------------------------------------------------------------


def test_pr_title_override_stored(config: OrchestratorConfig) -> None:
    text = (
        '---\nid: task-001\ntitle: "Task title"\npr_title: "Custom PR title"\n'
        "---\n\n## Description\n\nDo it.\n"
    )
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.pr_title == "Custom PR title"
    assert result.normalized.title == "Task title"


def test_pr_title_absent_is_none(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_GOOD))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.pr_title is None


def test_pr_title_null_is_none(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\npr_title: null\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.pr_title is None


def test_pr_title_empty_string_is_none(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\npr_title: ""\n---\n\n## Description\n\nDo it.\n'
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.pr_title is None


def test_pr_title_whitespace_only_is_none(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\npr_title: "   "\n---\n\n## Description\n\nDo it.\n'
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.pr_title is None


def test_pr_title_flag_shaped_rejected(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\npr_title: "--inject"\n---\n\n## Description\n\nDo it.\n'
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INJECTION_SUSPECTED


def test_pr_title_wrong_type_rejected(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\npr_title: 42\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


def test_pr_title_list_type_rejected(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\npr_title:\n  - a\n  - b\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


def test_pr_title_json_task(config: OrchestratorConfig) -> None:
    text = json.dumps(
        {
            "id": "task-json",
            "title": "Task title",
            "pr_title": "Custom PR",
            "description": "Do it. Acceptance: works.",
        }
    )
    result = _gate(config).validate(_src(text, ".json"))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.pr_title == "Custom PR"
    assert result.normalized.title == "Task title"
