"""Task model: id-regex accept/reject cases, tri-state decompose, schema constants."""

from __future__ import annotations

import pytest

from wastech_orchestrator.providers.base import ProviderId, Stage
from wastech_orchestrator.task.model import (
    ALLOWED_TASK_KEYS,
    REQUIRED_TASK_FIELDS,
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


def test_decompose_is_tri_state() -> None:
    assert NormalizedTask(id="t", title="x", description="d").decompose is None
    assert NormalizedTask(id="t", title="x", description="d", decompose=True).decompose is True
    assert NormalizedTask(id="t", title="x", description="d", decompose=False).decompose is False


def test_default_collections_are_independent() -> None:
    a = NormalizedTask(id="a", title="A", description="d")
    b = NormalizedTask(id="b", title="B", description="d")
    a.agents[Stage.PLANNING] = ProviderId.CODEX
    a.contacts.append("ops")
    assert b.agents == {} and b.contacts == []


def test_schema_constants() -> None:
    assert {"id", "title", "refined", "decompose", "agents", "contacts"} == ALLOWED_TASK_KEYS
    assert {"id", "title"} == REQUIRED_TASK_FIELDS
    assert REQUIRED_TASK_FIELDS <= ALLOWED_TASK_KEYS
