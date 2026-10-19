"""The per-stage rework count a non-blocking evaluator is capped by, and what resets it."""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.state_store import (
    REWORK_BUDGET_RESET,
    EvaluationRow,
    NodeRunRow,
    StateStore,
    TaskRow,
)

_TASK = "t1"


def _store(tmp_path: Path) -> StateStore:
    store = StateStore.open(tmp_path / "state.db")
    store.insert_task(TaskRow(task_id=_TASK, title="T", status=Status.VALIDATED))
    return store


def _verdict(store: StateStore, verdict: str, node_id: str = "gate") -> None:
    run_id = store.record_node_run(
        NodeRunRow(task_id=_TASK, node_id=node_id, node_kind="evaluator", status="succeeded")
    )
    store.record_evaluation(
        EvaluationRow(
            task_id=_TASK,
            node_id=node_id,
            source_node_run_id=run_id,
            kind="in_flow_verdict",
            verdict=verdict,
        )
    )


def test_a_budget_reset_starts_the_rework_count_afresh(tmp_path: Path) -> None:
    # `rerun --continue --reset-fix-budget` reset the engine's loop counters but not this count, so
    # the resumed non-blocking evaluator still had its `max_rework_per_stage` spent and accepted on
    # its first pass — a full paid run and not one fix round. The marker is what carries the grant.
    store = _store(tmp_path)
    _verdict(store, "rework")
    _verdict(store, "rework")
    assert store.count_rework_verdicts(_TASK, node_id="gate") == 2

    store.record_rework_budget_reset(_TASK)
    assert store.count_rework_verdicts(_TASK, node_id="gate") == 0

    _verdict(store, "rework")
    assert store.count_rework_verdicts(_TASK, node_id="gate") == 1
    # Append-only: the earlier verdicts and the marker all stay in the audit trail.
    kinds = [row.kind for row in store.get_evaluations(_TASK)]
    assert kinds.count("in_flow_verdict") == 3
    assert kinds.count(REWORK_BUDGET_RESET) == 1


def test_a_fresh_rerun_does_not_inherit_spent_rework_rounds(tmp_path: Path) -> None:
    # A fresh rerun deletes the attempt's node runs but may not delete evaluations; the count is
    # scoped to verdicts whose run still exists, so the new attempt starts with its full budget.
    store = _store(tmp_path)
    _verdict(store, "rework")
    _verdict(store, "accept")
    store.reset_task_for_rerun(_TASK)
    assert store.count_rework_verdicts(_TASK, node_id="gate") == 0
    assert len(store.get_evaluations(_TASK)) == 2


def test_the_reset_marker_is_invisible_to_verdict_readers(tmp_path: Path) -> None:
    # Every consumer of the evaluations table filters on `kind`; the marker must not read as a
    # verdict anywhere (gate digest, follow-ups, the finalize packet).
    store = _store(tmp_path)
    store.record_rework_budget_reset(_TASK)
    rows = [row for row in store.get_evaluations(_TASK) if row.kind == "in_flow_verdict"]
    assert rows == []
