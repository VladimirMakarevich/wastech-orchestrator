"""Unit tests for the SQLite State Store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from wastech_orchestrator.core.loop_control import LoopCounters
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.state_store import (
    ArtifactRow,
    CheckRunRow,
    EditingLineageRow,
    EvaluationRow,
    NodeRunRow,
    ProviderAttemptRow,
    PublishOpRow,
    StateStore,
    SubtaskRow,
    TaskRow,
)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore.open(tmp_path / "state.db")


def _new_task(task_id: str = "task-001") -> TaskRow:
    return TaskRow(task_id=task_id, title="A task", status=Status.NEW)


def test_no_memory_tables_in_state_db(store: StateStore) -> None:
    # AC-S3: the memory subsystem is files-first — its tiers/audit/quarantine live under
    # .worc/memory/, NEVER in state.db. Guard the exact table set so an accidental memory table is
    # caught; the subsystem's only state.db touch is the reused `evaluations` marker row
    # (kind="memory_write"), not a new table. (Update this set consciously when a table is added.)
    names = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert names == {
        "tasks",
        "node_runs",
        "provider_attempts",
        "check_runs",
        "artifacts",
        "publish_operations",
        "subtasks",
        "evaluations",
        "editing_lineage",
        "node_lineage",
    }
    memory_keywords = ("memory", "lesson", "episode", "entit", "quarantine")
    assert not any(kw in name for name in names for kw in memory_keywords)


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

    store.update_task("task-001", branch="worc/task-001-x", validation_passed=True)
    row = store.get_task("task-001")
    assert row is not None
    assert row.branch == "worc/task-001-x"
    assert row.validation_passed is True


def test_blocked_since_round_trips_and_clears(store: StateStore) -> None:
    # B-lite (DB v13): the soft-pause marker defaults to None, is set on park, cleared at terminal.
    store.insert_task(_new_task())
    assert store.get_task("task-001").blocked_since is None  # type: ignore[union-attr]
    store.update_task("task-001", blocked_since="2026-06-27T00:00:00+00:00")
    assert store.get_task("task-001").blocked_since == "2026-06-27T00:00:00+00:00"  # type: ignore[union-attr]
    store.update_task("task-001", blocked_since=None)
    assert store.get_task("task-001").blocked_since is None  # type: ignore[union-attr]


def test_find_active_tasks_excludes_terminal_and_pending(store: StateStore) -> None:
    store.insert_task(TaskRow(task_id="a", title="a", status=Status.RUNNING))
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
            status=Status.RUNNING,
            updated_at="2026-01-02T00:00:00+00:00",
        )
    )

    latest = store.latest_task()
    assert latest is not None
    assert latest.task_id == "newer"


def _stamped(task_id: str, status: Status, updated_at: str) -> TaskRow:
    return TaskRow(task_id=task_id, title=task_id, status=status, updated_at=updated_at)


def test_recent_tasks_only_terminal_by_recency_and_limit(store: StateStore) -> None:
    store.insert_task(_stamped("run", Status.RUNNING, "2026-01-05"))
    store.insert_task(_stamped("pend", Status.PENDING, "2026-01-04"))
    store.insert_task(_stamped("done", Status.DONE, "2026-01-03"))
    store.insert_task(_stamped("fail", Status.FAILED, "2026-01-02"))
    store.insert_task(_stamped("manual", Status.MANUAL_ACTION_REQUIRED, "2026-01-01"))

    # Only terminal tasks, most recently updated first.
    assert [t.task_id for t in store.recent_tasks(10)] == ["done", "fail", "manual"]
    # The limit caps the result.
    assert [t.task_id for t in store.recent_tasks(2)] == ["done", "fail"]


def test_recent_tasks_empty_when_no_terminal(store: StateStore) -> None:
    store.insert_task(TaskRow(task_id="run", title="run", status=Status.RUNNING))
    assert store.recent_tasks(10) == []


def test_all_tasks_returns_every_status_by_recency(store: StateStore) -> None:
    store.insert_task(_stamped("run", Status.RUNNING, "2026-01-03"))
    store.insert_task(_stamped("done", Status.DONE, "2026-01-02"))
    store.insert_task(_stamped("pend", Status.PENDING, "2026-01-01"))
    assert [t.task_id for t in store.all_tasks()] == ["run", "done", "pend"]


def test_counters_round_trip(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.save_counters(
        "task-001",
        LoopCounters(
            test_fix_cycles=2,
            review_fix_cycles=1,
            test_fix_total=4,
            review_fix_total=7,
            fix_iterations=3,
        ),
    )
    counters = store.get_counters("task-001")
    assert counters.test_fix_cycles == 2
    assert counters.review_fix_cycles == 1
    # F49: the cumulative totals round-trip alongside the consecutive counters.
    assert counters.test_fix_total == 4
    assert counters.review_fix_total == 7
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


def test_provider_attempts_round_trip(store: StateStore) -> None:
    # provider_attempts hangs off a node_runs id (node_run_id); node-run reserve/complete/skip
    # round-trips are covered in tests/state/test_node_runs.py.
    store.insert_task(_new_task())
    run_id = store.record_node_run(
        NodeRunRow(
            task_id="task-001",
            node_id="planning",
            node_kind="agent",
            status="running",
            route_primary="claude",
            route_fallback="codex",
            route_source="config",
        )
    )
    assert run_id > 0
    store.record_provider_attempt(
        ProviderAttemptRow(node_run_id=run_id, provider="claude", attempt=1, status="succeeded")
    )
    cur = store._conn.execute(
        "SELECT provider FROM provider_attempts WHERE node_run_id = ?", (run_id,)
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
    checks = store._conn.execute("SELECT passed FROM check_runs").fetchall()
    assert checks[0]["passed"] == 0
    arts = store._conn.execute("SELECT kind, checksum FROM artifacts").fetchall()
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
    # Re-registering the same (task_id, kind, path) updates the checksum, never duplicates.
    store.insert_task(_new_task())
    path = "logs/task-001/plan.md"
    store.register_artifact(ArtifactRow(task_id="task-001", kind="plan", path=path, checksum="aaa"))
    store.register_artifact(ArtifactRow(task_id="task-001", kind="plan", path=path, checksum="bbb"))
    rows = store._conn.execute("SELECT checksum FROM artifacts WHERE kind='plan'").fetchall()
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
    count = store._conn.execute("SELECT COUNT(*) AS n FROM publish_operations").fetchone()["n"]
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
    #: no secret/token/env columns anywhere in the schema.
    cur = store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    forbidden = ("token", "secret", "password", "env", "credential", "api_key")
    for table in [r["name"] for r in cur.fetchall()]:
        cols = [
            c["name"].lower()
            for c in store._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        ]
        for col in cols:
            assert not any(bad in col for bad in forbidden), f"{table}.{col}"


def test_foreign_keys_enforced(store: StateStore) -> None:
    # A node_run for a non-existent task violates the FK (foreign_keys=ON).
    with pytest.raises(sqlite3.IntegrityError):
        store.record_node_run(NodeRunRow(task_id="missing", node_id="planning", node_kind="agent"))


# --- rerun / continue reset primitives ---------------------------------------------------


def _seed_terminal_task(store: StateStore, *, status: Status = Status.FAILED) -> None:
    """A failed task carrying branch, counters, decomposition, subtasks and publish ops."""
    store.insert_task(
        TaskRow(
            task_id="task-001",
            title="A task",
            status=status,
            branch="worc/task-001-a-task",
            slug="a-task",
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
        )
    )
    store.insert_subtasks([SubtaskRow("task-001", 1, "a", "A", "committed", (), commit_sha="abc")])
    store.record_publish_op(
        PublishOpRow(
            task_id="task-001",
            kind="pr",
            fingerprint="b",
            status="completed",
            result_ref="https://example/pull/1",
        )
    )


def test_reset_task_for_rerun_clears_per_attempt_state(store: StateStore) -> None:
    _seed_terminal_task(store)
    store.reset_task_for_rerun("task-001")

    row = store.get_task("task-001")
    assert row is not None
    assert row.status is Status.FAILED  # left terminal — run_task's upsert flips it
    assert row.branch is None and row.slug is None
    assert row.fix_iterations == 0
    assert row.test_fix_cycles == 0 and row.review_fix_cycles == 0
    assert row.decomposition_accepted is None and row.subtask_count is None
    assert row.subtasks_completed == 0
    assert row.failure_report_path is None
    assert row.cleanup_completed is None and row.cleanup_completed_at is None
    assert row.cleanup_target_branch is None and row.finished_at is None
    assert store.get_subtasks("task-001") == []  # subtasks deleted
    assert store.get_publish_op("task-001", "pr") is None  # publish idempotency cleared


def test_revive_task_for_continue_preserves_work(store: StateStore) -> None:
    _seed_terminal_task(store)
    store.revive_task_for_continue("task-001", Status.RUNNING)

    row = store.get_task("task-001")
    assert row is not None
    assert row.status is Status.RUNNING  # revived to the generic in-flight status
    assert row.finished_at is None
    assert row.cleanup_completed is None and row.cleanup_completed_at is None
    assert row.cleanup_target_branch is None
    # The work is kept — that is the whole point of continue.
    assert row.branch == "worc/task-001-a-task"
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


# --- editing_lineage (durable sessions, P2.2) ---------------------------------------------


def test_editing_lineage_roundtrip_and_one_per_lineage(store: StateStore) -> None:
    store.insert_task(_new_task())
    assert store.get_editing_lineage("task-001", "implementation") is None  # none yet
    store.upsert_editing_lineage(
        EditingLineageRow(
            task_id="task-001",
            lineage_key="implementation",
            provider="claude",
            raw_session_id="sess-a",
        )
    )
    row = store.get_editing_lineage("task-001", "implementation")
    assert row is not None and row.provider == "claude" and row.raw_session_id == "sess-a"
    # Upsert replaces in place — exactly one active editing session per (unit, lineage_key).
    store.upsert_editing_lineage(
        EditingLineageRow(
            task_id="task-001",
            lineage_key="implementation",
            provider="claude",
            raw_session_id="sess-b",
        )
    )
    row = store.get_editing_lineage("task-001", "implementation")
    assert row is not None and row.raw_session_id == "sess-b"
    count = store._conn.execute(
        "SELECT COUNT(*) FROM editing_lineage WHERE task_id = ?", ("task-001",)
    ).fetchone()[0]
    assert count == 1


def test_editing_lineage_multiple_lineages_per_unit_are_isolated(store: StateStore) -> None:
    # One execution unit can hold more than one durable editing session, one per lineage_key; the
    # two tracks (e.g. a code track and a spec track) never leak session context into each other.
    store.insert_task(_new_task())
    store.upsert_editing_lineage(
        EditingLineageRow(
            task_id="task-001", lineage_key="code", provider="claude", raw_session_id="code-sess"
        )
    )
    store.upsert_editing_lineage(
        EditingLineageRow(
            task_id="task-001", lineage_key="spec", provider="claude", raw_session_id="spec-sess"
        )
    )
    assert store.get_editing_lineage("task-001", "code").raw_session_id == "code-sess"  # type: ignore[union-attr]
    assert store.get_editing_lineage("task-001", "spec").raw_session_id == "spec-sess"  # type: ignore[union-attr]
    count = store._conn.execute(
        "SELECT COUNT(*) FROM editing_lineage WHERE task_id = ?", ("task-001",)
    ).fetchone()[0]
    assert count == 2


def test_editing_lineage_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = StateStore.open(db)
    store.insert_task(_new_task())
    store.upsert_editing_lineage(
        EditingLineageRow(
            task_id="task-001",
            lineage_key="implementation",
            provider="codex",
            raw_session_id="sess-durable",
        )
    )
    store.close()
    # A restart (a fresh store on the same file) rehydrates the editing session.
    store2 = StateStore.open(db)
    row = store2.get_editing_lineage("task-001", "implementation")
    assert row is not None and row.provider == "codex" and row.raw_session_id == "sess-durable"
    store2.close()


def test_editing_lineage_root_and_subtask_are_distinct(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.upsert_editing_lineage(
        EditingLineageRow(
            task_id="task-001",
            lineage_key="implementation",
            provider="claude",
            raw_session_id="root",
        )
    )
    store.upsert_editing_lineage(
        EditingLineageRow(
            task_id="task-001",
            lineage_key="implementation",
            subtask_order=2,
            provider="claude",
            raw_session_id="sub-2",
        )
    )
    assert store.get_editing_lineage("task-001", "implementation").raw_session_id == "root"  # type: ignore[union-attr]
    assert store.get_editing_lineage("task-001", "implementation", 2).raw_session_id == "sub-2"  # type: ignore[union-attr]


def test_reset_for_rerun_clears_editing_lineage(store: StateStore) -> None:
    store.insert_task(_new_task())
    store.upsert_editing_lineage(
        EditingLineageRow(
            task_id="task-001",
            lineage_key="implementation",
            provider="claude",
            raw_session_id="sess",
        )
    )
    store.reset_task_for_rerun("task-001")
    # A fresh rerun clears every lineage of the task.
    assert store.get_editing_lineage("task-001", "implementation") is None


def test_evaluations_append_only_and_counted(store: StateStore) -> None:
    store.insert_task(_new_task())
    for i, verdict in enumerate(("rework", "accept", "rework"), start=1):
        store.record_evaluation(
            EvaluationRow(
                task_id="task-001",
                node_id="review",
                source_node_run_id=i,
                kind="in_flow_verdict",
                verdict=verdict,
                findings_json="[]",
            )
        )
    rows = store.get_evaluations("task-001")
    assert [r.verdict for r in rows] == ["rework", "accept", "rework"]  # append-only
    assert store.count_rework_verdicts("task-001") == 2  # the per-instance limit derives from COUNT
