"""Pure tests for typed stage HITL validation and dangerous-diff classification."""

from __future__ import annotations

import pytest

from wastech_orchestrator.core.dangerous_diff import classify_dangerous_diff
from wastech_orchestrator.core.hitl import (
    StageOutputError,
    handle_from_artifact,
    parse_typed_stage_output,
)
from wastech_orchestrator.git_manager import ChangedPath
from wastech_orchestrator.providers.base import Stage


def test_refinement_typed_output_parses_question() -> None:
    parsed = parse_typed_stage_output(
        Stage.REFINEMENT,
        {
            "content": "",
            "human_input": {
                "kind": "question",
                "question": "A or B?",
                "context": "Both are possible.",
                "risk": "clarification",
                "paths": ["src/app.py"],
            },
        },
    )
    assert parsed.human_input is not None
    assert parsed.human_input.paths == ("src/app.py",)


def test_human_input_paths_are_normalized_as_an_exact_set() -> None:
    parsed = parse_typed_stage_output(
        Stage.REFINEMENT,
        {
            "content": "",
            "human_input": {
                "kind": "approval",
                "question": "Proceed?",
                "context": "",
                "risk": "dependency",
                "paths": ["src\\b.py", "src/a.py", "src/a.py"],
            },
        },
    )

    assert parsed.human_input is not None
    assert parsed.human_input.paths == ("src/a.py", "src/b.py")


def test_planning_output_requires_exact_keys() -> None:
    with pytest.raises(StageOutputError):
        parse_typed_stage_output(
            Stage.PLANNING,
            {
                "content": "plan",
                "human_input": None,
                "decompose": False,
                "subtasks": [],
                "unexpected": True,
            },
        )


def test_human_input_rejects_traversal_path() -> None:
    with pytest.raises(StageOutputError):
        parse_typed_stage_output(
            Stage.REFINEMENT,
            {
                "content": "",
                "human_input": {
                    "kind": "approval",
                    "question": "Proceed?",
                    "context": "",
                    "risk": "other",
                    "paths": ["../secret"],
                },
            },
        )


def test_planning_output_rejects_malformed_subtask() -> None:
    structured = {
        "content": "plan",
        "human_input": None,
        "decompose": True,
        "subtasks": [
            {
                "order": True,
                "title": "Part one",
                "slug": "part-one",
                "acceptance_criteria": ["works"],
                "depends_on": [],
            }
        ],
        "skills": [],
    }

    with pytest.raises(StageOutputError, match="positive integer"):
        parse_typed_stage_output(Stage.PLANNING, structured)


def test_persisted_handle_rejects_invalid_kind() -> None:
    with pytest.raises(StageOutputError, match="malformed"):
        handle_from_artifact(
            {
                "handle": {
                    "interaction_id": "h1",
                    "kind": "anything",
                    "expires_at": 1.0,
                    "message_id": 1,
                    "update_offset": 0,
                    "delivered": True,
                }
            }
        )


def test_ordinary_diff_needs_no_approval() -> None:
    result = classify_dangerous_diff((ChangedPath(status="M", path="src/app.py"),))
    assert result is None


def test_dependency_and_deletion_diff_is_combined_risk() -> None:
    result = classify_dangerous_diff(
        (
            ChangedPath(status="D", path="src/old.py"),
            ChangedPath(status="M", path="pyproject.toml"),
        )
    )
    assert result is not None
    assert result.risk == "other"
    assert result.paths == ("pyproject.toml", "src/old.py")


def test_renamed_dependency_counts_as_deletion_and_dependency() -> None:
    result = classify_dangerous_diff(
        (
            ChangedPath(
                status="R100",
                path="requirements-dev.txt",
                previous_path="requirements.txt",
            ),
        )
    )
    assert result is not None
    assert result.risk == "other"
