"""state.db schema-version gate (PRAGMA user_version): stamp, adopt legacy, refuse newer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from wastech_orchestrator.state_store import (
    DB_SCHEMA_VERSION,
    IncompatibleStateError,
    StateStore,
)


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_open_stamps_current_version(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    StateStore.open(db).close()
    assert _user_version(db) == DB_SCHEMA_VERSION


def test_legacy_zero_version_is_adopted_without_error(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE x(a)")
    conn.commit()
    conn.close()
    assert _user_version(db) == 0  # a pre-versioning database

    StateStore.open(db).close()  # adopted, no error
    assert _user_version(db) == DB_SCHEMA_VERSION


def test_newer_version_is_refused_on_both_open_paths(tmp_path: Path) -> None:
    db = tmp_path / "newer.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(IncompatibleStateError, match="newer than this orchestrator supports"):
        StateStore.open(db)
    with pytest.raises(IncompatibleStateError):
        StateStore.open_readonly(db)


def test_pre_v7_incompatible_shape_is_refused_not_stamped(tmp_path: Path) -> None:
    # An older versioned database carries a now-incompatible shape (here the legacy
    # `provider_attempts.stage_run_id`, before the v6 rename to `node_run_id`). Because the v5-v7
    # changes are destructive and `_migrate` is additive-only, open() must refuse fail-closed — it
    # must NOT stamp the current version onto the old shape (which previously passed the gate and
    # then crashed on the first provider-attempt write). Greenfield: the fix is refusal, not a
    # destructive migration.
    db = tmp_path / "v6.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE provider_attempts (id INTEGER PRIMARY KEY, stage_run_id INTEGER NOT NULL)"
    )
    conn.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION - 1}")  # a pre-v7 (legacy) database
    conn.commit()
    conn.close()

    with pytest.raises(IncompatibleStateError, match="predates an incompatible"):
        StateStore.open(db)
    with pytest.raises(IncompatibleStateError):
        StateStore.open_readonly(db)
    # The refusal left the file untouched (no version stamp), so it is not silently wedged.
    assert _user_version(db) == DB_SCHEMA_VERSION - 1


def test_the_gate_reference_column_is_added_to_a_pre_versioning_database(tmp_path: Path) -> None:
    # v22 is additive, so the one database `_migrate` may reshape — a `0`-stamped, pre-versioning
    # one — gains `tasks.gate_reference_sha` and keeps the rows it already had. (An *older
    # versioned* database is refused fail-closed instead, as the test above shows; greenfield means
    # there is no production data to migrate.)
    db = tmp_path / "prev.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (task_id, title, status, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("task-001", "kept", "new", "t0", "t0"),
    )
    conn.commit()
    conn.close()
    assert _user_version(db) == 0

    store = StateStore.open(db)
    try:
        columns = {str(row[1]) for row in store._conn.execute("PRAGMA table_info(tasks)")}
        assert "gate_reference_sha" in columns
        assert store.get_gate_reference("task-001") is None  # NULL means "the task's diff base"
        row = store._conn.execute(
            "SELECT title FROM tasks WHERE task_id = ?", ("task-001",)
        ).fetchone()
        assert row["title"] == "kept"  # the pre-existing task survived the column add
    finally:
        store.close()
    assert _user_version(db) == DB_SCHEMA_VERSION


def test_the_pushed_sha_column_is_added_to_a_pre_versioning_database(tmp_path: Path) -> None:
    # v23 is additive on `publish_operations`, so the one database `_migrate` may reshape — a
    # `0`-stamped, pre-versioning one — gains `pushed_sha` and keeps the publish rows it had.
    db = tmp_path / "prev.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE publish_operations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL REFERENCES tasks(task_id), kind TEXT NOT NULL, "
        "subtask_order INTEGER NOT NULL DEFAULT -1, fingerprint TEXT NOT NULL, result_ref TEXT, "
        "status TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(task_id, kind, subtask_order))"
    )
    conn.execute(
        "INSERT INTO tasks (task_id, title, status, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("task-001", "kept", "new", "t0", "t0"),
    )
    conn.execute(
        "INSERT INTO publish_operations (task_id, kind, subtask_order, fingerprint, result_ref, "
        "status, created_at) VALUES (?,?,?,?,?,?,?)",
        ("task-001", "push", -1, "worc/task-001", "worc/task-001", "completed", "t0"),
    )
    conn.commit()
    conn.close()
    assert _user_version(db) == 0

    store = StateStore.open(db)
    try:
        columns = {
            str(row[1]) for row in store._conn.execute("PRAGMA table_info(publish_operations)")
        }
        assert "pushed_sha" in columns
        op = store.get_publish_op("task-001", "push")
        assert op is not None
        assert op.result_ref == "worc/task-001"  # the pre-existing publish row survived
        assert op.pushed_sha is None  # NULL means "we have no record of what we pushed"
    finally:
        store.close()
    assert _user_version(db) == DB_SCHEMA_VERSION
