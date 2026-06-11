"""Unit tests for the §19 validation gate."""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.config.schema import OrchestratorConfig
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


def test_invalid_field_type_refined(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nrefined: maybe\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_FIELD_TYPE


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


def test_invalid_route_override_unknown_stage(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nagents:\n  nonsense: claude\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_ROUTE_OVERRIDE


def test_invalid_route_override_unknown_provider(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nagents:\n  review: gpt5\n---\n\n## Description\n\nx\n"
    result = _gate(config).validate(_src(text))
    assert result.reason is ValidationReason.INVALID_ROUTE_OVERRIDE


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


def test_phase_b_complete_when_refined(config: OrchestratorConfig) -> None:
    text = "---\nid: task-001\ntitle: T\nrefined: true\n---\n\n## Description\n\nVague.\n"
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
