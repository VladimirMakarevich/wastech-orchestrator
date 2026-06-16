"""Task model: id-regex accept/reject cases, tri-state decompose, schema constants."""

from __future__ import annotations

from typing import Any

import pytest

from wastech_orchestrator.providers.base import ProviderId, Stage
from wastech_orchestrator.task.model import (
    ALLOWED_TASK_KEYS,
    REQUIRED_TASK_FIELDS,
    NormalizedTask,
    StageParams,
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
    a.stage_params[Stage.PLANNING] = StageParams(model="m")
    assert b.agents == {} and b.contacts == [] and b.stage_params == {}


def test_schema_constants() -> None:
    assert {
        "id",
        "title",
        "pr_title",
        "refined",
        "decompose",
        "auto_merge",
        "prompt_audit",
        "agents",
        "contacts",
        "model",
        "reasoning",
        "stages",
    } == ALLOWED_TASK_KEYS
    assert {"id", "title"} == REQUIRED_TASK_FIELDS
    assert REQUIRED_TASK_FIELDS <= ALLOWED_TASK_KEYS


def _task(**kwargs: Any) -> NormalizedTask:
    return NormalizedTask(id="t", title="x", description="d", **kwargs)


def test_model_for_falls_back_to_task_wide_when_no_stage_params() -> None:
    task = _task(model="task-model", reasoning="low")
    assert task.model_for(Stage.PLANNING) == "task-model"
    assert task.reasoning_for(Stage.PLANNING) == "low"


def test_model_for_uses_stage_override() -> None:
    task = _task(
        model="task-model",
        reasoning="low",
        stage_params={Stage.PLANNING: StageParams(model="opus", reasoning="high")},
    )
    assert task.model_for(Stage.PLANNING) == "opus"
    assert task.reasoning_for(Stage.PLANNING) == "high"
    # A stage without an override still gets the task-wide value.
    assert task.model_for(Stage.IMPLEMENTATION) == "task-model"
    assert task.reasoning_for(Stage.IMPLEMENTATION) == "low"


def test_model_and_reasoning_resolved_independently() -> None:
    # Only reasoning overridden for the stage → model stays task-wide.
    task = _task(
        model="task-model",
        reasoning="low",
        stage_params={Stage.REVIEW: StageParams(reasoning="high")},
    )
    assert task.model_for(Stage.REVIEW) == "task-model"
    assert task.reasoning_for(Stage.REVIEW) == "high"


def test_stage_params_none_fields_inherit_task_wide() -> None:
    task = _task(
        model="task-model",
        reasoning="low",
        stage_params={Stage.PLANNING: StageParams()},
    )
    assert task.model_for(Stage.PLANNING) == "task-model"
    assert task.reasoning_for(Stage.PLANNING) == "low"


def test_model_for_returns_none_when_nothing_set() -> None:
    task = _task()
    assert task.model_for(Stage.PLANNING) is None
    assert task.reasoning_for(Stage.PLANNING) is None
