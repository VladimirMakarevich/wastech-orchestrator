"""Unit tests for the validation gate."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import BranchMode, OrchestratorConfig, PublishScope
from wastech_orchestrator.observability.logging import LOGGER_NAME
from wastech_orchestrator.task.model import REQUIRED_TASK_FIELDS, NodeOverride
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
    validation_reject_ids: set[str] | None = None,
) -> ValidationGate:
    store_ids = store_ids or set()
    ledger_ids = ledger_ids or set()
    recovery_ids = recovery_ids or set()
    validation_reject_ids = validation_reject_ids or set()
    return ValidationGate(
        config,
        store_has_task_id=lambda i: i in store_ids,
        ledger_has_task_id=lambda i: i in ledger_ids,
        is_recovery_rerun=lambda i: i in recovery_ids,
        ledger_only_validation_rejects=lambda i: i in validation_reject_ids,
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
    text = "---\nid: task-001\ntitle: T\nbogus: true\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.UNKNOWN_TOP_LEVEL_FIELD


def test_missing_required_title(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.MISSING_REQUIRED_FIELD
    assert result.detail == "title"


@pytest.mark.parametrize("omitted", sorted(REQUIRED_TASK_FIELDS))
def test_every_required_field_is_enforced(config: OrchestratorConfig, omitted: str) -> None:
    # The gate reads REQUIRED_TASK_FIELDS, so the constant is the whole required set — not a
    # docstring beside two hard-coded literals. Adding a key there must make this fail without it.
    frontmatter = {"id": "task-001", "title": "T"}
    del frontmatter[omitted]
    fields = "".join(f"{key}: {value}\n" for key, value in frontmatter.items())
    result = _gate(config).validate(_src(f"---\n{fields}---\n\n## Description\n\nx\n"))
    assert result.reason is ValidationReason.MISSING_REQUIRED_FIELD
    assert result.detail == omitted


def test_blank_title_is_missing_not_present(config: OrchestratorConfig) -> None:
    # Presence alone is not enough for `title`: it names the branch and the summary.
    text = '---\nid: task-001\ntitle: "   "\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.MISSING_REQUIRED_FIELD
    assert result.detail == "title"


def test_missing_description(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\n---\n\n## Description\n\n\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.MISSING_REQUIRED_FIELD
    assert result.detail == "description"


def test_task_type_parsed_into_normalized(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\ntask_type: deep_research\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.task_type == "deep_research"


def test_task_type_absent_defaults_to_none(config: OrchestratorConfig) -> None:
    # No ``task_type`` → ``None`` (the registry defaults it to ``implementation`` at dispatch).
    result = _gate(config).validate(_src(_GOOD))
    assert result.normalized is not None
    assert result.normalized.task_type is None


def test_task_type_must_be_a_string(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\ntask_type: 7\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


def test_queue_parsed_into_normalized(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nqueue: backend\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.queue == "backend"


def test_queue_absent_defaults_to_default(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_GOOD))
    assert result.normalized is not None
    assert result.normalized.queue == "default"


def test_queue_must_be_a_string(config: OrchestratorConfig) -> None:
    # Unlike priority (fail-open), queue is fail-closed: a non-string value rejects the task.
    text = "---\nid: task-001\ntitle: T\nqueue: 7\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


def test_queue_empty_string_rejected(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\nqueue: ""\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


def test_queue_whitespace_only_rejected(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\nqueue: "   "\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


def test_refined_is_now_an_unknown_field(config: OrchestratorConfig) -> None:
    # The clean task dropped ``refined`` (refinement-skip is completeness-driven). The key
    # is no longer in the allowlist → fail-closed UNKNOWN_TOP_LEVEL_FIELD.
    text = "---\nid: task-001\ntitle: T\nrefined: true\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.UNKNOWN_TOP_LEVEL_FIELD


def test_invalid_field_type_contacts(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\ncontacts: not-a-list\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


def test_depends_on_non_list_rejected(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\ndepends_on: "task-002"\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_DEPENDS_ON


def test_depends_on_non_string_element_rejected(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\ndepends_on: ["task-002", 7]\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_DEPENDS_ON


def test_depends_on_empty_string_element_rejected(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\ndepends_on: ["task-002", ""]\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_DEPENDS_ON


def test_depends_on_self_reference_rejected(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\ndepends_on: ["task-001"]\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_DEPENDS_ON


def test_depends_on_list_of_strings_passes(config: OrchestratorConfig) -> None:
    text = (
        '---\nid: task-001\ntitle: T\ndepends_on: [" task-002 ", "task-003"]\n---\n\n'
        "## Description\n\nx\n"
    )
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    # Stripped on construction; preserves order.
    assert result.normalized.depends_on == ("task-002", "task-003")


def test_depends_on_absent_defaults_empty(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_GOOD))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.depends_on == ()


@pytest.mark.parametrize("value", ["low", "mid", "high"])
def test_priority_valid_values_parsed(config: OrchestratorConfig, value: str) -> None:
    text = f"---\nid: task-001\ntitle: T\npriority: {value}\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.priority == value


def test_priority_absent_defaults_to_mid(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_GOOD))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.priority == "mid"


def test_priority_unknown_string_is_tolerated_as_mid(config: OrchestratorConfig) -> None:
    # Fail-open (unlike auto_merge): an unrecognised scheduling hint must not block a valid task.
    text = "---\nid: task-001\ntitle: T\npriority: urgent\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.priority == "mid"


def test_priority_wrong_type_is_tolerated_as_mid(config: OrchestratorConfig) -> None:
    # A wrong type would reject for auto_merge; priority instead folds to the safe default.
    text = "---\nid: task-001\ntitle: T\npriority: 3\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.priority == "mid"


def test_subtasks_non_list_rejected(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\nsubtasks: "sub/01.md"\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_SUBTASKS


def test_subtasks_empty_string_element_rejected(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\nsubtasks: ["sub/01.md", ""]\n---\n\n## Description\n\nx\n'
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_SUBTASKS


def test_subtasks_list_of_strings_passes_gate_shape(config: OrchestratorConfig) -> None:
    # The gate only checks the list shape; file/path/count/linear validation is the orchestrator's.
    text = (
        '---\nid: task-001\ntitle: T\nsubtasks: ["sub/01-a.md", "sub/02-b.md"]\n---\n\n'
        "## Description\n\nx\n"
    )
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.subtasks == ("sub/01-a.md", "sub/02-b.md")


def test_invalid_task_id(config: OrchestratorConfig) -> None:
    text = "---\nid: 'Bad Id!'\ntitle: T\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_TASK_ID


@pytest.mark.parametrize("task_id", ["con", "nul.txt", "com1", "lpt9", "task."])
def test_non_portable_task_id_rejected(config: OrchestratorConfig, task_id: str) -> None:
    # A Windows device name or a trailing dot is rejected host-independently (the id becomes a
    # directory/file component and a branch fragment), never sanitized.
    text = f"---\nid: {task_id!r}\ntitle: T\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_TASK_ID


def test_duplicate_task_id_in_store(config: OrchestratorConfig) -> None:
    result = _gate(config, store_ids={"task-001"}).validate(_src(_GOOD))
    assert result.reason is ValidationReason.DUPLICATE_TASK_ID


def test_duplicate_task_id_in_ledger(config: OrchestratorConfig) -> None:
    result = _gate(config, ledger_ids={"task-001"}).validate(_src(_GOOD))
    assert result.reason is ValidationReason.DUPLICATE_TASK_ID


def test_validation_reject_only_ledger_id_is_resubmittable(config: OrchestratorConfig) -> None:
    # An id whose only ledger trace is a validation reject (no tasks row) does NOT count as a
    # duplicate — the "rejected → fix → re-submit under the same id" loop works.
    result = _gate(config, ledger_ids={"task-001"}, validation_reject_ids={"task-001"}).validate(
        _src(_GOOD)
    )
    assert result.passed is True


def test_validation_reject_but_also_claimed_still_duplicate(config: OrchestratorConfig) -> None:
    # Regression: if a real tasks row also exists, the id is still reserved (a real attempt),
    # even though a validation-reject ledger record is present.
    result = _gate(
        config,
        store_ids={"task-001"},
        ledger_ids={"task-001"},
        validation_reject_ids={"task-001"},
    ).validate(_src(_GOOD))
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
    # Per-task provider routing is gone — a node declares its own ``provider``. The
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


def test_phase_b_acceptance_prose_without_section_needs_enrichment(
    config: OrchestratorConfig,
) -> None:
    # The substring "acceptance" in the body no longer counts as completeness — only the structured
    # ## Acceptance criteria section does. "no acceptance criteria yet" must route to refinement.
    text = (
        "---\nid: task-001\ntitle: T\n---\n\n"
        "## Description\n\nThere are no acceptance criteria yet.\n"
    )
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.completeness is Completeness.NEEDS_ENRICHMENT


def test_phase_b_complete_with_acceptance_criteria(config: OrchestratorConfig) -> None:
    # Completeness is the only input to the refinement-skip — a description + acceptance
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


def test_trust_level_override_passes_and_is_stored(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\ntrust_level: strict\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.trust_level == "strict"


def test_trust_level_absent_normalizes_to_none(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.trust_level is None


def test_trust_level_invalid_value_is_rejected(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\ntrust_level: reckless\n---\n\n## Description\n\nx.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE
    assert "trust_level" in result.detail


def test_commit_type_passes_and_is_stored(config: OrchestratorConfig) -> None:
    # The task file is the ONLY channel into its own commit subject — no node can write a commit
    # message — so this key is what makes a task's commits anything other than ``feat``.
    text = "---\nid: task-001\ntitle: T\ncommit_type: fix\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.commit_type == "fix"


def test_commit_type_absent_normalizes_to_none(config: OrchestratorConfig) -> None:
    # Absent stays ``None`` rather than the literal default, so the subject builder owns the
    # fallback and every task written before the key existed still commits as ``feat``.
    text = "---\nid: task-001\ntitle: T\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.commit_type is None


def test_commit_type_unknown_value_is_rejected(config: OrchestratorConfig) -> None:
    # Fail-closed, unlike ``priority``: the value lands in permanent history on the base branch,
    # where a typo silently deferring to ``feat`` is only discovered after the merge.
    text = "---\nid: task-001\ntitle: T\ncommit_type: feet\n---\n\n## Description\n\nx.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE
    assert "commit_type" in result.detail


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


def test_decomposition_true_passes_and_is_stored(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\ndecomposition: true\n---\n\n## Description\n\nx.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.decomposition is True


def test_decomposition_false_is_stored(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\ndecomposition: false\n---\n\n## Description\n\nx.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.decomposition is False


def test_decomposition_absent_normalizes_to_none(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.decomposition is None


def test_decomposition_non_boolean_is_rejected(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\ndecomposition: maybe\n---\n\n## Description\n\nx.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE
    assert "decomposition" in result.detail


@pytest.mark.parametrize("field", ["model", "reasoning"])
def test_model_and_reasoning_are_now_unknown_fields(config: OrchestratorConfig, field: str) -> None:
    # Model/reasoning live on the flow node, never the task → unknown top-level fields now.
    text = f"---\nid: task-001\ntitle: T\n{field}: x\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.UNKNOWN_TOP_LEVEL_FIELD


def _nodes_task(nodes_block: str) -> str:
    """Build a minimal valid task whose front matter carries the given ``nodes:`` block."""
    return f"---\nid: task-001\ntitle: T\n{nodes_block}---\n\n## Description\n\nDo it.\n"


def test_nodes_absent_is_empty(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.node_overrides == {}


def test_nodes_null_is_empty(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_nodes_task("nodes: null\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.node_overrides == {}


def test_nodes_empty_mapping_is_empty(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_nodes_task("nodes: {}\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.node_overrides == {}


def test_nodes_enabled_passes_and_is_stored(config: OrchestratorConfig) -> None:
    block = "nodes:\n  planning:\n    enabled: false\n"
    result = _gate(config).validate(_src(_nodes_task(block)))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.node_overrides == {"planning": NodeOverride(enabled=False)}


def test_nodes_node_null_inherits(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_nodes_task("nodes:\n  planning: null\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.node_overrides == {"planning": NodeOverride()}


def test_nodes_empty_block_inherits(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_nodes_task("nodes:\n  planning: {}\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.node_overrides == {"planning": NodeOverride()}


def test_nodes_arbitrary_node_id_accepted(config: OrchestratorConfig) -> None:
    # The gate checks shape only — it cannot see the task's flow, so any node id is accepted here.
    # A non-legacy id (impossible to disable under the old ``Stage`` vocabulary) passes the gate;
    # whether the node exists in the resolved flow is checked later, at flow resolution.
    block = "nodes:\n  code_review:\n    enabled: false\n"
    result = _gate(config).validate(_src(_nodes_task(block)))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.disabled_nodes() == frozenset({"code_review"})


def test_nodes_unknown_subkey_rejected(config: OrchestratorConfig) -> None:
    block = "nodes:\n  planning:\n    temperature: 1\n"
    result = _gate(config).validate(_src(_nodes_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_NODE_OVERRIDE
    assert "temperature" in result.detail


@pytest.mark.parametrize("key", ["disable_read_isolation", "strict_isolation"])
def test_nodes_security_isolation_subkey_rejected(config: OrchestratorConfig, key: str) -> None:
    # Security invariant: read-isolation (and strict_isolation) are operator-config ONLY —
    # a task node override can never set them (only enabled/model/reasoning/provider are accepted).
    block = f"nodes:\n  planning:\n    {key}: true\n"
    result = _gate(config).validate(_src(_nodes_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_NODE_OVERRIDE
    assert key in result.detail


def test_nodes_non_mapping_value_rejected(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_nodes_task("nodes:\n  planning: opus\n")))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_NODE_OVERRIDE


def test_nodes_non_mapping_top_level_rejected(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_nodes_task("nodes: opus\n")))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_NODE_OVERRIDE


# --- per-node model/reasoning/provider overrides (shape only) ----------------------------


def test_nodes_model_reasoning_provider_accepted_and_stored(config: OrchestratorConfig) -> None:
    # The gate validates shape only — provider/reasoning support is resolved at run time.
    block = (
        "nodes:\n  implementation:\n    model: claude-opus-5\n"
        "    reasoning: high\n    provider: claude\n"
    )
    result = _gate(config).validate(_src(_nodes_task(block)))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.node_overrides == {
        "implementation": NodeOverride(model="claude-opus-5", reasoning="high", provider="claude")
    }


def test_nodes_override_combined_with_enabled(config: OrchestratorConfig) -> None:
    block = "nodes:\n  review:\n    enabled: false\n    provider: codex\n"
    result = _gate(config).validate(_src(_nodes_task(block)))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.node_overrides == {
        "review": NodeOverride(enabled=False, provider="codex")
    }


def test_nodes_override_value_is_stripped(config: OrchestratorConfig) -> None:
    block = 'nodes:\n  planning:\n    model: "  opus  "\n'
    result = _gate(config).validate(_src(_nodes_task(block)))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.node_overrides["planning"].model == "opus"


@pytest.mark.parametrize("subkey", ["model", "reasoning", "provider"])
def test_nodes_override_empty_string_rejected(config: OrchestratorConfig, subkey: str) -> None:
    block = f'nodes:\n  planning:\n    {subkey}: "  "\n'
    result = _gate(config).validate(_src(_nodes_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_NODE_OVERRIDE
    assert subkey in result.detail


@pytest.mark.parametrize("subkey", ["model", "reasoning", "provider"])
def test_nodes_override_non_string_rejected(config: OrchestratorConfig, subkey: str) -> None:
    block = f"nodes:\n  planning:\n    {subkey}: 3\n"
    result = _gate(config).validate(_src(_nodes_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_NODE_OVERRIDE
    assert subkey in result.detail


# --- node-disable control (nodes.<node-id>.enabled) --------------------------------------


def test_nodes_enabled_false_accepted_and_disabled(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_nodes_task("nodes:\n  testing:\n    enabled: false\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.node_overrides["testing"] == NodeOverride(enabled=False)
    assert result.normalized.disabled_nodes() == frozenset({"testing"})


def test_nodes_enabled_true_is_not_a_disable(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_nodes_task("nodes:\n  planning:\n    enabled: true\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.disabled_nodes() == frozenset()


def test_nodes_enabled_non_bool_rejected(config: OrchestratorConfig) -> None:
    block = "nodes:\n  testing:\n    enabled: nope\n"
    result = _gate(config).validate(_src(_nodes_task(block)))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_NODE_OVERRIDE
    assert "enabled" in result.detail


def test_nodes_review_disable_needs_no_config_opt_in(config: OrchestratorConfig) -> None:
    # There is no ``review``-special-case: disabling ``review`` is accepted shape-wise like any
    # other node (no ``allow_review_skip`` gate). Skip safety is the operator's flow-authoring job.
    result = _gate(config).validate(_src(_nodes_task("nodes:\n  review:\n    enabled: false\n")))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.disabled_nodes() == frozenset({"review"})


# ---------------------------------------------------------------------------
# branch_name field
# ---------------------------------------------------------------------------


def test_pr_title_is_now_an_unknown_field(config: OrchestratorConfig) -> None:
    text = (
        '---\nid: task-001\ntitle: "Task title"\npr_title: "Custom PR title"\n'
        "---\n\n## Description\n\nDo it.\n"
    )
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.UNKNOWN_TOP_LEVEL_FIELD


def test_branch_name_override_stored(config: OrchestratorConfig) -> None:
    text = (
        '---\nid: task-001\ntitle: "Task title"\nbranch_name: "feature/ABC-123-task"\n'
        "---\n\n## Description\n\nDo it.\n"
    )
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_name == "feature/ABC-123-task"
    assert result.normalized.title == "Task title"


def test_branch_name_absent_is_none(config: OrchestratorConfig) -> None:
    result = _gate(config).validate(_src(_GOOD))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_name is None


def test_branch_name_null_is_none(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nbranch_name: null\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_name is None


def test_branch_name_empty_string_is_none(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\nbranch_name: ""\n---\n\n## Description\n\nDo it.\n'
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_name is None


def test_branch_name_whitespace_only_is_none(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\nbranch_name: "   "\n---\n\n## Description\n\nDo it.\n'
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_name is None


def test_branch_name_flag_shaped_rejected(config: OrchestratorConfig) -> None:
    text = '---\nid: task-001\ntitle: T\nbranch_name: "--inject"\n---\n\n## Description\n\nDo it.\n'
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INJECTION_SUSPECTED


def test_branch_name_wrong_type_rejected(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nbranch_name: 42\n---\n\n## Description\n\nDo it.\n"
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


def test_branch_name_list_type_rejected(config: OrchestratorConfig) -> None:
    text = (
        "---\nid: task-001\ntitle: T\nbranch_name:\n  - a\n  - b\n---\n\n## Description\n\nDo it.\n"
    )
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


@pytest.mark.parametrize(
    "branch_name",
    [
        "main",
        "feature/has space",
        "feature//x",
        "refs/heads/feature",
        "HEAD",
    ],
)
def test_branch_name_invalid_rejected(config: OrchestratorConfig, branch_name: str) -> None:
    text = (
        "---\nid: task-001\ntitle: T\n"
        f'branch_name: "{branch_name}"\n'
        "---\n\n## Description\n\nDo it.\n"
    )
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_BRANCH_NAME


def test_branch_name_json_task(config: OrchestratorConfig) -> None:
    text = json.dumps(
        {
            "id": "task-json",
            "title": "Task title",
            "branch_name": "feature/JSON-7-task",
            "description": "Do it. Acceptance: works.",
        }
    )
    result = _gate(config).validate(_src(text, ".json"))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_name == "feature/JSON-7-task"
    assert result.normalized.title == "Task title"


def test_branch_name_at_soft_cap_accepted(config: OrchestratorConfig) -> None:
    name = "feature/" + "x" * 42  # exactly 50 chars: within the soft cap, kept as-is
    assert len(name) == 50
    text = f'---\nid: task-001\ntitle: T\nbranch_name: "{name}"\n---\n\n## Description\n\nDo it.\n'
    result = _gate(config).validate(_src(text))
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_name == name


def test_branch_name_over_soft_cap_falls_back(
    config: OrchestratorConfig,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Another suite's configure_logging sets propagate=False on the package logger (state leaks
    # across tests); re-enable it so caplog — attached to root — sees the warning.
    monkeypatch.setattr(logging.getLogger(LOGGER_NAME), "propagate", True)
    name = "feature/" + "x" * 60  # > 50 chars but a valid ref (<= 255 bytes)
    text = f'---\nid: task-001\ntitle: T\nbranch_name: "{name}"\n---\n\n## Description\n\nDo it.\n'
    with caplog.at_level(logging.WARNING):
        result = _gate(config).validate(_src(text))
    # Not a hard reject — the task validates and falls back to the auto-generated branch name.
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_name is None
    assert "exceeds 50 chars" in caplog.text


def test_branch_name_over_byte_ceiling_rejected(config: OrchestratorConfig) -> None:
    name = "feature/" + "x" * 300  # > 255 bytes: the hard Git ceiling stays a hard error
    text = f'---\nid: task-001\ntitle: T\nbranch_name: "{name}"\n---\n\n## Description\n\nDo it.\n'
    result = _gate(config).validate(_src(text))
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_BRANCH_NAME


# --- branch mode / branch_ref / publish --------------------------------------------------

_BODY = "\n\n## Description\n\nDo it.\n"


def _fm(config: OrchestratorConfig, **fields: str):
    lines = "".join(f"{k}: {v}\n" for k, v in fields.items())
    text = f"---\nid: task-001\ntitle: T\n{lines}---{_BODY}"
    return _gate(config).validate(_src(text))


def test_branch_mode_valid_parses(config: OrchestratorConfig) -> None:
    result = _fm(config, branch_mode="current")
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_mode is BranchMode.CURRENT


def test_branch_mode_invalid_rejected(config: OrchestratorConfig) -> None:
    result = _fm(config, branch_mode="sideways")
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_BRANCH_MODE


def test_existing_requires_branch_ref(config: OrchestratorConfig) -> None:
    result = _fm(config, branch_mode="existing")
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_BRANCH_MODE


def test_existing_with_branch_ref_passes(config: OrchestratorConfig) -> None:
    result = _fm(config, branch_mode="existing", branch_ref="feature/keep")
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_mode is BranchMode.EXISTING
    assert result.normalized.branch_ref == "feature/keep"


def test_branch_ref_without_existing_rejected(config: OrchestratorConfig) -> None:
    # branch_ref is a contradiction unless the (effective) mode is `existing`.
    result = _fm(config, branch_ref="feature/keep")  # mode defaults to `new`
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_BRANCH_MODE


def test_branch_ref_invalid_name_rejected(config: OrchestratorConfig) -> None:
    result = _fm(config, branch_mode="existing", branch_ref='"bad ref"')
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_BRANCH_MODE


def test_depends_on_with_existing_branch_ref_warns_but_passes(
    config: OrchestratorConfig,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Combining depends_on with a pinned pre-existing branch can deadlock when that branch is a
    # dependency's own unmerged PR branch — an advisory warning, not a reject (can be legitimate).
    monkeypatch.setattr(logging.getLogger(LOGGER_NAME), "propagate", True)  # so caplog sees it
    with caplog.at_level(logging.WARNING):
        result = _fm(
            config,
            depends_on='["task-000"]',
            branch_mode="existing",
            branch_ref="feat/shared",
        )
    assert result.passed is True
    assert "depends_on" in caplog.text and "branch_ref" in caplog.text


def test_depends_on_alone_does_not_warn_about_branch(
    config: OrchestratorConfig,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging.getLogger(LOGGER_NAME), "propagate", True)
    with caplog.at_level(logging.WARNING):
        result = _fm(config, depends_on='["task-000"]')
    assert result.passed is True
    assert "branch_ref" not in caplog.text


def test_publish_valid_parses(config: OrchestratorConfig) -> None:
    result = _fm(config, publish="push")
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.publish is PublishScope.PUSH


def test_publish_invalid_rejected(config: OrchestratorConfig) -> None:
    result = _fm(config, publish="rebase")
    assert result.passed is False
    assert result.reason is ValidationReason.INVALID_BRANCH_MODE


def test_branch_name_ignored_outside_new_mode(
    config: OrchestratorConfig, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger(LOGGER_NAME), "propagate", True)
    with caplog.at_level(logging.WARNING):
        result = _fm(config, branch_mode="current", branch_name="feature/x")
    assert result.passed is True
    assert result.normalized is not None
    assert result.normalized.branch_name is None  # dropped, not a hard reject
    assert "is ignored in branch_mode" in caplog.text
