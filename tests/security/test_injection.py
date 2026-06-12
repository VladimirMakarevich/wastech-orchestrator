"""Front-matter injection scanner — security/injection.py (spec §19.5, §6.2).

Adversarial coverage of the argv-shaped-token scanner plus the reject-don't-sanitize guarantee for
the task id. The file-path-only *structural* guarantee is proven in
tests/security/test_no_shell_interpolation.py.
"""

from __future__ import annotations

import pytest

from wastech_orchestrator.security.injection import scan_frontmatter, scan_value
from wastech_orchestrator.task.model import is_valid_task_id


def test_clean_frontmatter_passes() -> None:
    fm = {"id": "task-1", "title": "Add a feature", "refined": True, "contacts": ["a@b.c"]}
    assert scan_frontmatter(fm) is None


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("--dangerously-skip-permissions", "value starts with '-'"),
        ("-rf", "value starts with '-'"),
        ("do; rm -rf /", "argv-shaped token"),
        ("echo `whoami`", "argv-shaped token"),
        ("a | b", "argv-shaped token"),
        ("$(touch x)", "argv-shaped token"),
        ("line1\nline2", "argv-shaped token"),
    ],
)
def test_argv_shaped_values_are_rejected(value: str, reason: str) -> None:
    finding = scan_frontmatter({"title": value})
    assert finding is not None
    assert finding.key == "title"
    assert finding.reason == reason
    assert finding.detail == f"title: {reason}"


def test_nested_mapping_is_scanned_with_dotted_key() -> None:
    finding = scan_frontmatter({"agents": {"review": "co; dex"}})
    assert finding is not None and finding.key == "agents.review"


def test_list_item_is_scanned_with_indexed_key() -> None:
    finding = scan_frontmatter({"contacts": ["ok", "$(x)"]})
    assert finding is not None and finding.key == "contacts[1]"


def test_body_like_content_in_a_value_is_only_caught_by_tokens() -> None:
    # A plain multi-word title is fine; only argv-shaped tokens trip the scanner.
    assert scan_value("title", "Fix the parser for src/foo.py") is None


@pytest.mark.parametrize(
    "task_id",
    ["Task-1", "task 1", "../etc", ".hidden", "task/sub", "task;rm", "tȯask"],
)
def test_task_id_is_reject_not_sanitize(task_id: str) -> None:
    # A value that would change under normalization is rejected outright (§19.3: reject, don't fix).
    assert is_valid_task_id(task_id) is False


@pytest.mark.parametrize("task_id", ["task-001", "t", "a.b_c-1", "task001"])
def test_valid_task_ids_accepted(task_id: str) -> None:
    assert is_valid_task_id(task_id) is True
