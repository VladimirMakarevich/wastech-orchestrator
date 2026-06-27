"""find_open_pr_tasks — the read-only ``worc prs`` population query."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.state_store import PublishOpRow, StateStore, TaskRow


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore.open(tmp_path / "state.db")


def _task(store: StateStore, task_id: str, status: Status = Status.DONE) -> None:
    store.insert_task(TaskRow(task_id=task_id, title=task_id, status=status))


def _op(store: StateStore, task_id: str, kind: str, status: str = "completed") -> None:
    store.record_publish_op(
        PublishOpRow(task_id=task_id, kind=kind, fingerprint="fp", status=status, result_ref="x")
    )


def test_find_open_pr_tasks_selection(store: StateStore) -> None:
    # A: completed pr, no pr_merge → open (in the list).
    _task(store, "a")
    _op(store, "a", "pr")
    # B: completed pr AND completed pr_merge → merged (excluded).
    _task(store, "b")
    _op(store, "b", "pr")
    _op(store, "b", "pr_merge")
    # C: no pr op at all → excluded.
    _task(store, "c")
    # D: a pr op that only *started* (never completed) → excluded.
    _task(store, "d")
    _op(store, "d", "pr", status="started")

    ids = {row.task_id for row in store.find_open_pr_tasks()}

    assert ids == {"a"}


def test_find_open_pr_tasks_empty(store: StateStore) -> None:
    assert store.find_open_pr_tasks() == []
