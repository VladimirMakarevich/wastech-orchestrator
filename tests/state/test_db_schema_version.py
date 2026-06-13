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


def _stage_runs_columns(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {str(r[1]) for r in conn.execute("PRAGMA table_info(stage_runs)")}
    finally:
        conn.close()


def test_v1_database_is_migrated_to_add_skip_columns(tmp_path: Path) -> None:
    # A pre-v2 stage_runs table lacks the skip columns; opening writable must add them in place.
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE stage_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            route_primary TEXT NOT NULL,
            route_source TEXT NOT NULL,
            stage_attempts INTEGER NOT NULL
        )
        """
    )
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    assert "skipped" not in _stage_runs_columns(db)

    StateStore.open(db).close()
    cols = _stage_runs_columns(db)
    assert {"skipped", "skip_reason"} <= cols
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
