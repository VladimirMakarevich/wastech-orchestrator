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
