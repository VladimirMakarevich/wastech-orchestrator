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


def _tasks_columns(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(tasks)")}
    finally:
        conn.close()


# The full v2 ``tasks`` schema (everything the v3 schema has except ``interrupted_status``), so the
# migration is exercised against a realistic prior database that ``_task_from_row`` can read back.
_V2_TASKS = """
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    source_path TEXT,
    branch TEXT,
    slug TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    validation_passed INTEGER,
    validation_reason TEXT,
    refinement_ran INTEGER,
    refinement_skip_reason TEXT,
    stage_attempts INTEGER NOT NULL DEFAULT 0,
    test_fix_cycles INTEGER NOT NULL DEFAULT 0,
    review_fix_cycles INTEGER NOT NULL DEFAULT 0,
    fix_iterations INTEGER NOT NULL DEFAULT 0,
    decomposition_enabled INTEGER,
    decomposition_accepted INTEGER,
    decomposition_reason TEXT,
    subtask_count INTEGER,
    active_subtask INTEGER,
    subtasks_completed INTEGER NOT NULL DEFAULT 0,
    failure_report_path TEXT,
    cleanup_target_branch TEXT,
    cleanup_completed INTEGER,
    cleanup_completed_at TEXT,
    cleanup_last_error TEXT,
    finished_at TEXT
);
"""


def test_v2_database_is_migrated_to_add_interrupted_status(tmp_path: Path) -> None:
    # A pre-v3 tasks table lacks ``interrupted_status``; opening writable must add it in place and
    # leave the existing row readable (for ``rerun --continue``).
    db = tmp_path / "v2.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_V2_TASKS)
    conn.execute(
        "INSERT INTO tasks (task_id, title, status, created_at, updated_at) VALUES "
        "('task-1', 'T', 'failed', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    conn.close()
    assert "interrupted_status" not in _tasks_columns(db)

    store = StateStore.open(db)
    try:
        assert "interrupted_status" in _tasks_columns(db)
        assert _user_version(db) == DB_SCHEMA_VERSION
        row = store.get_task("task-1")
        assert row is not None and row.interrupted_status is None
    finally:
        store.close()


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
