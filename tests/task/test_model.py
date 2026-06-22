"""Task model: id-regex accept/reject cases, tri-state flags, the clean-task schema (PRE.3)."""

from __future__ import annotations

import pytest

from wastech_orchestrator.task.model import (
    ALLOWED_TASK_KEYS,
    REQUIRED_TASK_FIELDS,
    NodeOverride,
    NormalizedTask,
    is_valid_task_id,
)


@pytest.mark.parametrize(
    "task_id",
    ["task-001", "a", "0", "task.1_2-3", "a" * 64],
)
def test_valid_task_ids(task_id: str) -> None:
    assert is_valid_task_id(task_id)


@pytest.mark.parametrize(
    "task_id",
    [
        "",  # empty
        "Task-001",  # uppercase
        "-task",  # leading separator
        ".task",  # leading dot
        "_task",  # leading underscore
        "task 001",  # whitespace
        "task/01",  # illegal char
        "tÉst",  # non-ascii
        "a" * 65,  # too long
    ],
)
def test_invalid_task_ids(task_id: str) -> None:
    assert not is_valid_task_id(task_id)


def test_auto_merge_is_tri_state() -> None:
    assert NormalizedTask(id="t", title="x", description="d").auto_merge is None
    assert NormalizedTask(id="t", title="x", description="d", auto_merge=True).auto_merge is True
    assert NormalizedTask(id="t", title="x", description="d", auto_merge=False).auto_merge is False


def test_prompt_audit_is_tri_state() -> None:
    assert NormalizedTask(id="t", title="x", description="d").prompt_audit is None
    t = NormalizedTask(id="t", title="x", description="d", prompt_audit=True)
    assert t.prompt_audit is True


def test_default_collections_are_independent() -> None:
    a = NormalizedTask(id="a", title="A", description="d")
    b = NormalizedTask(id="b", title="B", description="d")
    a.contacts.append("ops")
    a.node_overrides["planning"] = NodeOverride(enabled=False)
    assert b.contacts == [] and b.node_overrides == {}


def test_schema_constants() -> None:
    # A clean task carries only identity/dispatch (``task_type`` selects the flow) + the two
    # sanctioned exceptions (PRE.3): ``nodes.<node-id>.enabled`` (disable) and ``auto_merge``
    # (task-wins). No provider/model/reasoning/decompose/refined.
    assert {
        "id",
        "title",
        "task_type",
        "pr_title",
        "auto_merge",
        "prompt_audit",
        "contacts",
        "nodes",
    } == ALLOWED_TASK_KEYS
    assert {"id", "title"} == REQUIRED_TASK_FIELDS
    assert REQUIRED_TASK_FIELDS <= ALLOWED_TASK_KEYS


def test_disabled_nodes_reflects_enabled_false_only() -> None:
    task = NormalizedTask(
        id="t",
        title="x",
        description="d",
        node_overrides={
            "review": NodeOverride(enabled=False),
            "testing": NodeOverride(enabled=True),
            "planning": NodeOverride(),  # unset → default (runs)
        },
    )
    assert task.disabled_nodes() == frozenset({"review"})
