"""Unit tests for the §9 SQLite State Store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from wastech_orchestrator.core.loop_control import LoopCounters
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.state_store import (
    ArtifactRow,
    CheckRunRow,
    ProviderAttemptRow,
    PublishOpRow,
    StageRunRow,
    StateStore,
    SubtaskRow,
    TaskRow,
)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore.open(tmp_path / "state.db")


def _new_task(task_id: str = "task-001") -> TaskRow:
    return TaskRow(task_id=task_id, title="A task", status=Status.NEW)


def test_open_creates_schema_and_persists(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "state.db"
    store = StateStore.open(db)
    store.insert_task(_new_task())
    store.close()
    assert db.exists()
    # Re-open: the row survived (WAL + a real file).
    store2 = StateStore.open(db)
    assert store2.get_task("task-001") is not None


def test_task_round_trip(store: StateStore) -> None:
    store.insert_task(_new_task())
    row = store.get_task("task-001")
    assert row is not None
    assert row.task_id == "task-001"
    assert row.status is Status.NEW
    assert row.created_at is not None and row.updated_at is not None


def test_task_id_exists(store: StateStore) -> None:
    assert store.task_id_exists("task-001") is False
    store.insert_task(_new_task())
    assert store.task_id_exists("task-001") is True


def test_set_status_and_update_task(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.set_status("task-001", Status.VALIDATED)
    row = store.get_task("task-001")
    assert row is not None and row.status is Status.VALIDATED

    store.update_task("task-001", branch="agent/task-001-x", validation_passed=True)
    row = store.get_task("task-001")
    assert row is not None
    assert row.branch == "agent/task-001-x"
    assert row.validation_passed is True


def test_find_active_tasks_excludes_terminal_and_pending(store: StateStore) -> None:
    store.insert_task(TaskRow(task_id="a", title="a", status=Status.IMPLEMENTING))
    store.insert_task(TaskRow(task_id="b", title="b", status=Status.PENDING))
    store.insert_task(TaskRow(task_id="c", title="c", status=Status.DONE))
    store.insert_task(TaskRow(task_id="d", title="d", status=Status.NEW))
    active = {t.task_id for t in store.find_active_tasks()}
    assert active == {"a"}


def test_counters_round_trip(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.save_counters(
        "task-001",
        LoopCounters(stage_attempts=1, test_fix_cycles=2, review_fix_cycles=1, fix_iterations=3),
    )
    counters = store.get_counters("task-001")
    assert counters.stage_attempts == 1
    assert counters.test_fix_cycles == 2
    assert counters.review_fix_cycles == 1
    assert counters.fix_iterations == 3


def test_transaction_rolls_back_on_error(store: StateStore) -> None:
    store.insert_task(_new_task())
    with pytest.raises(RuntimeError), store.transaction() as conn:
        store.set_status("task-001", Status.VALIDATED, conn)
        raise RuntimeError("boom")
    # The status change inside the failed transaction was rolled back.
    row = store.get_task("task-001")
    assert row is not None and row.status is Status.NEW


def test_transaction_commits_atomically(store: StateStore) -> None:
    store.insert_task(_new_task())
    with store.transaction() as conn:
        store.set_status("task-001", Status.VALIDATED, conn)
        store.update_task("task-001", conn, slug="abc")
    row = store.get_task("task-001")
    assert row is not None
    assert row.status is Status.VALIDATED
    assert row.slug == "abc"


def test_stage_run_and_provider_attempts(store: StateStore) -> None:
    store.insert_task(_new_task())
    run_id = store.record_stage_run(
        StageRunRow(
            task_id="task-001",
            stage="planning",
            route_primary="claude",
            route_fallback="codex",
            route_source="config",
            provider_used="claude",
            status="succeeded",
            stage_attempts=1,
        )
    )
    assert run_id > 0
    store.record_provider_attempt(
        ProviderAttemptRow(stage_run_id=run_id, provider="claude", attempt=1, status="succeeded")
    )
    cur = store._conn.execute(  # noqa: SLF001 - inspecting persisted rows in a unit test
        "SELECT provider FROM provider_attempts WHERE stage_run_id = ?", (run_id,)
    )
    assert [r["provider"] for r in cur.fetchall()] == ["claude"]


def test_check_run_and_artifact(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.record_check_run(
        CheckRunRow(
            task_id="task-001",
            command="npm test",
            passed=False,
            log_path="logs/task-001/checks/1.log",
            exit_code=1,
        )
    )
    store.register_artifact(
        ArtifactRow(
            task_id="task-001", kind="plan", path="logs/task-001/plan.md", checksum="deadbeef"
        )
    )
    checks = store._conn.execute("SELECT passed FROM check_runs").fetchall()  # noqa: SLF001
    assert checks[0]["passed"] == 0
    arts = store._conn.execute("SELECT kind, checksum FROM artifacts").fetchall()  # noqa: SLF001
    assert arts[0]["kind"] == "plan"


def test_artifact_registration_is_idempotent(store: StateStore) -> None:
    # Re-registering the same (task_id, kind, path) updates the checksum, never duplicates (§13).
    store.insert_task(_new_task())
    path = "logs/task-001/plan.md"
    store.register_artifact(ArtifactRow(task_id="task-001", kind="plan", path=path, checksum="aaa"))
    store.register_artifact(ArtifactRow(task_id="task-001", kind="plan", path=path, checksum="bbb"))
    rows = store._conn.execute(  # noqa: SLF001
        "SELECT checksum FROM artifacts WHERE kind='plan'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["checksum"] == "bbb"


def test_publish_op_idempotent_upsert(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.record_publish_op(
        PublishOpRow(task_id="task-001", kind="push", fingerprint="fp1", status="started")
    )
    store.record_publish_op(
        PublishOpRow(
            task_id="task-001", kind="push", fingerprint="fp1", status="completed", result_ref="ok"
        )
    )
    op = store.get_publish_op("task-001", "push")
    assert op is not None
    assert op.status == "completed"
    assert op.result_ref == "ok"
    # A single row, not two (idempotency via the UNIQUE constraint).
    count = store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM publish_operations"
    ).fetchone()["n"]
    assert count == 1


def test_publish_op_per_subtask(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.record_publish_op(
        PublishOpRow(
            task_id="task-001",
            kind="subtask_commit",
            subtask_order=1,
            fingerprint="f1",
            status="completed",
            result_ref="sha1",
        )
    )
    store.record_publish_op(
        PublishOpRow(
            task_id="task-001",
            kind="subtask_commit",
            subtask_order=2,
            fingerprint="f2",
            status="completed",
            result_ref="sha2",
        )
    )
    assert store.get_publish_op("task-001", "subtask_commit", 1).result_ref == "sha1"
    assert store.get_publish_op("task-001", "subtask_commit", 2).result_ref == "sha2"


def test_subtasks_round_trip_and_commit_marker(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.insert_subtasks(
        [
            SubtaskRow(
                task_id="task-001",
                order=1,
                slug="a",
                title="A",
                status="pending",
                depends_on=(),
            ),
            SubtaskRow(
                task_id="task-001",
                order=2,
                slug="b",
                title="B",
                status="pending",
                depends_on=(1,),
            ),
        ]
    )
    subs = store.get_subtasks("task-001")
    assert [s.order for s in subs] == [1, 2]
    assert subs[1].depends_on == (1,)
    assert all(s.commit_sha is None for s in subs)

    store.set_subtask_commit("task-001", 1, "abc123", "committed")
    subs = store.get_subtasks("task-001")
    assert subs[0].commit_sha == "abc123"
    assert subs[0].status == "committed"
    assert subs[1].commit_sha is None


def test_no_secret_columns_in_schema(store: StateStore) -> None:
    # §9/§12.6: no secret/token/env columns anywhere in the schema.
    cur = store._conn.execute(  # noqa: SLF001
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    forbidden = ("token", "secret", "password", "env", "credential", "api_key")
    for table in [r["name"] for r in cur.fetchall()]:
        cols = [
            c["name"].lower()
            for c in store._conn.execute(f'PRAGMA table_info("{table}")').fetchall()  # noqa: SLF001
        ]
        for col in cols:
            assert not any(bad in col for bad in forbidden), f"{table}.{col}"


def test_foreign_keys_enforced(store: StateStore) -> None:
    # A stage_run for a non-existent task violates the FK (foreign_keys=ON).
    with pytest.raises(sqlite3.IntegrityError):
        store.record_stage_run(
            StageRunRow(
                task_id="missing",
                stage="planning",
                route_primary="claude",
                route_source="config",
                stage_attempts=1,
            )
        )
