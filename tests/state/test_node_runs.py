"""node_runs table + FlowRunState checkpoint columns (schema v4, flow-engine P1.2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.state_store import NodeRunRow, StateStore, TaskRow


def _store(tmp_path: Path) -> StateStore:
    store = StateStore.open(tmp_path / "state.db")
    store.insert_task(TaskRow(task_id="t1", title="T", status=Status.VALIDATED))
    return store


def _columns(path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_v4_adds_node_runs_table_and_checkpoint_columns(tmp_path: Path) -> None:
    StateStore.open(tmp_path / "state.db").close()
    db = tmp_path / "state.db"
    assert _columns(db, "node_runs")  # table exists
    assert {"current_node", "flow_run_counters", "flow_fingerprint"} <= _columns(db, "tasks")


def test_node_run_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = store.record_node_run(
        NodeRunRow(
            task_id="t1",
            node_id="implementation",
            node_kind="agent",
            route_primary="codex",
            route_source="config",
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    store.complete_node_run(
        rid,
        status="passed",
        outcome="done",
        provider_used="codex",
        stage_attempts=1,
        finished_at="2026-01-01T00:01:00+00:00",
        commit_sha_after="abc123",
    )
    runs = store.get_node_runs("t1")
    assert len(runs) == 1
    row = runs[0]
    assert row.node_id == "implementation"
    assert row.node_kind == "agent"
    assert row.status == "passed"
    assert row.outcome == "done"
    assert row.provider_used == "codex"
    assert row.commit_sha_after == "abc123"
    assert row.skipped is False


def test_node_skip_is_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_node_skip("t1", "refinement", "agent", reason="when needs_refinement false")
    runs = store.get_node_runs("t1")
    assert len(runs) == 1
    assert runs[0].skipped is True
    assert runs[0].status == "skipped"
    assert runs[0].route_primary is None


def test_node_runs_ordered_by_execution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for nid in ("a", "b", "c"):
        store.record_node_run(NodeRunRow(task_id="t1", node_id=nid, node_kind="agent"))
    assert [r.node_id for r in store.get_node_runs("t1")] == ["a", "b", "c"]


def test_flow_checkpoint_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_flow_checkpoint(
        "t1", current_node="review", counters_json='{"test_fix": 2}', flow_fingerprint="fp-1"
    )
    assert store.get_flow_checkpoint("t1") == ("review", '{"test_fix": 2}', "fp-1")


def test_fresh_task_has_empty_flow_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_flow_checkpoint("t1") == (None, None, None)


def test_rerun_reset_clears_flow_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_node_run(NodeRunRow(task_id="t1", node_id="a", node_kind="agent"))
    store.save_flow_checkpoint(
        "t1", current_node="a", counters_json='{"x": 1}', flow_fingerprint="fp-1"
    )
    store.reset_task_for_rerun("t1")
    assert store.get_flow_checkpoint("t1") == (None, None, None)
    assert store.get_node_runs("t1") == []
