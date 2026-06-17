"""SQLite State Store (spec §9).

The authoritative persisted state for the pipeline: the ``tasks``, ``stage_runs``,
``provider_attempts``, ``check_runs``, ``artifacts``, ``publish_operations`` and ``subtasks``
entities. State transitions are **transactional** (``BEGIN IMMEDIATE`` … ``COMMIT``) so a crash
leaves a consistent prior state and a restart can reconcile (§13).

**No secrets, tokens, or full process environment are ever written here** (§9, §12.6) — only ids,
statuses, error classes, file paths, sha256 checksums, counters, idempotency fingerprints and
commit SHAs. Callers are responsible for never passing a secret into a field; this module persists
exactly what it is given, so the redaction boundary lives in the providers and the artifact writer.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from wastech_orchestrator.core.loop_control import LoopCounters
from wastech_orchestrator.core.state_machine import Status


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# The SQLite schema version, stamped into ``PRAGMA user_version``. Bumped only when the schema
# changes (not on every release). ``open()`` adopts a 0 (brand-new, or pre-versioning) database as
# the current version; both open paths refuse a database stamped newer than this. See the spec's
# "Versioning & compatibility" section.
# v2: added ``stage_runs.skipped`` / ``stage_runs.skip_reason`` (stage-skip control). Migrated
# in-place by ``_migrate`` (idempotent ``ALTER TABLE ADD COLUMN``); no data is rewritten.
# v3: added ``tasks.interrupted_status`` (the stage a task was on before going terminal), so
# ``rerun --continue`` can re-enter the pipeline at the failed stage.
# v4 (flow-engine P1.2): added the flow-engine execution path **alongside** the legacy driver — the
# ``node_runs`` table (the generic per-node audit trail that generalizes ``stage_runs``) and the
# ``tasks.current_node`` / ``tasks.flow_run_counters`` / ``tasks.flow_fingerprint`` columns (the
# durable :class:`~wastech_orchestrator.core.flow.run_state.FlowRunState` checkpoint). All are
# additive and nullable; the legacy ``stage_runs`` + granular statuses stay until P1.5 removes them.
DB_SCHEMA_VERSION = 4


class IncompatibleStateError(Exception):
    """The on-disk ``state.db`` schema version is newer than this orchestrator understands."""


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema (writable open only).

    Idempotent: each column add is guarded by a ``PRAGMA table_info`` check, so this is a no-op on
    a brand-new DB (``_SCHEMA`` already created the columns) and adds only what a pre-v2 DB lacks.
    """
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(stage_runs)")}
    if "skipped" not in cols:
        conn.execute("ALTER TABLE stage_runs ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0")
    if "skip_reason" not in cols:
        conn.execute("ALTER TABLE stage_runs ADD COLUMN skip_reason TEXT")
    task_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
    if "interrupted_status" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN interrupted_status TEXT")
    # v4: the FlowRunState checkpoint columns (flow-engine execution path).
    if "current_node" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN current_node TEXT")
    if "flow_run_counters" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN flow_run_counters TEXT")
    if "flow_fingerprint" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN flow_fingerprint TEXT")


def _enforce_schema_version(conn: sqlite3.Connection, *, writable: bool) -> None:
    """Verify (and, when ``writable``, migrate + stamp) ``PRAGMA user_version``.

    A database newer than this orchestrator is refused (fail-loud). On the writable path an older or
    pre-versioning (``0``) database is migrated in place by :func:`_migrate` and then stamped to
    ``DB_SCHEMA_VERSION``. The read-only path only verifies the bound (it never mutates the file).
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > DB_SCHEMA_VERSION:
        raise IncompatibleStateError(
            f"state.db schema version {current} is newer than this orchestrator supports "
            f"({DB_SCHEMA_VERSION}); upgrade wastech-orchestrator or start a fresh workspace"
        )
    if writable:
        _migrate(conn)
        if current != DB_SCHEMA_VERSION:
            # DB_SCHEMA_VERSION is a trusted int constant (no injection); PRAGMA can't be a param.
            conn.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION}")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
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
    finished_at TEXT,
    interrupted_status TEXT,
    current_node TEXT,
    flow_run_counters TEXT,
    flow_fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    stage TEXT NOT NULL,
    subtask_order INTEGER,
    status TEXT,
    route_primary TEXT NOT NULL,
    route_fallback TEXT,
    route_source TEXT NOT NULL,
    provider_used TEXT,
    error_class TEXT,
    stage_attempts INTEGER NOT NULL,
    commit_sha_before TEXT,
    commit_sha_after TEXT,
    started_at TEXT,
    finished_at TEXT,
    skipped INTEGER NOT NULL DEFAULT 0,
    skip_reason TEXT
);

CREATE TABLE IF NOT EXISTS node_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    node_id TEXT NOT NULL,
    node_kind TEXT NOT NULL,
    subtask_order INTEGER,
    status TEXT,
    outcome TEXT,
    route_primary TEXT,
    route_fallback TEXT,
    route_source TEXT,
    provider_used TEXT,
    error_class TEXT,
    stage_attempts INTEGER NOT NULL DEFAULT 0,
    commit_sha_before TEXT,
    commit_sha_after TEXT,
    started_at TEXT,
    finished_at TEXT,
    skipped INTEGER NOT NULL DEFAULT 0,
    skip_reason TEXT
);

CREATE TABLE IF NOT EXISTS provider_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_run_id INTEGER NOT NULL REFERENCES stage_runs(id),
    provider TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT,
    error_class TEXT,
    exit_code INTEGER,
    attempt_dir TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS check_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    subtask_order INTEGER,
    command TEXT NOT NULL,
    exit_code INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL,
    log_path TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, kind, path)
);

CREATE TABLE IF NOT EXISTS publish_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    kind TEXT NOT NULL,
    subtask_order INTEGER NOT NULL DEFAULT -1,
    fingerprint TEXT NOT NULL,
    result_ref TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, kind, subtask_order)
);

CREATE TABLE IF NOT EXISTS subtasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    "order" INTEGER NOT NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    commit_sha TEXT,
    artifact_path TEXT,
    UNIQUE(task_id, "order")
);
"""


# --- Row dataclasses (mirroring the §9 entities) ---------------------------------------------


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    title: str
    status: Status
    source_path: str | None = None
    branch: str | None = None
    slug: str | None = None
    validation_passed: bool | None = None
    validation_reason: str | None = None
    refinement_ran: bool | None = None
    refinement_skip_reason: str | None = None
    stage_attempts: int = 0
    test_fix_cycles: int = 0
    review_fix_cycles: int = 0
    fix_iterations: int = 0
    decomposition_enabled: bool | None = None
    decomposition_accepted: bool | None = None
    decomposition_reason: str | None = None
    subtask_count: int | None = None
    active_subtask: int | None = None
    subtasks_completed: int = 0
    failure_report_path: str | None = None
    cleanup_target_branch: str | None = None
    cleanup_completed: bool | None = None
    cleanup_completed_at: str | None = None
    cleanup_last_error: str | None = None
    finished_at: str | None = None
    interrupted_status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class StageRunRow:
    task_id: str
    stage: str
    route_primary: str
    route_source: str
    stage_attempts: int
    route_fallback: str | None = None
    subtask_order: int | None = None
    status: str | None = None
    provider_used: str | None = None
    error_class: str | None = None
    commit_sha_before: str | None = None
    commit_sha_after: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    # Stage-skip audit (stage-skip control): a skipped stage gets a row with ``skipped=True`` and a
    # human-readable ``skip_reason`` and no provider data.
    skipped: bool = False
    skip_reason: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class NodeRunRow:
    """One flow-graph node execution (flow-engine P1.2 — the generic ``stage_runs`` successor).

    ``node_kind`` is one of ``agent``/``evaluator``/``checks``/``hitl``/``publish``; ``outcome`` is
    the engine outcome (``accept``/``rework``/``pass``/``fail``/``done``/``route:*``). ``route_*``
    are ``None`` for non-agent nodes. A skipped node carries ``skipped=True`` + ``skip_reason`` and
    no provider data (mirrors :meth:`record_skip`).
    """

    task_id: str
    node_id: str
    node_kind: str
    subtask_order: int | None = None
    status: str | None = None
    outcome: str | None = None
    route_primary: str | None = None
    route_fallback: str | None = None
    route_source: str | None = None
    provider_used: str | None = None
    error_class: str | None = None
    stage_attempts: int = 0
    commit_sha_before: str | None = None
    commit_sha_after: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    skipped: bool = False
    skip_reason: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class ProviderAttemptRow:
    stage_run_id: int
    provider: str
    attempt: int
    status: str | None = None
    error_class: str | None = None
    exit_code: int | None = None
    attempt_dir: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class CheckRunRow:
    task_id: str
    command: str
    passed: bool
    log_path: str
    subtask_order: int | None = None
    exit_code: int | None = None
    timed_out: bool = False
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class ArtifactRow:
    task_id: str
    kind: str
    path: str
    checksum: str


@dataclass(frozen=True)
class PublishOpRow:
    task_id: str
    kind: str
    fingerprint: str
    status: str
    subtask_order: int | None = None
    result_ref: str | None = None


@dataclass(frozen=True)
class SubtaskRow:
    task_id: str
    order: int
    slug: str
    title: str
    status: str
    depends_on: tuple[int, ...]
    commit_sha: str | None = None
    artifact_path: str | None = None


# ``publish_operations`` uses -1 as the "no subtask" sentinel so the UNIQUE constraint works
# (SQLite treats every NULL as distinct, which would defeat idempotency).
_NO_SUBTASK = -1


class StateStore:
    """A thin transactional wrapper over a single SQLite database file (§9)."""

    def __init__(
        self, conn: sqlite3.Connection, *, clock: Callable[[], str] = _utc_now_iso
    ) -> None:
        self._conn = conn
        self._clock = clock

    @classmethod
    def open(cls, db_path: str | Path, *, clock: Callable[[], str] = _utc_now_iso) -> StateStore:
        """Open (creating if needed) the database, apply the schema, and return a store."""
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None → manual transaction control via explicit BEGIN/COMMIT.
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        _enforce_schema_version(conn, writable=True)
        return cls(conn, clock=clock)

    @classmethod
    def open_readonly(
        cls, db_path: str | Path, *, clock: Callable[[], str] = _utc_now_iso
    ) -> StateStore:
        """Open an existing database without creating or mutating it (operator ``status``)."""
        path = Path(db_path).resolve()
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        _enforce_schema_version(conn, writable=False)
        return cls(conn, clock=clock)

    def close(self) -> None:
        self._conn.close()

    # --- transactions ---------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A serialized (``BEGIN IMMEDIATE``) write txn: commit on success, roll back on error."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    @contextmanager
    def _writer(self, conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        """Reuse an open transaction's connection, or open a one-shot transaction."""
        if conn is not None:
            yield conn
        else:
            with self.transaction() as own:
                yield own

    # --- tasks ----------------------------------------------------------------------------

    def insert_task(self, row: TaskRow, conn: sqlite3.Connection | None = None) -> None:
        """Insert a task row, or — when the id already exists (a ``rerun``) — refresh the
        registration fields in place. Upsert keeps the FK-referenced row (and its audit history)
        intact; ``reset_task_for_rerun`` has already cleared the per-attempt state."""
        now = self._clock()
        with self._writer(conn) as c:
            c.execute(
                """
                INSERT INTO tasks (
                    task_id, title, status, source_path, branch, slug,
                    created_at, updated_at, validation_passed, validation_reason,
                    refinement_ran, refinement_skip_reason,
                    stage_attempts, test_fix_cycles, review_fix_cycles, fix_iterations,
                    decomposition_enabled, decomposition_accepted, decomposition_reason,
                    subtask_count, active_subtask, subtasks_completed,
                    failure_report_path, cleanup_target_branch, cleanup_completed,
                    cleanup_completed_at, cleanup_last_error, finished_at, interrupted_status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    title = excluded.title,
                    status = excluded.status,
                    source_path = excluded.source_path,
                    validation_passed = excluded.validation_passed,
                    updated_at = excluded.updated_at
                """,
                (
                    row.task_id,
                    row.title,
                    row.status.value,
                    row.source_path,
                    row.branch,
                    row.slug,
                    row.created_at or now,
                    row.updated_at or now,
                    _b(row.validation_passed),
                    row.validation_reason,
                    _b(row.refinement_ran),
                    row.refinement_skip_reason,
                    row.stage_attempts,
                    row.test_fix_cycles,
                    row.review_fix_cycles,
                    row.fix_iterations,
                    _b(row.decomposition_enabled),
                    _b(row.decomposition_accepted),
                    row.decomposition_reason,
                    row.subtask_count,
                    row.active_subtask,
                    row.subtasks_completed,
                    row.failure_report_path,
                    row.cleanup_target_branch,
                    _b(row.cleanup_completed),
                    row.cleanup_completed_at,
                    row.cleanup_last_error,
                    row.finished_at,
                    row.interrupted_status,
                ),
            )

    def get_task(self, task_id: str) -> TaskRow | None:
        cur = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        row = cur.fetchone()
        return _task_from_row(row) if row is not None else None

    def latest_task(self) -> TaskRow | None:
        """Most recently updated task, used when ``status`` has no explicit task id."""
        cur = self._conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 1")
        row = cur.fetchone()
        return _task_from_row(row) if row is not None else None

    def task_id_exists(self, task_id: str) -> bool:
        """True iff ``task_id`` already has a row (used for the §19 ``duplicate_task_id`` check)."""
        cur = self._conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,))
        return cur.fetchone() is not None

    def find_active_tasks(self) -> list[TaskRow]:
        """All tasks owning the processing slot, i.e. in an active (non-terminal) status (§8.2)."""
        active = tuple(s.value for s in Status if s not in _NON_ACTIVE)
        placeholders = ",".join("?" * len(active))
        cur = self._conn.execute(f"SELECT * FROM tasks WHERE status IN ({placeholders})", active)
        return [_task_from_row(r) for r in cur.fetchall()]

    def find_incomplete_cleanup(self) -> list[TaskRow]:
        """Terminal tasks that have a branch but whose terminal cleanup never completed (§13)."""
        terminal = (Status.DONE.value, Status.FAILED.value, Status.MANUAL_ACTION_REQUIRED.value)
        placeholders = ",".join("?" * len(terminal))
        cur = self._conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
            "AND branch IS NOT NULL AND (cleanup_completed IS NULL OR cleanup_completed = 0)",
            terminal,
        )
        return [_task_from_row(r) for r in cur.fetchall()]

    def update_task(
        self, task_id: str, conn: sqlite3.Connection | None = None, **fields: object
    ) -> None:
        """Update arbitrary ``tasks`` columns, always bumping ``updated_at``."""
        if not fields:
            return
        normalized = {k: _normalize(v) for k, v in fields.items()}
        normalized["updated_at"] = self._clock()
        assignments = ", ".join(f"{col} = ?" for col in normalized)
        params = [*normalized.values(), task_id]
        with self._writer(conn) as c:
            c.execute(f"UPDATE tasks SET {assignments} WHERE task_id = ?", params)

    def set_status(
        self, task_id: str, status: Status, conn: sqlite3.Connection | None = None
    ) -> None:
        self.update_task(task_id, conn, status=status.value)

    def reset_task_for_rerun(self, task_id: str, conn: sqlite3.Connection | None = None) -> None:
        """Clear all per-attempt state of a terminal task for a fresh ``rerun`` (one transaction).

        The ``status`` is left at its terminal value on purpose — ``run_task``'s ``insert_task``
        upsert flips it to ``new`` when it re-registers, so an interrupted rerun stays re-runnable
        (a terminal row with ``branch IS NULL`` is invisible to both ``find_active_tasks`` and
        ``find_incomplete_cleanup``). ``branch`` is nulled so the reset row can never be mistaken
        for an interrupted cleanup. The ``subtasks`` and ``publish_operations`` rows are deleted so
        a decomposed rerun re-implements every unit and the publish idempotency layer does not
        short-circuit on the prior attempt's commit/push/PR.
        """
        with self._writer(conn) as c:
            self.update_task(
                task_id,
                c,
                branch=None,
                slug=None,
                validation_passed=None,
                validation_reason=None,
                refinement_ran=None,
                refinement_skip_reason=None,
                stage_attempts=0,
                test_fix_cycles=0,
                review_fix_cycles=0,
                fix_iterations=0,
                decomposition_enabled=None,
                decomposition_accepted=None,
                decomposition_reason=None,
                subtask_count=None,
                active_subtask=None,
                subtasks_completed=0,
                failure_report_path=None,
                cleanup_target_branch=None,
                cleanup_completed=None,
                cleanup_completed_at=None,
                cleanup_last_error=None,
                finished_at=None,
                interrupted_status=None,
                current_node=None,
                flow_run_counters=None,
                flow_fingerprint=None,
            )
            c.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
            c.execute("DELETE FROM node_runs WHERE task_id = ?", (task_id,))
            self.clear_publish_operations(task_id, c)

    def revive_task_for_continue(
        self, task_id: str, stage_status: Status, conn: sqlite3.Connection | None = None
    ) -> None:
        """Flip a terminal task back to the stage it failed at for ``rerun --continue``.

        Keeps ``branch``/``slug``/counters/decomposition/``subtasks``/``publish_operations`` — the
        whole point is to reuse the work already done — and only clears the terminal markers so the
        resume engine drives the task to a fresh terminal + cleanup. This terminal → active flip is
        a deliberate operator-driven, out-of-band transition (no ``assert_transition``), mirroring
        how recovery sets statuses directly.
        """
        self.update_task(
            task_id,
            conn,
            status=stage_status.value,
            finished_at=None,
            cleanup_completed=None,
            cleanup_completed_at=None,
            cleanup_last_error=None,
            cleanup_target_branch=None,
            interrupted_status=None,
        )

    # --- loop counters --------------------------------------------------------------------

    def get_counters(self, task_id: str) -> LoopCounters:
        cur = self._conn.execute(
            "SELECT stage_attempts, test_fix_cycles, review_fix_cycles, fix_iterations "
            "FROM tasks WHERE task_id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(task_id)
        return LoopCounters(
            stage_attempts=row["stage_attempts"],
            test_fix_cycles=row["test_fix_cycles"],
            review_fix_cycles=row["review_fix_cycles"],
            fix_iterations=row["fix_iterations"],
        )

    def save_counters(
        self, task_id: str, counters: LoopCounters, conn: sqlite3.Connection | None = None
    ) -> None:
        self.update_task(
            task_id,
            conn,
            stage_attempts=counters.stage_attempts,
            test_fix_cycles=counters.test_fix_cycles,
            review_fix_cycles=counters.review_fix_cycles,
            fix_iterations=counters.fix_iterations,
        )

    # --- stage_runs / provider_attempts ---------------------------------------------------

    def record_stage_run(self, run: StageRunRow, conn: sqlite3.Connection | None = None) -> int:
        with self._writer(conn) as c:
            cur = c.execute(
                """
                INSERT INTO stage_runs (
                    task_id, stage, subtask_order, status, route_primary, route_fallback,
                    route_source, provider_used, error_class, stage_attempts,
                    commit_sha_before, commit_sha_after, started_at, finished_at,
                    skipped, skip_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run.task_id,
                    run.stage,
                    run.subtask_order,
                    run.status,
                    run.route_primary,
                    run.route_fallback,
                    run.route_source,
                    run.provider_used,
                    run.error_class,
                    run.stage_attempts,
                    run.commit_sha_before,
                    run.commit_sha_after,
                    run.started_at,
                    run.finished_at,
                    1 if run.skipped else 0,
                    run.skip_reason,
                ),
            )
            return int(cur.lastrowid or 0)

    def record_skip(
        self,
        task_id: str,
        stage: str,
        *,
        reason: str,
        subtask_order: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Record a skipped stage in the audit trail (stage-skip control).

        A skipped stage runs no provider, so it carries sentinel route fields (``route_primary`` /
        ``route_source`` are ``NOT NULL``) and ``stage_attempts=0``; ``skipped`` and ``skip_reason``
        hold the audit detail.
        """
        return self.record_stage_run(
            StageRunRow(
                task_id=task_id,
                stage=stage,
                route_primary="(skipped)",
                route_source="skip",
                stage_attempts=0,
                subtask_order=subtask_order,
                status="skipped",
                skipped=True,
                skip_reason=reason,
                started_at=self._clock(),
                finished_at=self._clock(),
            ),
            conn,
        )

    def complete_stage_run(
        self,
        run_id: int,
        *,
        status: str,
        provider_used: str | None,
        error_class: str | None,
        stage_attempts: int,
        finished_at: str,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Finalize a stage run that was reserved before invoking its provider."""
        with self._writer(conn) as c:
            cur = c.execute(
                """
                UPDATE stage_runs
                SET status = ?, provider_used = ?, error_class = ?,
                    stage_attempts = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    provider_used,
                    error_class,
                    stage_attempts,
                    finished_at,
                    run_id,
                ),
            )
            if cur.rowcount != 1:
                raise KeyError(run_id)

    # --- node_runs / flow checkpoint (flow-engine P1.2) -----------------------------------

    def record_node_run(self, run: NodeRunRow, conn: sqlite3.Connection | None = None) -> int:
        """Reserve a flow node-run row before executing the node; returns its id."""
        with self._writer(conn) as c:
            cur = c.execute(
                """
                INSERT INTO node_runs (
                    task_id, node_id, node_kind, subtask_order, status, outcome,
                    route_primary, route_fallback, route_source, provider_used, error_class,
                    stage_attempts, commit_sha_before, commit_sha_after, started_at, finished_at,
                    skipped, skip_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run.task_id,
                    run.node_id,
                    run.node_kind,
                    run.subtask_order,
                    run.status,
                    run.outcome,
                    run.route_primary,
                    run.route_fallback,
                    run.route_source,
                    run.provider_used,
                    run.error_class,
                    run.stage_attempts,
                    run.commit_sha_before,
                    run.commit_sha_after,
                    run.started_at,
                    run.finished_at,
                    1 if run.skipped else 0,
                    run.skip_reason,
                ),
            )
            return int(cur.lastrowid or 0)

    def record_node_skip(
        self,
        task_id: str,
        node_id: str,
        node_kind: str,
        *,
        reason: str,
        subtask_order: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Record a deterministically-skipped node (``when`` false) in the audit trail."""
        now = self._clock()
        return self.record_node_run(
            NodeRunRow(
                task_id=task_id,
                node_id=node_id,
                node_kind=node_kind,
                subtask_order=subtask_order,
                status="skipped",
                skipped=True,
                skip_reason=reason,
                started_at=now,
                finished_at=now,
            ),
            conn,
        )

    def complete_node_run(
        self,
        run_id: int,
        *,
        status: str,
        outcome: str | None,
        provider_used: str | None = None,
        error_class: str | None = None,
        stage_attempts: int = 0,
        finished_at: str,
        commit_sha_after: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Finalize a node run reserved by :meth:`record_node_run`."""
        with self._writer(conn) as c:
            cur = c.execute(
                """
                UPDATE node_runs
                SET status = ?, outcome = ?, provider_used = ?, error_class = ?,
                    stage_attempts = ?, finished_at = ?, commit_sha_after = ?
                WHERE id = ?
                """,
                (
                    status,
                    outcome,
                    provider_used,
                    error_class,
                    stage_attempts,
                    finished_at,
                    commit_sha_after,
                    run_id,
                ),
            )
            if cur.rowcount != 1:
                raise KeyError(run_id)

    def get_node_runs(self, task_id: str) -> list[NodeRunRow]:
        """All node runs for a task in execution order (ascending id)."""
        cur = self._conn.execute(
            "SELECT * FROM node_runs WHERE task_id = ? ORDER BY id ASC", (task_id,)
        )
        return [_node_run_from_row(row) for row in cur.fetchall()]

    def save_flow_checkpoint(
        self,
        task_id: str,
        *,
        current_node: str | None,
        counters_json: str,
        flow_fingerprint: str,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Persist the FlowRunState checkpoint columns on the ``tasks`` row (flow-engine path)."""
        self.update_task(
            task_id,
            conn,
            current_node=current_node,
            flow_run_counters=counters_json,
            flow_fingerprint=flow_fingerprint,
        )

    def get_flow_checkpoint(self, task_id: str) -> tuple[str | None, str | None, str | None]:
        """Return ``(current_node, flow_run_counters_json, flow_fingerprint)`` for a task.

        Any element is ``None`` when the flow-engine path has not run (legacy task or fresh row).
        """
        cur = self._conn.execute(
            "SELECT current_node, flow_run_counters, flow_fingerprint FROM tasks WHERE task_id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(task_id)
        return row["current_node"], row["flow_run_counters"], row["flow_fingerprint"]

    def record_provider_attempt(
        self, attempt: ProviderAttemptRow, conn: sqlite3.Connection | None = None
    ) -> None:
        with self._writer(conn) as c:
            c.execute(
                """
                INSERT INTO provider_attempts (
                    stage_run_id, provider, attempt, status, error_class, exit_code,
                    attempt_dir, started_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt.stage_run_id,
                    attempt.provider,
                    attempt.attempt,
                    attempt.status,
                    attempt.error_class,
                    attempt.exit_code,
                    attempt.attempt_dir,
                    attempt.started_at,
                    attempt.finished_at,
                ),
            )

    # --- check_runs / artifacts -----------------------------------------------------------

    def record_check_run(self, run: CheckRunRow, conn: sqlite3.Connection | None = None) -> None:
        with self._writer(conn) as c:
            c.execute(
                """
                INSERT INTO check_runs (
                    task_id, subtask_order, command, exit_code, timed_out, passed,
                    log_path, started_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    run.task_id,
                    run.subtask_order,
                    run.command,
                    run.exit_code,
                    _b(run.timed_out),
                    _b(run.passed),
                    run.log_path,
                    run.started_at,
                    run.finished_at,
                ),
            )

    def latest_failed_check_log(self, task_id: str, subtask_order: int | None = None) -> str | None:
        """Return the newest failed check log for recovery of a fixing stage."""
        row = self._conn.execute(
            """
            SELECT log_path
            FROM check_runs
            WHERE task_id = ? AND subtask_order IS ? AND passed = 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (task_id, subtask_order),
        ).fetchone()
        return str(row["log_path"]) if row is not None else None

    def register_artifact(
        self, artifact: ArtifactRow, conn: sqlite3.Connection | None = None
    ) -> None:
        """Register an artifact with its checksum (§10). Idempotent: re-registering the same
        ``(task_id, kind, path)`` updates the checksum rather than inserting a duplicate, so a
        resumed run that re-writes ``plan.md`` etc. does not accumulate rows (§13)."""
        now = self._clock()
        with self._writer(conn) as c:
            c.execute(
                "INSERT INTO artifacts (task_id, kind, path, checksum, created_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(task_id, kind, path) DO UPDATE SET checksum = excluded.checksum",
                (artifact.task_id, artifact.kind, artifact.path, artifact.checksum, now),
            )

    # --- publish_operations (idempotency) -------------------------------------------------

    def record_publish_op(self, op: PublishOpRow, conn: sqlite3.Connection | None = None) -> None:
        """Insert or update a publish operation, keyed by (task_id, kind, subtask_order)."""
        now = self._clock()
        subtask = _NO_SUBTASK if op.subtask_order is None else op.subtask_order
        with self._writer(conn) as c:
            c.execute(
                """
                INSERT INTO publish_operations (
                    task_id, kind, subtask_order, fingerprint, result_ref, status, created_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(task_id, kind, subtask_order) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    result_ref = excluded.result_ref,
                    status = excluded.status
                """,
                (op.task_id, op.kind, subtask, op.fingerprint, op.result_ref, op.status, now),
            )

    def get_publish_op(
        self, task_id: str, kind: str, subtask_order: int | None = None
    ) -> PublishOpRow | None:
        subtask = _NO_SUBTASK if subtask_order is None else subtask_order
        cur = self._conn.execute(
            "SELECT * FROM publish_operations WHERE task_id = ? AND kind = ? AND subtask_order = ?",
            (task_id, kind, subtask),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return PublishOpRow(
            task_id=row["task_id"],
            kind=row["kind"],
            fingerprint=row["fingerprint"],
            status=row["status"],
            subtask_order=None if row["subtask_order"] == _NO_SUBTASK else row["subtask_order"],
            result_ref=row["result_ref"],
        )

    def clear_publish_operations(
        self, task_id: str, conn: sqlite3.Connection | None = None
    ) -> None:
        """Delete a task's publish idempotency rows so a fresh ``rerun`` re-commits/pushes/PRs."""
        with self._writer(conn) as c:
            c.execute("DELETE FROM publish_operations WHERE task_id = ?", (task_id,))

    # --- subtasks -------------------------------------------------------------------------

    def insert_subtasks(
        self, rows: Sequence[SubtaskRow], conn: sqlite3.Connection | None = None
    ) -> None:
        """Insert planned subtasks idempotently for a recovery re-run of planning.

        Uncommitted rows may be refreshed. Committed rows retain their status and commit marker, so
        recovery cannot turn completed work back into pending.
        """
        with self._writer(conn) as c:
            for row in rows:
                c.execute(
                    'INSERT INTO subtasks (task_id, "order", slug, title, status, depends_on, '
                    "commit_sha, artifact_path) VALUES (?,?,?,?,?,?,?,?) "
                    'ON CONFLICT(task_id, "order") DO UPDATE SET '
                    "slug = excluded.slug, title = excluded.title, status = excluded.status, "
                    "depends_on = excluded.depends_on, artifact_path = excluded.artifact_path "
                    "WHERE subtasks.commit_sha IS NULL",
                    (
                        row.task_id,
                        row.order,
                        row.slug,
                        row.title,
                        row.status,
                        json.dumps(list(row.depends_on)),
                        row.commit_sha,
                        row.artifact_path,
                    ),
                )

    def get_subtasks(self, task_id: str) -> list[SubtaskRow]:
        cur = self._conn.execute(
            'SELECT * FROM subtasks WHERE task_id = ? ORDER BY "order"', (task_id,)
        )
        return [
            SubtaskRow(
                task_id=r["task_id"],
                order=r["order"],
                slug=r["slug"],
                title=r["title"],
                status=r["status"],
                depends_on=tuple(json.loads(r["depends_on"])),
                commit_sha=r["commit_sha"],
                artifact_path=r["artifact_path"],
            )
            for r in cur.fetchall()
        ]

    def set_subtask_commit(
        self,
        task_id: str,
        order: int,
        commit_sha: str,
        status: str,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._writer(conn) as c:
            c.execute(
                'UPDATE subtasks SET commit_sha = ?, status = ? WHERE task_id = ? AND "order" = ?',
                (commit_sha, status, task_id, order),
            )


# Statuses in which a task does NOT own the processing slot.
_NON_ACTIVE: frozenset[Status] = frozenset(
    {Status.NEW, Status.PENDING, Status.DONE, Status.FAILED, Status.MANUAL_ACTION_REQUIRED}
)


def _b(value: bool | None) -> int | None:
    """Map an optional bool to SQLite's integer 0/1 (or NULL)."""
    if value is None:
        return None
    return 1 if value else 0


def _normalize(value: object) -> object:
    """Coerce a Python value to a SQLite-storable scalar for ``update_task`` kwargs."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, Status):
        return value.value
    return value


def _ob(value: object) -> bool | None:
    """Map an optional SQLite 0/1 back to a bool."""
    if value is None:
        return None
    return bool(value)


def _node_run_from_row(row: sqlite3.Row) -> NodeRunRow:
    return NodeRunRow(
        task_id=row["task_id"],
        node_id=row["node_id"],
        node_kind=row["node_kind"],
        subtask_order=row["subtask_order"],
        status=row["status"],
        outcome=row["outcome"],
        route_primary=row["route_primary"],
        route_fallback=row["route_fallback"],
        route_source=row["route_source"],
        provider_used=row["provider_used"],
        error_class=row["error_class"],
        stage_attempts=row["stage_attempts"],
        commit_sha_before=row["commit_sha_before"],
        commit_sha_after=row["commit_sha_after"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        skipped=bool(row["skipped"]),
        skip_reason=row["skip_reason"],
        id=row["id"],
    )


def _task_from_row(row: sqlite3.Row) -> TaskRow:
    return TaskRow(
        task_id=row["task_id"],
        title=row["title"],
        status=Status(row["status"]),
        source_path=row["source_path"],
        branch=row["branch"],
        slug=row["slug"],
        validation_passed=_ob(row["validation_passed"]),
        validation_reason=row["validation_reason"],
        refinement_ran=_ob(row["refinement_ran"]),
        refinement_skip_reason=row["refinement_skip_reason"],
        stage_attempts=row["stage_attempts"],
        test_fix_cycles=row["test_fix_cycles"],
        review_fix_cycles=row["review_fix_cycles"],
        fix_iterations=row["fix_iterations"],
        decomposition_enabled=_ob(row["decomposition_enabled"]),
        decomposition_accepted=_ob(row["decomposition_accepted"]),
        decomposition_reason=row["decomposition_reason"],
        subtask_count=row["subtask_count"],
        active_subtask=row["active_subtask"],
        subtasks_completed=row["subtasks_completed"],
        failure_report_path=row["failure_report_path"],
        cleanup_target_branch=row["cleanup_target_branch"],
        cleanup_completed=_ob(row["cleanup_completed"]),
        cleanup_completed_at=row["cleanup_completed_at"],
        cleanup_last_error=row["cleanup_last_error"],
        finished_at=row["finished_at"],
        interrupted_status=row["interrupted_status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
