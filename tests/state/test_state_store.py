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


def test_open_readonly_reads_without_allowing_writes(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    writable = StateStore.open(db)
    writable.insert_task(_new_task())
    writable.close()

    readonly = StateStore.open_readonly(db)
    assert readonly.get_task("task-001") is not None
    with pytest.raises(sqlite3.OperationalError):
        readonly.insert_task(_new_task("task-002"))
    readonly.close()


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


def test_latest_task_uses_updated_at(store: StateStore) -> None:
    store.insert_task(
        TaskRow(
            task_id="older",
            title="older",
            status=Status.DONE,
            updated_at="2026-01-01T00:00:00+00:00",
        )
    )
    store.insert_task(
        TaskRow(
            task_id="newer",
            title="newer",
            status=Status.PLANNING,
            updated_at="2026-01-02T00:00:00+00:00",
        )
    )

    latest = store.latest_task()
    assert latest is not None
    assert latest.task_id == "newer"


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


def test_record_skip_writes_audit_row(store: StateStore) -> None:
    store.insert_task(_new_task())
    run_id = store.record_skip(
        "task-001", "testing", reason="global config (agents.skip_stages)", subtask_order=2
    )
    assert run_id > 0
    row = store._conn.execute(  # noqa: SLF001 - inspecting persisted rows in a unit test
        "SELECT * FROM stage_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row is not None
    assert row["stage"] == "testing"
    assert row["status"] == "skipped"
    assert row["skipped"] == 1
    assert row["skip_reason"] == "global config (agents.skip_stages)"
    assert row["subtask_order"] == 2
    assert row["provider_used"] is None
    assert row["stage_attempts"] == 0


def test_stage_run_can_be_reserved_then_completed(store: StateStore) -> None:
    store.insert_task(_new_task())
    run_id = store.record_stage_run(
        StageRunRow(
            task_id="task-001",
            stage="fixing",
            route_primary="claude",
            route_source="config",
            status="running",
            stage_attempts=0,
            started_at="t0",
        )
    )

    store.complete_stage_run(
        run_id,
        status="succeeded",
        provider_used="claude",
        error_class=None,
        stage_attempts=1,
        finished_at="t1",
    )

    row = store._conn.execute(  # noqa: SLF001 - inspecting persisted rows in a unit test
        "SELECT * FROM stage_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["provider_used"] == "claude"
    assert row["stage_attempts"] == 1
    assert row["started_at"] == "t0"
    assert row["finished_at"] == "t1"


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


def test_latest_failed_check_log_is_scoped_to_subtask(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.record_check_run(
        CheckRunRow(
            task_id="task-001",
            subtask_order=1,
            command="pytest",
            passed=False,
            log_path="checks/sub-1-old.log",
        )
    )
    store.record_check_run(
        CheckRunRow(
            task_id="task-001",
            subtask_order=2,
            command="pytest",
            passed=False,
            log_path="checks/sub-2.log",
        )
    )
    store.record_check_run(
        CheckRunRow(
            task_id="task-001",
            subtask_order=1,
            command="pytest",
            passed=False,
            log_path="checks/sub-1-new.log",
        )
    )

    assert store.latest_failed_check_log("task-001", 1) == "checks/sub-1-new.log"
    assert store.latest_failed_check_log("task-001", 2) == "checks/sub-2.log"
    assert store.latest_failed_check_log("task-001") is None


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


def test_subtask_planning_insert_is_idempotent_without_reopening_committed_work(
    store: StateStore,
) -> None:
    store.insert_task(_new_task())
    original = SubtaskRow("task-001", 1, "old", "Old", "pending", ())
    store.insert_subtasks([original])
    store.insert_subtasks([SubtaskRow("task-001", 1, "new", "New", "pending", ())])
    assert store.get_subtasks("task-001")[0].slug == "new"

    store.set_subtask_commit("task-001", 1, "abc123", "committed")
    store.insert_subtasks([original])
    committed = store.get_subtasks("task-001")[0]
    assert committed.slug == "new"
    assert committed.status == "committed"
    assert committed.commit_sha == "abc123"


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


# --- rerun / continue reset primitives ---------------------------------------------------


def _seed_terminal_task(store: StateStore, *, status: Status = Status.FAILED) -> None:
    """A failed task carrying branch, counters, decomposition, subtasks and publish ops."""
    store.insert_task(
        TaskRow(
            task_id="task-001",
            title="A task",
            status=status,
            branch="agent/task-001-a-task",
            slug="a-task",
            stage_attempts=3,
            test_fix_cycles=2,
            review_fix_cycles=1,
            fix_iterations=4,
            decomposition_accepted=True,
            decomposition_reason="big",
            subtask_count=2,
            subtasks_completed=1,
            failure_report_path="logs/task-001/failure_report.json",
            cleanup_target_branch="main",
            cleanup_completed=True,
            cleanup_completed_at="2026-01-01T00:00:00+00:00",
            finished_at="2026-01-01T00:00:00+00:00",
            interrupted_status=Status.REVIEWING.value,
        )
    )
    store.insert_subtasks([SubtaskRow("task-001", 1, "a", "A", "committed", (), commit_sha="abc")])
    store.record_publish_op(
        PublishOpRow(task_id="task-001", kind="pr", fingerprint="b", status="completed",
                     result_ref="https://example/pull/1")
    )


def test_reset_task_for_rerun_clears_per_attempt_state(store: StateStore) -> None:
    _seed_terminal_task(store)
    store.reset_task_for_rerun("task-001")

    row = store.get_task("task-001")
    assert row is not None
    assert row.status is Status.FAILED  # left terminal — run_task's upsert flips it
    assert row.branch is None and row.slug is None
    assert row.stage_attempts == 0 and row.fix_iterations == 0
    assert row.test_fix_cycles == 0 and row.review_fix_cycles == 0
    assert row.decomposition_accepted is None and row.subtask_count is None
    assert row.subtasks_completed == 0
    assert row.failure_report_path is None
    assert row.cleanup_completed is None and row.cleanup_completed_at is None
    assert row.cleanup_target_branch is None and row.finished_at is None
    assert row.interrupted_status is None
    assert store.get_subtasks("task-001") == []  # subtasks deleted
    assert store.get_publish_op("task-001", "pr") is None  # publish idempotency cleared


def test_revive_task_for_continue_preserves_work(store: StateStore) -> None:
    _seed_terminal_task(store)
    store.revive_task_for_continue("task-001", Status.REVIEWING)

    row = store.get_task("task-001")
    assert row is not None
    assert row.status is Status.REVIEWING  # revived to the failed stage
    assert row.finished_at is None
    assert row.cleanup_completed is None and row.cleanup_completed_at is None
    assert row.cleanup_target_branch is None
    assert row.interrupted_status is None
    # The work is kept — that is the whole point of continue.
    assert row.branch == "agent/task-001-a-task"
    assert row.fix_iterations == 4 and row.review_fix_cycles == 1
    assert row.decomposition_accepted is True and row.subtask_count == 2
    assert store.get_subtasks("task-001")[0].commit_sha == "abc"
    assert store.get_publish_op("task-001", "pr") is not None


def test_clear_publish_operations(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.record_publish_op(
        PublishOpRow(task_id="task-001", kind="push", fingerprint="f", status="completed")
    )
    store.clear_publish_operations("task-001")
    assert store.get_publish_op("task-001", "push") is None


def test_insert_task_upsert_refreshes_registration_fields(store: StateStore) -> None:
    store.insert_task(
        TaskRow(task_id="task-001", title="Old", status=Status.FAILED, source_path="a.md")
    )
    created = store.get_task("task-001").created_at
    # A second insert with the same id (a rerun re-register) updates in place, does not raise.
    store.insert_task(
        TaskRow(
            task_id="task-001",
            title="New",
            status=Status.NEW,
            source_path="b.md",
            validation_passed=True,
        )
    )
    row = store.get_task("task-001")
    assert row is not None
    assert row.title == "New" and row.status is Status.NEW
    assert row.source_path == "b.md" and row.validation_passed is True
    assert row.created_at == created  # creation timestamp preserved
