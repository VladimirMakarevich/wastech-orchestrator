"""SQLite State Store.

The authoritative persisted state for the pipeline: the ``tasks``, ``node_runs``,
``provider_attempts``, ``check_runs``, ``artifacts``, ``publish_operations``, ``subtasks``,
``evaluations``, ``editing_lineage`` and ``node_lineage`` entities. State transitions are
**transactional** (``BEGIN IMMEDIATE`` … ``COMMIT``) so a crash leaves a consistent prior state and
a restart can reconcile.

**No secrets, tokens, or full process environment are ever written here** — only ids,
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
from wastech_orchestrator.core.state_machine import TERMINAL, Status


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# The SQLite schema version, stamped into ``PRAGMA user_version``. Bumped only when the schema
# changes (not on every release). ``open()`` adopts a ``0`` (brand-new, or pre-versioning) database
# as the current version (``_SCHEMA`` creates it at the current shape); both open paths refuse a
# database stamped newer than this. Because every bump below is **destructive** and ``_migrate``
# only adds columns, a database stamped ``1..DB_SCHEMA_VERSION-1`` cannot be reshaped in place and
# is also refused fail-closed (see :func:`_enforce_schema_version`) — greenfield means there is no
# production data to migrate, so the local ``state.db`` is simply recreated.
#
# The entries below describe how the **current** ``_SCHEMA`` differs from each past version (they
# document the cutover, they are NOT migration steps ``_migrate`` performs):
# v4 (flow-engine P1.2): added the ``node_runs`` per-node audit table and the durable
# :class:`~wastech_orchestrator.core.flow.run_state.FlowRunState` checkpoint columns
# (``tasks.current_node`` / ``tasks.flow_run_counters`` / ``tasks.flow_fingerprint``). These are the
# only **additive** columns ``_migrate`` knows how to add to a ``0``/new database.
# v5 (flow-engine P1.4 cutover): the ``provider_attempts`` FK to ``stage_runs`` was dropped so the
# flow-engine path can store the ``node_runs`` id there (both monotonic).
# v6 (flow-engine P1 Slice 7): the legacy ``stage_runs`` table was dropped (the engine writes
# ``node_runs``) and ``provider_attempts.stage_run_id`` was renamed to ``node_run_id``.
# v7 (flow-engine P1 Slice 7): ``tasks.interrupted_status`` was dropped — the granular statuses it
# stored are gone; ``rerun --continue`` re-enters at the ``current_node`` flow checkpoint.
# v8 (flow-engine P2.1): added the immutable, append-only ``evaluations`` table — the per-verdict
# audit for in-flow evaluators (``in_flow_verdict``) and the constant supervisor layer's per-step /
# final advisory observations (``supervisor_step`` / ``supervisor_final``). A fresh database creates
# it via ``_SCHEMA``; a brand-new (``0``) database adopts it (no additive column to migrate).
# v9 (flow-engine P2.2): added the ``editing_lineage`` table — the durable per-execution-unit
# editing session (provider + raw session id), the **only** place a raw session id is ever stored
# (it is redacted everywhere else). One active editing session per ``(task_id, subtask_order)``;
# resume for Claude/Codex reads it, the author nodes (implementation/fixing) update it. Created on a
# fresh DB by ``_SCHEMA`` (no additive column to migrate).
# v10 (flow-engine P3.3): added the ``node_lineage`` table — the durable own session for a
# ``resume_own_lineage`` node (the research critic), keyed by ``(task_id, node_id, subtask_order)``
# so a node remembers what it flagged across rework rounds. Like ``editing_lineage`` the raw session
# id lives only here. Created on a fresh DB by ``_SCHEMA`` (no additive column to migrate).
# v11 (2026-06-22, audit remediation #17): the always-0 ``tasks.stage_attempts`` column was dropped
# — ``stage_attempts`` is an inherently per-node quantity that lives on ``node_runs`` (the
# task-level integer was never populated). A destructive change (dropped column), so an older
# versioned DB is refused fail-closed and recreated (greenfield). ``node_runs.stage_attempts`` is
# untouched.
# v12 (2026-06-23, checks-monorepo): added ``check_runs.skipped`` — a check whose ``command_sets``
# entry is ``skip_if_unavailable`` and whose toolchain binary is absent is recorded as skipped
# (distinct from a quality failure), which surfaces in the summary/PR and blocks ``git.auto_merge``
# (an incomplete gate is never auto-merged). Created on a fresh DB by ``_SCHEMA``; an older
# versioned DB is refused fail-closed and recreated (greenfield — no production data to migrate).
# v13 (2026-06-27, transient provider recovery): added the **additive** ``tasks.blocked_since``
# column — the wall-clock instant a task first parked as resumable because every allowed provider
# was transiently unavailable (B-lite). Set once on first park, cleared at terminal; the task is
# failed only after it stays parked longer than ``agents.retry.max_blocked_s``. Additive, so
# ``_migrate`` adds it on a brand-new (``0``) database; an older versioned DB is still refused
# fail-closed and recreated (greenfield).
# v14 (2026-07-08, F49): added the **additive** ``tasks.test_fix_total`` /
# ``tasks.review_fix_total`` columns — the cumulative per-loop rework totals for the whole task.
# Unlike the consecutive
# ``*_fix_cycles`` columns (zeroed when the loop converges), these are never reset, so a task that
# succeeded after N reworks records N (the consecutive columns legitimately read 0 at that point).
# Additive, so ``_migrate`` adds them on a brand-new (``0``) database; an older versioned DB is
# refused fail-closed and recreated (greenfield).
# v15 (2026-07-08, multiple editing lineages): added ``editing_lineage.lineage_key`` and widened the
# primary key to ``(task_id, subtask_order, lineage_key)`` so one execution unit can hold more than
# one durable editing session. The lineage key is derived from the flow graph
# (``node.lineage_affinity or node.id``), so an affinity-less ``editing_lineage`` node owns a
# lineage named after itself and a node with ``lineage_affinity: X`` joins lineage ``X``. A widened
# primary key is a destructive change (not an additive column), so an older versioned DB is refused
# fail-closed and recreated (greenfield — no production data to migrate).
# v16 (2026-07-16, normalized usage accounting): added the **additive** normalized-token-usage
# columns — the per-run delta on ``provider_attempts`` (``usage_scope`` / ``usage_input_total`` /
# ``usage_cache_read`` / ``usage_cache_write`` / ``usage_uncached_input`` / ``usage_output_total`` /
# ``usage_reasoning_output`` / ``usage_cost`` / ``usage_delta_status`` / ``provider_usage_raw``) and
# the running-cumulative ``usage_snapshot`` on ``editing_lineage`` and ``node_lineage``. All
# nullable, so ``_migrate`` adds them on a brand-new (``0``) database; an older versioned DB is
# refused fail-closed and recreated (greenfield).
DB_SCHEMA_VERSION = 16


class IncompatibleStateError(Exception):
    """The on-disk ``state.db`` schema version is newer than this orchestrator understands."""


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema (writable open only).

    Idempotent: each column add is guarded by a ``PRAGMA table_info`` check, so this is a no-op on
    a brand-new DB (``_SCHEMA`` already created the columns) and adds only what an older DB lacks.
    """
    task_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
    # v4: the FlowRunState checkpoint columns (flow-engine execution path).
    if "current_node" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN current_node TEXT")
    if "flow_run_counters" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN flow_run_counters TEXT")
    if "flow_fingerprint" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN flow_fingerprint TEXT")
    # v13: the B-lite soft-pause timestamp (transient-provider-failure-recovery).
    if "blocked_since" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN blocked_since TEXT")
    # v14: cumulative per-loop rework totals (F49).
    if "test_fix_total" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN test_fix_total INTEGER NOT NULL DEFAULT 0")
    if "review_fix_total" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN review_fix_total INTEGER NOT NULL DEFAULT 0")
    # v16: normalized token usage — the per-run delta on ``provider_attempts`` and the running
    # cumulative snapshot on the two lineage tables. All nullable, so no defaults.
    attempt_cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(provider_attempts)")}
    for column, decl in (
        ("usage_scope", "TEXT"),
        ("usage_input_total", "INTEGER"),
        ("usage_cache_read", "INTEGER"),
        ("usage_cache_write", "INTEGER"),
        ("usage_uncached_input", "INTEGER"),
        ("usage_output_total", "INTEGER"),
        ("usage_reasoning_output", "INTEGER"),
        ("usage_cost", "REAL"),
        ("usage_delta_status", "TEXT"),
        ("provider_usage_raw", "TEXT"),
    ):
        if column not in attempt_cols:
            conn.execute(f"ALTER TABLE provider_attempts ADD COLUMN {column} {decl}")
    for table in ("editing_lineage", "node_lineage"):
        cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if "usage_snapshot" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN usage_snapshot TEXT")


def _enforce_schema_version(conn: sqlite3.Connection, *, writable: bool) -> None:
    """Verify (and, when ``writable``, migrate + stamp) ``PRAGMA user_version``.

    Two databases are refused fail-loud on both open paths: a **newer** one (beyond this
    orchestrator), and an **older versioned** one (``1 <= v < DB_SCHEMA_VERSION``) — its shape
    predates a destructive change and :func:`_migrate` is additive-only, so it cannot be reshaped in
    place (greenfield: recreate it). Only a brand-new / pre-versioning (``0``) database is adopted:
    on the writable path :func:`_migrate` adds any missing additive columns and the version is
    stamped to ``DB_SCHEMA_VERSION``. The read-only path only verifies these bounds (never mutates).
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > DB_SCHEMA_VERSION:
        raise IncompatibleStateError(
            f"state.db schema version {current} is newer than this orchestrator supports "
            f"({DB_SCHEMA_VERSION}); upgrade wastech-orchestrator or start a fresh workspace"
        )
    if 0 < current < DB_SCHEMA_VERSION:
        # The v5-v7 schema changes were destructive (dropped/renamed tables and columns) and
        # ``_migrate`` only *adds* columns, so an older database cannot be reshaped in place.
        # Greenfield (no production data): refuse fail-closed rather than stamp the current version
        # onto a still-old shape — which used to pass the version gate and then crash on the first
        # write to a reshaped table (e.g. ``provider_attempts.node_run_id``). A brand-new / pre-
        # versioning database (``current == 0``) is created at the current shape by ``_SCHEMA`` and
        # is adopted normally below.
        raise IncompatibleStateError(
            f"state.db schema version {current} predates an incompatible (destructive) schema "
            f"change and cannot be migrated in place; delete the local state.db or start a fresh "
            f"workspace (greenfield — there is no production data to preserve)"
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
    test_fix_cycles INTEGER NOT NULL DEFAULT 0,
    review_fix_cycles INTEGER NOT NULL DEFAULT 0,
    test_fix_total INTEGER NOT NULL DEFAULT 0,
    review_fix_total INTEGER NOT NULL DEFAULT 0,
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
    current_node TEXT,
    flow_run_counters TEXT,
    flow_fingerprint TEXT,
    blocked_since TEXT
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
    -- the ``node_runs`` id this attempt belongs to (a plain monotonic id, not an FK).
    node_run_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT,
    error_class TEXT,
    exit_code INTEGER,
    attempt_dir TEXT,
    started_at TEXT,
    finished_at TEXT,
    -- Normalized token usage for this attempt, as a summation-safe PER-RUN delta (the session's
    -- previous cumulative already subtracted). ``usage_scope`` records how the provider counted
    -- (``session_cumulative`` / ``per_invocation``); ``usage_delta_status`` is ``ok`` or
    -- ``unknown`` (a snapshot smaller than its baseline degrades to raw). ``provider_usage_raw``
    -- keeps the verbatim redacted CLI payload for audit. All nullable (a result-less attempt).
    usage_scope TEXT,
    usage_input_total INTEGER,
    usage_cache_read INTEGER,
    usage_cache_write INTEGER,
    usage_uncached_input INTEGER,
    usage_output_total INTEGER,
    usage_reasoning_output INTEGER,
    usage_cost REAL,
    usage_delta_status TEXT,
    provider_usage_raw TEXT
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
    skipped INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    node_id TEXT,
    source_node_run_id INTEGER,
    subtask_order INTEGER,
    kind TEXT NOT NULL,
    verdict TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS editing_lineage (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    subtask_order INTEGER NOT NULL DEFAULT -1,
    lineage_key TEXT NOT NULL,
    provider TEXT NOT NULL,
    raw_session_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- The session's running cumulative normalized usage (JSON), rewritten on every visit so a later
    -- resume can subtract it to get a per-run delta. Only cumulative-scope providers (Codex) write
    -- it; per-invocation providers (Claude) leave it NULL (nothing to subtract).
    usage_snapshot TEXT,
    PRIMARY KEY (task_id, subtask_order, lineage_key)
);

CREATE TABLE IF NOT EXISTS node_lineage (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    node_id TEXT NOT NULL,
    subtask_order INTEGER NOT NULL DEFAULT -1,
    provider TEXT NOT NULL,
    raw_session_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- The session's running cumulative normalized usage (JSON); see ``editing_lineage`` above.
    usage_snapshot TEXT,
    PRIMARY KEY (task_id, node_id, subtask_order)
);
"""


# --- Row dataclasses (mirroring the entities) ---------------------------------------------


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
    test_fix_cycles: int = 0
    review_fix_cycles: int = 0
    test_fix_total: int = 0
    review_fix_total: int = 0
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
    created_at: str | None = None
    updated_at: str | None = None
    # ISO instant of the first B-lite soft-pause (every provider transiently unavailable); cleared
    # at terminal. None = not parked. The ceiling is measured from here (total parked wall-clock).
    blocked_since: str | None = None


@dataclass(frozen=True)
class NodeRunRow:
    """One flow-graph node execution — the per-node audit trail row (flow-engine).

    ``node_kind`` is one of ``agent``/``evaluator``/``checks``/``hitl``/``publish``; ``outcome`` is
    the engine outcome (``accept``/``rework``/``pass``/``fail``/``done``/``route:*``). ``route_*``
    are ``None`` for non-agent nodes. A skipped node carries ``skipped=True`` + ``skip_reason`` and
    no provider data (see :meth:`record_node_skip`).

    ``commit_sha_after`` is the node's **result reference**: the commit SHA a code/audit/subtask
    node produced, or — for a ``publish`` node opening a PR — the PR URL (the authoritative PR URL
    also lives in ``publish_operations``; this is the audit-trail copy). The name reads "commit sha"
    for the common case; the publish overload is intentional and documented here.
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
    node_run_id: int
    provider: str
    attempt: int
    status: str | None = None
    error_class: str | None = None
    exit_code: int | None = None
    attempt_dir: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    # Normalized token usage as a summation-safe PER-RUN delta (see the ``provider_attempts`` DDL).
    # Kept as plain scalars + a JSON blob so this storage layer stays independent of the provider
    # domain types; the orchestrator maps its ``NormalizedUsage`` onto these before writing.
    usage_scope: str | None = None
    usage_input_total: int | None = None
    usage_cache_read: int | None = None
    usage_cache_write: int | None = None
    usage_uncached_input: int | None = None
    usage_output_total: int | None = None
    usage_reasoning_output: int | None = None
    usage_cost: float | None = None
    usage_delta_status: str | None = None
    provider_usage_raw: str | None = None


@dataclass(frozen=True)
class CheckRunRow:
    task_id: str
    command: str
    passed: bool
    log_path: str
    subtask_order: int | None = None
    exit_code: int | None = None
    timed_out: bool = False
    # A check skipped because its ``skip_if_unavailable`` set's toolchain binary was absent (never a
    # quality pass/fail). Distinct from ``passed`` so the gate stays loud and auto-merge is gated.
    skipped: bool = False
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


@dataclass(frozen=True)
class EvaluationRow:
    """One immutable, append-only evaluation record (flow-engine P2.1).

    Carries three kinds (``kind``): an in-flow evaluator's ``in_flow_verdict`` (``accept`` /
    ``rework`` with its findings, namespaced by ``source_node_run_id``); and the supervisor layer's
    advisory ``supervisor_step`` (one per completed node, namespaced by
    ``(subtask_order, source_node_run_id)``) and ``supervisor_final`` (one per whole task). The
    supervisor never routes, so its ``verdict`` is always ``advisory``. The in-flow per-instance
    rework limit is derived by **counting** ``rework`` verdicts — there is no mutable counter.
    ``node_id`` is the in-flow evaluator node, or ``None`` for the supervisor layer (not a node).
    """

    task_id: str
    kind: str  # in_flow_verdict | supervisor_step | supervisor_final
    verdict: str  # accept | rework (in-flow); advisory (supervisor — never routes)
    findings_json: str = "[]"
    node_id: str | None = None
    source_node_run_id: int | None = None
    subtask_order: int | None = None
    created_at: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class EditingLineageRow:
    """A durable editing session for one execution unit (flow-engine P2.2).

    Keyed ``(task_id, subtask_order, lineage_key)`` — one execution unit
    (``contracts.ExecutionUnit`` = ``(task_id, subtask_order)``) can hold more than one durable
    editing session, one per ``lineage_key``. The key is derived from the flow graph
    (``node.lineage_affinity or node.id``): an affinity-less ``editing_lineage`` node owns a lineage
    named after itself, and a node with ``lineage_affinity: X`` joins lineage ``X``.
    ``raw_session_id`` is the provider's real session id — it **never** leaves ``state.db`` (it is
    redacted in every artifact/log/argv). ``provider`` binds the lineage to the provider that
    produced it: a node resumes it only when its resolved provider matches (you cannot resume a
    Claude session on Codex).
    """

    task_id: str
    lineage_key: str
    provider: str  # claude | codex
    raw_session_id: str
    subtask_order: int | None = None
    updated_at: str | None = None
    # The session's running cumulative normalized usage (JSON), or ``None`` for a per-invocation
    # provider. Read as the baseline the orchestrator subtracts from a resume's cumulative.
    usage_snapshot: str | None = None


@dataclass(frozen=True)
class NodeLineageRow:
    """The durable own session for a ``resume_own_lineage`` node (flow-engine P3.3).

    Keyed by ``(task_id, node_id, subtask_order)`` — the research critic keeps its own session
    across rework rounds so it remembers what it already flagged. Like :class:`EditingLineageRow`,
    ``raw_session_id`` is the provider's real session id and **never** leaves ``state.db``;
    ``provider`` binds it so the node resumes only when its resolved provider matches.
    """

    task_id: str
    node_id: str
    provider: str  # claude | codex
    raw_session_id: str
    subtask_order: int | None = None
    updated_at: str | None = None
    # The session's running cumulative normalized usage (JSON); see ``EditingLineageRow``.
    usage_snapshot: str | None = None


# ``publish_operations`` uses -1 as the "no subtask" sentinel so the UNIQUE constraint works
# (SQLite treats every NULL as distinct, which would defeat idempotency).
_NO_SUBTASK = -1


class StateStore:
    """A thin transactional wrapper over a single SQLite database file."""

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
                    test_fix_cycles, review_fix_cycles,
                    test_fix_total, review_fix_total, fix_iterations,
                    decomposition_enabled, decomposition_accepted, decomposition_reason,
                    subtask_count, active_subtask, subtasks_completed,
                    failure_report_path, cleanup_target_branch, cleanup_completed,
                    cleanup_completed_at, cleanup_last_error, finished_at
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
                    row.test_fix_cycles,
                    row.review_fix_cycles,
                    row.test_fix_total,
                    row.review_fix_total,
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
        """True iff ``task_id`` already has a row (used for the ``duplicate_task_id`` check)."""
        cur = self._conn.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,))
        return cur.fetchone() is not None

    def find_active_tasks(self) -> list[TaskRow]:
        """All tasks owning the processing slot, i.e. in an active (non-terminal) status."""
        active = tuple(s.value for s in Status if s not in _NON_ACTIVE)
        placeholders = ",".join("?" * len(active))
        cur = self._conn.execute(f"SELECT * FROM tasks WHERE status IN ({placeholders})", active)
        return [_task_from_row(r) for r in cur.fetchall()]

    def find_incomplete_cleanup(self) -> list[TaskRow]:
        """Terminal tasks that have a branch but whose terminal cleanup never completed."""
        terminal = (Status.DONE.value, Status.FAILED.value, Status.MANUAL_ACTION_REQUIRED.value)
        placeholders = ",".join("?" * len(terminal))
        cur = self._conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
            "AND branch IS NOT NULL AND (cleanup_completed IS NULL OR cleanup_completed = 0)",
            terminal,
        )
        return [_task_from_row(r) for r in cur.fetchall()]

    def recent_tasks(self, limit: int) -> list[TaskRow]:
        """The last ``limit`` terminal tasks (done / failed / manual_action_required) by recency.

        The read-only ``worc list`` overview uses this for its "recent" section (and ``worc top``
        reuses it). ``updated_at`` is an ISO-8601 string, so a lexical DESC sort is chronological.
        """
        terminal = tuple(s.value for s in TERMINAL)
        placeholders = ",".join("?" * len(terminal))
        cur = self._conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
            "ORDER BY updated_at DESC LIMIT ?",
            (*terminal, limit),
        )
        return [_task_from_row(r) for r in cur.fetchall()]

    def all_tasks(self) -> list[TaskRow]:
        """Every task, most recently updated first (backs ``worc list --all``)."""
        cur = self._conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC")
        return [_task_from_row(r) for r in cur.fetchall()]

    def find_open_pr_tasks(self) -> list[TaskRow]:
        """Tasks whose orchestrator PR is open and un-merged — the ``worc prs`` population.

        A task qualifies when it has a completed ``pr`` publish op (a PR was created) and **no**
        completed ``pr_merge`` op (it has not been merged through the orchestrator). Read-only. The
        ``'pr'``/``'pr_merge'``/``'completed'`` literals are the ``publish_operations`` kind/status
        values owned by ``GitManager`` (``KIND_PR`` / ``KIND_PR_MERGE`` / ``_STATUS_COMPLETED``);
        spelled here rather than imported to avoid a state_store→git_manager import cycle.
        """
        cur = self._conn.execute(
            "SELECT * FROM tasks t WHERE EXISTS ("
            "  SELECT 1 FROM publish_operations po"
            "  WHERE po.task_id = t.task_id AND po.kind = 'pr' AND po.status = 'completed'"
            ") AND NOT EXISTS ("
            "  SELECT 1 FROM publish_operations po"
            "  WHERE po.task_id = t.task_id AND po.kind = 'pr_merge' AND po.status = 'completed'"
            ") ORDER BY t.updated_at DESC"
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
                test_fix_cycles=0,
                review_fix_cycles=0,
                test_fix_total=0,
                review_fix_total=0,
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
                current_node=None,
                flow_run_counters=None,
                flow_fingerprint=None,
            )
            c.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
            c.execute("DELETE FROM node_runs WHERE task_id = ?", (task_id,))
            self.clear_publish_operations(task_id, c)
            self.clear_editing_lineage(task_id, c)

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
        )

    # --- loop counters --------------------------------------------------------------------

    def get_counters(self, task_id: str) -> LoopCounters:
        cur = self._conn.execute(
            "SELECT test_fix_cycles, review_fix_cycles, test_fix_total, review_fix_total, "
            "fix_iterations FROM tasks WHERE task_id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(task_id)
        return LoopCounters(
            test_fix_cycles=row["test_fix_cycles"],
            review_fix_cycles=row["review_fix_cycles"],
            test_fix_total=row["test_fix_total"],
            review_fix_total=row["review_fix_total"],
            fix_iterations=row["fix_iterations"],
        )

    def save_counters(
        self, task_id: str, counters: LoopCounters, conn: sqlite3.Connection | None = None
    ) -> None:
        self.update_task(
            task_id,
            conn,
            test_fix_cycles=counters.test_fix_cycles,
            review_fix_cycles=counters.review_fix_cycles,
            test_fix_total=counters.test_fix_total,
            review_fix_total=counters.review_fix_total,
            fix_iterations=counters.fix_iterations,
        )

    # --- node_runs / flow checkpoint (flow-engine) ----------------------------------------

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
        fix_iterations: int,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Persist the FlowRunState checkpoint columns on the ``tasks`` row (flow-engine path).

        ``fix_iterations`` mirrors the engine's single global fix counter into the operator-facing
        ``tasks.fix_iterations`` column on every checkpoint, so live ``status`` reflects the loops
        the engine has run (the legacy column is otherwise never advanced on the engine path).
        """
        self.update_task(
            task_id,
            conn,
            current_node=current_node,
            flow_run_counters=counters_json,
            flow_fingerprint=flow_fingerprint,
            fix_iterations=fix_iterations,
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
                    node_run_id, provider, attempt, status, error_class, exit_code,
                    attempt_dir, started_at, finished_at,
                    usage_scope, usage_input_total, usage_cache_read, usage_cache_write,
                    usage_uncached_input, usage_output_total, usage_reasoning_output, usage_cost,
                    usage_delta_status, provider_usage_raw
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt.node_run_id,
                    attempt.provider,
                    attempt.attempt,
                    attempt.status,
                    attempt.error_class,
                    attempt.exit_code,
                    attempt.attempt_dir,
                    attempt.started_at,
                    attempt.finished_at,
                    attempt.usage_scope,
                    attempt.usage_input_total,
                    attempt.usage_cache_read,
                    attempt.usage_cache_write,
                    attempt.usage_uncached_input,
                    attempt.usage_output_total,
                    attempt.usage_reasoning_output,
                    attempt.usage_cost,
                    attempt.usage_delta_status,
                    attempt.provider_usage_raw,
                ),
            )

    def get_provider_attempts(self, node_run_id: int) -> list[ProviderAttemptRow]:
        """The provider attempts recorded for one node run, ordered by attempt.

        Exposes the normalized per-run usage delta for inspection. ``provider_attempts`` has no
        ``task_id`` of its own, so a task-level rollup must join through ``node_runs.id``.
        """
        cur = self._conn.execute(
            """
            SELECT node_run_id, provider, attempt, status, error_class, exit_code, attempt_dir,
                   started_at, finished_at, usage_scope, usage_input_total, usage_cache_read,
                   usage_cache_write, usage_uncached_input, usage_output_total,
                   usage_reasoning_output, usage_cost, usage_delta_status, provider_usage_raw
            FROM provider_attempts WHERE node_run_id = ? ORDER BY attempt
            """,
            (node_run_id,),
        )
        return [
            ProviderAttemptRow(
                node_run_id=row["node_run_id"],
                provider=row["provider"],
                attempt=row["attempt"],
                status=row["status"],
                error_class=row["error_class"],
                exit_code=row["exit_code"],
                attempt_dir=row["attempt_dir"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                usage_scope=row["usage_scope"],
                usage_input_total=row["usage_input_total"],
                usage_cache_read=row["usage_cache_read"],
                usage_cache_write=row["usage_cache_write"],
                usage_uncached_input=row["usage_uncached_input"],
                usage_output_total=row["usage_output_total"],
                usage_reasoning_output=row["usage_reasoning_output"],
                usage_cost=row["usage_cost"],
                usage_delta_status=row["usage_delta_status"],
                provider_usage_raw=row["provider_usage_raw"],
            )
            for row in cur.fetchall()
        ]

    # --- check_runs / artifacts -----------------------------------------------------------

    def record_check_run(self, run: CheckRunRow, conn: sqlite3.Connection | None = None) -> None:
        with self._writer(conn) as c:
            c.execute(
                """
                INSERT INTO check_runs (
                    task_id, subtask_order, command, exit_code, timed_out, passed,
                    log_path, skipped, started_at, finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run.task_id,
                    run.subtask_order,
                    run.command,
                    run.exit_code,
                    _b(run.timed_out),
                    _b(run.passed),
                    run.log_path,
                    _b(run.skipped),
                    run.started_at,
                    run.finished_at,
                ),
            )

    def latest_failed_check_log(self, task_id: str, subtask_order: int | None = None) -> str | None:
        """Return the newest *quality*-failed check log for recovery of a fixing stage.

        Excludes skipped runs (``passed = 0`` but not a quality failure) so the fixing loop never
        points at a "toolchain absent" log.
        """
        row = self._conn.execute(
            """
            SELECT log_path
            FROM check_runs
            WHERE task_id = ? AND subtask_order IS ? AND passed = 0 AND skipped = 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (task_id, subtask_order),
        ).fetchone()
        return str(row["log_path"]) if row is not None else None

    def task_had_skipped_checks(self, task_id: str) -> bool:
        """Whether any check for the task was skipped (toolchain absent) — an incomplete gate.

        The orchestrator consults this before ``git.auto_merge``: a partial skip can still pass the
        checks node but must never be auto-merged (the gate did not fully run).
        """
        row = self._conn.execute(
            "SELECT 1 FROM check_runs WHERE task_id = ? AND skipped = 1 LIMIT 1",
            (task_id,),
        ).fetchone()
        return row is not None

    def register_artifact(
        self, artifact: ArtifactRow, conn: sqlite3.Connection | None = None
    ) -> None:
        """Register an artifact with its checksum. Idempotent: re-registering the same
        ``(task_id, kind, path)`` updates the checksum rather than inserting a duplicate, so a
        resumed run that re-writes ``plan.md`` etc. does not accumulate rows."""
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

    # --- evaluations (immutable, append-only) ---------------------------------------------

    def record_evaluation(self, row: EvaluationRow, conn: sqlite3.Connection | None = None) -> int:
        """Append one immutable evaluation row (in-flow verdict or supervisor observation). Returns
        its id. The table is append-only — there is no update/delete (audit + recovery)."""
        now = self._clock()
        with self._writer(conn) as c:
            cur = c.execute(
                """
                INSERT INTO evaluations (
                    task_id, node_id, source_node_run_id, subtask_order, kind, verdict,
                    findings_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    row.task_id,
                    row.node_id,
                    row.source_node_run_id,
                    row.subtask_order,
                    row.kind,
                    row.verdict,
                    row.findings_json,
                    row.created_at or now,
                ),
            )
            return int(cur.lastrowid or 0)

    def get_evaluations(self, task_id: str) -> list[EvaluationRow]:
        """All evaluation rows for a task in append order (ascending id)."""
        cur = self._conn.execute(
            "SELECT * FROM evaluations WHERE task_id = ? ORDER BY id ASC", (task_id,)
        )
        return [_evaluation_from_row(r) for r in cur.fetchall()]

    def count_rework_verdicts(
        self, task_id: str, *, node_id: str | None = None, subtask_order: int | None = None
    ) -> int:
        """Count applied in-flow ``rework`` verdicts — the per-instance rework limit derives from
        this count, not a mutable counter. Scoped to ``node_id`` /
        ``subtask_order`` when given (``subtask_order`` matched with ``IS`` so ``NULL`` works)."""
        sql = (
            "SELECT COUNT(*) FROM evaluations "
            "WHERE task_id = ? AND kind = 'in_flow_verdict' AND verdict = 'rework'"
        )
        params: list[object] = [task_id]
        if node_id is not None:
            sql += " AND node_id = ?"
            params.append(node_id)
        if subtask_order is not None:
            sql += " AND subtask_order = ?"
            params.append(subtask_order)
        row = self._conn.execute(sql, params).fetchone()
        return int(row[0]) if row is not None else 0

    # --- editing_lineage (durable sessions) -----------------------------------------------

    def get_editing_lineage(
        self, task_id: str, lineage_key: str, subtask_order: int | None = None
    ) -> EditingLineageRow | None:
        """The active editing session for one lineage of an execution unit, or ``None`` (P2.2)."""
        subtask = _NO_SUBTASK if subtask_order is None else subtask_order
        cur = self._conn.execute(
            "SELECT provider, raw_session_id, updated_at, usage_snapshot FROM editing_lineage "
            "WHERE task_id = ? AND subtask_order = ? AND lineage_key = ?",
            (task_id, subtask, lineage_key),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return EditingLineageRow(
            task_id=task_id,
            lineage_key=lineage_key,
            provider=row["provider"],
            raw_session_id=row["raw_session_id"],
            subtask_order=subtask_order,
            updated_at=row["updated_at"],
            usage_snapshot=row["usage_snapshot"],
        )

    def upsert_editing_lineage(
        self, row: EditingLineageRow, conn: sqlite3.Connection | None = None
    ) -> None:
        """Insert or replace one editing session (one per ``(unit, lineage_key)``)."""
        now = self._clock()
        subtask = _NO_SUBTASK if row.subtask_order is None else row.subtask_order
        with self._writer(conn) as c:
            c.execute(
                """
                INSERT INTO editing_lineage (
                    task_id, subtask_order, lineage_key, provider, raw_session_id, updated_at,
                    usage_snapshot
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(task_id, subtask_order, lineage_key) DO UPDATE SET
                    provider = excluded.provider,
                    raw_session_id = excluded.raw_session_id,
                    updated_at = excluded.updated_at,
                    usage_snapshot = excluded.usage_snapshot
                """,
                (
                    row.task_id,
                    subtask,
                    row.lineage_key,
                    row.provider,
                    row.raw_session_id,
                    row.updated_at or now,
                    row.usage_snapshot,
                ),
            )

    def clear_editing_lineage(self, task_id: str, conn: sqlite3.Connection | None = None) -> None:
        """Delete all of a task's editing sessions so a fresh ``rerun`` starts new provider
        sessions (clears every lineage of the task, keyed by ``task_id`` alone)."""
        with self._writer(conn) as c:
            c.execute("DELETE FROM editing_lineage WHERE task_id = ?", (task_id,))
            c.execute("DELETE FROM node_lineage WHERE task_id = ?", (task_id,))

    # --- node_lineage (resume_own_lineage durable sessions, P3.3) -------------------------

    def get_node_lineage(
        self, task_id: str, node_id: str, subtask_order: int | None = None
    ) -> NodeLineageRow | None:
        """The durable own session for a ``resume_own_lineage`` node, or ``None`` if none yet."""
        subtask = _NO_SUBTASK if subtask_order is None else subtask_order
        cur = self._conn.execute(
            "SELECT provider, raw_session_id, updated_at, usage_snapshot FROM node_lineage "
            "WHERE task_id = ? AND node_id = ? AND subtask_order = ?",
            (task_id, node_id, subtask),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return NodeLineageRow(
            task_id=task_id,
            node_id=node_id,
            provider=row["provider"],
            raw_session_id=row["raw_session_id"],
            subtask_order=subtask_order,
            updated_at=row["updated_at"],
            usage_snapshot=row["usage_snapshot"],
        )

    def upsert_node_lineage(
        self, row: NodeLineageRow, conn: sqlite3.Connection | None = None
    ) -> None:
        """Insert or replace the durable own session for a ``resume_own_lineage`` node."""
        now = self._clock()
        subtask = _NO_SUBTASK if row.subtask_order is None else row.subtask_order
        with self._writer(conn) as c:
            c.execute(
                """
                INSERT INTO node_lineage (
                    task_id, node_id, subtask_order, provider, raw_session_id, updated_at,
                    usage_snapshot
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(task_id, node_id, subtask_order) DO UPDATE SET
                    provider = excluded.provider,
                    raw_session_id = excluded.raw_session_id,
                    updated_at = excluded.updated_at,
                    usage_snapshot = excluded.usage_snapshot
                """,
                (
                    row.task_id,
                    row.node_id,
                    subtask,
                    row.provider,
                    row.raw_session_id,
                    row.updated_at or now,
                    row.usage_snapshot,
                ),
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


def _evaluation_from_row(row: sqlite3.Row) -> EvaluationRow:
    return EvaluationRow(
        task_id=row["task_id"],
        kind=row["kind"],
        verdict=row["verdict"],
        findings_json=row["findings_json"],
        node_id=row["node_id"],
        source_node_run_id=row["source_node_run_id"],
        subtask_order=row["subtask_order"],
        created_at=row["created_at"],
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
        test_fix_cycles=row["test_fix_cycles"],
        review_fix_cycles=row["review_fix_cycles"],
        test_fix_total=row["test_fix_total"],
        review_fix_total=row["review_fix_total"],
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
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        blocked_since=row["blocked_since"],
    )
