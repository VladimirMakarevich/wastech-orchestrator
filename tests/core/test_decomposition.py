"""Unit tests for decomposition acceptance and artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.core.decomposition import (
    REASON_ACCEPTED,
    REASON_GATE_OFF,
    REASON_MALFORMED_SUBTASK,
    REASON_N_OUT_OF_RANGE,
    REASON_NON_LINEAR_DEPENDENCIES,
    REASON_NOT_RECOMMENDED,
    SUBTASK_COMMITTED,
    decide_decomposition,
    subtask_handoff_path,
    subtask_spec_path,
    update_subtask_index,
    write_subtask_artifacts,
)


def test_subtask_handoff_path_sits_beside_the_spec(tmp_path: Path) -> None:
    # The handoff brief is named for the successor subtask and lives beside its immutable spec under
    # logs/<task-id>/subtasks/ (local, uncommitted; never a memory tier).
    spec = subtask_spec_path(tmp_path, "task-1", 2, "second")
    handoff = subtask_handoff_path(tmp_path, "task-1", 2, "second")
    assert handoff.parent == spec.parent
    assert handoff.name == "02-second.handoff.md"
    assert handoff.parent.as_posix().endswith("logs/task-1/subtasks")


def _subtask(order: int, depends_on: list[int] | None = None) -> dict:
    return {
        "order": order,
        "title": f"Subtask {order}",
        "slug": f"sub-{order}",
        "acceptance_criteria": [f"criterion {order}"],
        "depends_on": depends_on or [],
    }


def _recommended(n: int) -> dict:
    return {"decompose": True, "subtasks": [_subtask(i) for i in range(1, n + 1)]}


def test_gate_off_runs_as_single_unit() -> None:
    decision = decide_decomposition(_recommended(3), gate_on=False, max_subtasks=8)
    assert decision.accepted is False
    assert decision.reason == REASON_GATE_OFF
    assert decision.n == 1


def test_accepts_well_formed_split() -> None:
    out = {
        "decompose": True,
        "subtasks": [_subtask(1), _subtask(2, depends_on=[1]), _subtask(3, depends_on=[1, 2])],
    }
    decision = decide_decomposition(out, gate_on=True, max_subtasks=8)
    assert decision.accepted is True
    assert decision.reason == REASON_ACCEPTED
    assert decision.n == 3
    assert [s.order for s in decision.subtasks] == [1, 2, 3]
    assert decision.subtasks[2].depends_on == (1, 2)


def test_not_recommended_when_decompose_false() -> None:
    out = {"decompose": False, "subtasks": [_subtask(1), _subtask(2)]}
    decision = decide_decomposition(out, gate_on=True, max_subtasks=8)
    assert decision.accepted is False
    assert decision.reason == REASON_NOT_RECOMMENDED


def test_not_recommended_when_no_structured_output() -> None:
    decision = decide_decomposition(None, gate_on=True, max_subtasks=8)
    assert decision.reason == REASON_NOT_RECOMMENDED


def test_n_below_two_is_rejected() -> None:
    decision = decide_decomposition(_recommended(1), gate_on=True, max_subtasks=8)
    assert decision.reason == REASON_N_OUT_OF_RANGE


def test_n_above_max_is_rejected() -> None:
    decision = decide_decomposition(_recommended(9), gate_on=True, max_subtasks=8)
    assert decision.reason == REASON_N_OUT_OF_RANGE


def test_missing_required_field_is_malformed() -> None:
    bad = _subtask(2)
    del bad["acceptance_criteria"]
    out = {"decompose": True, "subtasks": [_subtask(1), bad]}
    decision = decide_decomposition(out, gate_on=True, max_subtasks=8)
    assert decision.reason == REASON_MALFORMED_SUBTASK


def test_forward_dependency_is_rejected() -> None:
    # Subtask 1 depends on subtask 2 (a later order) — forbidden.
    out = {"decompose": True, "subtasks": [_subtask(1, depends_on=[2]), _subtask(2)]}
    decision = decide_decomposition(out, gate_on=True, max_subtasks=8)
    assert decision.reason == REASON_NON_LINEAR_DEPENDENCIES


def test_self_dependency_is_rejected() -> None:
    out = {"decompose": True, "subtasks": [_subtask(1), _subtask(2, depends_on=[2])]}
    decision = decide_decomposition(out, gate_on=True, max_subtasks=8)
    assert decision.reason == REASON_NON_LINEAR_DEPENDENCIES


def test_non_sequential_orders_rejected() -> None:
    out = {"decompose": True, "subtasks": [_subtask(1), _subtask(3)]}
    decision = decide_decomposition(out, gate_on=True, max_subtasks=8)
    assert decision.reason == REASON_NON_LINEAR_DEPENDENCIES


def test_write_and_update_subtask_artifacts(tmp_path: Path) -> None:
    decision = decide_decomposition(_recommended(2), gate_on=True, max_subtasks=8)
    write_subtask_artifacts(decision, tmp_path, "task-001")

    sub_dir = tmp_path / "logs" / "task-001" / "subtasks"
    index = json.loads((sub_dir / "index.json").read_text(encoding="utf-8"))
    assert [e["order"] for e in index] == [1, 2]
    assert all(e["commit_sha"] is None for e in index)
    assert (sub_dir / "01-sub-1.md").exists()
    assert (sub_dir / "02-sub-2.md").exists()

    update_subtask_index(tmp_path, "task-001", 1, status=SUBTASK_COMMITTED, commit_sha="abc123")
    index = json.loads((sub_dir / "index.json").read_text(encoding="utf-8"))
    assert index[0]["status"] == SUBTASK_COMMITTED
    assert index[0]["commit_sha"] == "abc123"
    assert index[1]["commit_sha"] is None


def test_subtask_spec_md_is_immutable(tmp_path: Path) -> None:
    decision = decide_decomposition(_recommended(2), gate_on=True, max_subtasks=8)
    write_subtask_artifacts(decision, tmp_path, "task-001")
    spec_path = tmp_path / "logs" / "task-001" / "subtasks" / "01-sub-1.md"
    spec_path.write_text("EDITED", encoding="utf-8")
    # A second write (e.g. on restart) must not overwrite the existing spec.
    write_subtask_artifacts(decision, tmp_path, "task-001")
    assert spec_path.read_text(encoding="utf-8") == "EDITED"


def test_rejected_decision_writes_nothing(tmp_path: Path) -> None:
    decision = decide_decomposition(_recommended(1), gate_on=True, max_subtasks=8)
    write_subtask_artifacts(decision, tmp_path, "task-001")
    assert not (tmp_path / "logs" / "task-001" / "subtasks").exists()
