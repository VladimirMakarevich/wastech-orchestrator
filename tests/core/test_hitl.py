"""Pure tests for typed stage HITL validation and dangerous-diff classification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wastech_orchestrator.core.dangerous_diff import classify_dangerous_diff
from wastech_orchestrator.core.hitl import (
    StageOutputError,
    consume_pending_interactions,
    handle_from_artifact,
    parse_typed_output,
    reset_pending_interactions,
)
from wastech_orchestrator.git_manager import ChangedPath


def test_refinement_typed_output_parses_question() -> None:
    parsed = parse_typed_output(
        "human_input",
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
    parsed = parse_typed_output(
        "human_input",
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
        parse_typed_output(
            "planning",
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
        parse_typed_output(
            "human_input",
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
    }

    with pytest.raises(StageOutputError, match="positive integer"):
        parse_typed_output("planning", structured)


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


def test_multi_letter_modify_status_is_not_a_deletion() -> None:
    # Matching the status code exactly (first letter) means a porcelain-style "MM" is read as a
    # modify, not mistaken for a deletion the way a naive ``startswith("D")`` could go wrong.
    result = classify_dangerous_diff((ChangedPath(status="MM", path="src/app.py"),))
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


def _write_hitl(root: Path, task_id: str, node_id: str, status: str) -> Path:
    hitl = root / "logs" / task_id / "hitl"
    hitl.mkdir(parents=True, exist_ok=True)
    path = hitl / f"{node_id}.json"
    path.write_text(json.dumps({"status": status, "node_id": node_id}), encoding="utf-8")
    return path


def test_consume_pending_interactions_closes_only_unanswered(tmp_path: Path) -> None:
    # #11: every un-answered status — waiting AND all three AskFailures — must be closed, not just
    # waiting/transport_error (else timeout/invalid_response leak past finalize and block a resume).
    waiting = _write_hitl(tmp_path, "task-1", "refinement", "waiting")
    errored = _write_hitl(tmp_path, "task-1", "planning", "transport_error")
    timed_out = _write_hitl(tmp_path, "task-1", "review", "timeout")
    invalid = _write_hitl(tmp_path, "task-1", "fixing", "invalid_response")
    answered = _write_hitl(tmp_path, "task-1", "summary", "answered")

    closed = consume_pending_interactions(tmp_path, "task-1")

    assert set(closed) == {str(waiting), str(errored), str(timed_out), str(invalid)}
    for path in (waiting, errored, timed_out, invalid):
        assert json.loads(path.read_text())["status"] == "consumed"
    assert json.loads(answered.read_text())["status"] == "answered"  # untouched


def test_reset_pending_interactions_unlinks_all_unanswered(tmp_path: Path) -> None:
    # #11: a continue must delete every un-answered artifact (incl. timeout/invalid_response) so the
    # re-entered node asks fresh; answered/consumed artifacts stay.
    paths = {
        status: _write_hitl(tmp_path, "task-1", status, status)
        for status in ("waiting", "transport_error", "timeout", "invalid_response")
    }
    answered = _write_hitl(tmp_path, "task-1", "answered_node", "answered")

    reset = reset_pending_interactions(tmp_path, "task-1")

    assert set(reset) == {str(p) for p in paths.values()}
    assert all(not p.exists() for p in paths.values())
    assert answered.exists()  # untouched


def test_consume_pending_interactions_no_hitl_dir(tmp_path: Path) -> None:
    assert consume_pending_interactions(tmp_path, "task-missing") == []
