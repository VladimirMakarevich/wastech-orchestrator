"""Tests for the artifact writer (spec §10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wastech_orchestrator.providers.artifacts import (
    create_attempt_dir,
    write_request_artifact,
    write_result_artifact,
)
from wastech_orchestrator.providers.base import (
    AgentRunResult,
    ErrorClass,
    NormalizedError,
    RunStatus,
)


def test_attempt_dir_layout(tmp_path: Path) -> None:
    paths = create_attempt_dir(tmp_path, "task-001", "planning", 1, "codex", node_run_id=42)
    expected = tmp_path / "logs" / "task-001" / "stages" / "planning" / "run-000042" / "1-codex"
    assert Path(paths.attempt_dir) == expected
    assert expected.is_dir()
    assert Path(paths.request_path) == expected / "request.json"
    assert Path(paths.events_path) == expected / "events.jsonl"


def test_subtask_dir_is_zero_padded(tmp_path: Path) -> None:
    paths = create_attempt_dir(
        tmp_path,
        "t",
        "implementation",
        2,
        "codex",
        node_run_id=7,
        subtask=2,
    )
    expected = (
        tmp_path / "logs" / "t" / "stages" / "implementation" / "sub-02" / "run-000007" / "2-codex"
    )
    assert Path(paths.attempt_dir) == expected
    assert expected.is_dir()


def test_never_overwrites_existing_attempt_dir(tmp_path: Path) -> None:
    create_attempt_dir(tmp_path, "task-001", "planning", 1, "codex", node_run_id=1)
    with pytest.raises(FileExistsError):
        create_attempt_dir(tmp_path, "task-001", "planning", 1, "codex", node_run_id=1)


def test_distinct_attempts_get_distinct_dirs(tmp_path: Path) -> None:
    a = create_attempt_dir(tmp_path, "task-001", "planning", 1, "codex", node_run_id=1)
    b = create_attempt_dir(tmp_path, "task-001", "planning", 2, "codex", node_run_id=1)
    assert a.attempt_dir != b.attempt_dir


def test_distinct_stage_runs_get_distinct_dirs_when_attempt_resets(tmp_path: Path) -> None:
    a = create_attempt_dir(tmp_path, "task-001", "fixing", 1, "claude", node_run_id=10)
    b = create_attempt_dir(tmp_path, "task-001", "fixing", 1, "claude", node_run_id=11)
    assert a.attempt_dir != b.attempt_dir


def test_write_request_artifact_roundtrip(tmp_path: Path) -> None:
    paths = create_attempt_dir(tmp_path, "task-001", "planning", 1, "codex", node_run_id=1)
    write_request_artifact(paths, {"task_id": "task-001", "stage": "planning", "extra_args": []})
    loaded = json.loads(Path(paths.request_path).read_text(encoding="utf-8"))
    assert loaded == {"task_id": "task-001", "stage": "planning", "extra_args": []}


def test_write_result_artifact_serializes_enums_and_error(tmp_path: Path) -> None:
    paths = create_attempt_dir(tmp_path, "task-001", "review", 1, "codex", node_run_id=1)
    result = AgentRunResult(
        status=RunStatus.FAILED,
        provider="codex",
        node_id="review",
        attempt=1,
        exit_code=0,
        started_at="2026-06-11T00:00:00+00:00",
        finished_at="2026-06-11T00:01:00+00:00",
        error=NormalizedError(ErrorClass.TASK_FAILURE, "did not satisfy the task"),
    )
    write_result_artifact(paths, result)
    loaded = json.loads(Path(paths.result_path).read_text(encoding="utf-8"))
    assert loaded["status"] == "failed"
    assert loaded["node_id"] == "review"
    assert loaded["error"]["error_class"] == "task_failure"
    assert loaded["error"]["message"] == "did not satisfy the task"
