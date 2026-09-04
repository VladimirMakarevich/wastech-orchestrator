"""node_runs table + FlowRunState checkpoint columns (schema v4)."""

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


def test_reconcile_open_node_runs_closes_orphans(tmp_path: Path) -> None:
    # A hard operator stop strands a node run 'running'/finished_at NULL; a terminal
    # transition closes it to 'aborted' with the reason and returns the pre-update rows so the
    # caller can bill the killed provider attempt from ``route_primary``.
    store = _store(tmp_path)
    orphan = store.record_node_run(
        NodeRunRow(
            task_id="t1",
            node_id="implementation",
            node_kind="agent",
            route_primary="codex",
            status="running",
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    done = store.record_node_run(NodeRunRow(task_id="t1", node_id="planning", node_kind="agent"))
    store.complete_node_run(
        done, status="succeeded", outcome="done", finished_at="2026-01-01T00:00:30+00:00"
    )
    closed = store.reconcile_open_node_runs(
        "t1",
        finished_at="2026-01-01T00:05:00+00:00",
        error_class="cancelled",
        skip_reason="killed by operator",
    )
    assert [r.id for r in closed] == [orphan]
    assert closed[0].route_primary == "codex"
    by_id = {r.id: r for r in store.get_node_runs("t1")}
    assert by_id[orphan].status == "aborted"
    assert by_id[orphan].finished_at == "2026-01-01T00:05:00+00:00"
    assert by_id[orphan].skip_reason == "killed by operator"
    assert by_id[orphan].error_class == "cancelled"
    # The already-finalized node is untouched (only 'running'/finished_at NULL rows are closed).
    assert by_id[done].status == "succeeded"
    assert by_id[done].finished_at == "2026-01-01T00:00:30+00:00"


def test_reconcile_open_node_runs_noop_when_all_finished(tmp_path: Path) -> None:
    # A clean terminal (every node finalized) reconciles nothing — empty list, no writes.
    store = _store(tmp_path)
    rid = store.record_node_run(NodeRunRow(task_id="t1", node_id="a", node_kind="agent"))
    store.complete_node_run(rid, status="succeeded", outcome="done", finished_at="t-end")
    assert store.reconcile_open_node_runs("t1", finished_at="t-x") == []
    assert store.get_node_runs("t1")[0].status == "succeeded"


def test_flow_checkpoint_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save_flow_checkpoint(
        "t1",
        current_node="review",
        counters_json='{"test_fix": 2}',
        flow_fingerprint="fp-1",
        fix_iterations=0,
    )
    assert store.get_flow_checkpoint("t1") == ("review", '{"test_fix": 2}', "fp-1")


def test_fresh_task_has_empty_flow_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_flow_checkpoint("t1") == (None, None, None)


def test_rerun_reset_clears_flow_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_node_run(NodeRunRow(task_id="t1", node_id="a", node_kind="agent"))
    store.save_flow_checkpoint(
        "t1",
        current_node="a",
        counters_json='{"x": 1}',
        flow_fingerprint="fp-1",
        fix_iterations=0,
    )
    store.reset_task_for_rerun("t1")
    assert store.get_flow_checkpoint("t1") == (None, None, None)
    assert store.get_node_runs("t1") == []


def test_has_prior_provider_run_answers_only_for_settled_runs_on_this_unit(tmp_path: Path) -> None:
    # The fact a continuation prompt turns on. Everything here is about which rows may answer yes.
    store = _store(tmp_path)

    def _run(node_id: str, subtask: int | None) -> int:
        return store.record_node_run(
            NodeRunRow(
                task_id="t1",
                node_id=node_id,
                node_kind="agent",
                subtask_order=subtask,
                route_primary="codex",
                route_source="config",
                started_at="2026-01-01T00:00:00+00:00",
            )
        )

    first = _run("fixing", None)
    current = _run("fixing", None)

    # Only the caller's own reserved row exists, and it carries no provider yet.
    assert not store.has_prior_provider_run("t1", "fixing", None, "codex", exclude_run_id=current)

    store.complete_node_run(
        first,
        status="passed",
        outcome="done",
        provider_used="codex",
        stage_attempts=1,
        finished_at="2026-01-01T00:01:00+00:00",
    )
    assert store.has_prior_provider_run("t1", "fixing", None, "codex", exclude_run_id=current)
    # A run on the other provider is not this session's history.
    assert not store.has_prior_provider_run("t1", "fixing", None, "claude", exclude_run_id=current)
    # Another node's rounds are not this one's, however warm the shared session is.
    assert not store.has_prior_provider_run(
        "t1", "documentation", None, "codex", exclude_run_id=current
    )

    # A decomposed task keeps its units apart: NULL matches only the root-level rows.
    sub = _run("fixing", 2)
    store.complete_node_run(
        sub,
        status="passed",
        outcome="done",
        provider_used="codex",
        stage_attempts=1,
        finished_at="2026-01-01T00:02:00+00:00",
    )
    assert not store.has_prior_provider_run("t1", "fixing", 1, "codex", exclude_run_id=current)
    assert store.has_prior_provider_run("t1", "fixing", 2, "codex", exclude_run_id=current)


def test_a_skipped_node_never_answers_the_continuation_predicate(tmp_path: Path) -> None:
    # A `when`-skipped node has a node_runs row but was never sent to a provider, so it cannot
    # make a later round look like a continuation of a conversation that never happened.
    store = _store(tmp_path)
    store.record_node_skip("t1", "fixing", "agent", reason="when predicate false")
    assert not store.has_prior_provider_run("t1", "fixing", None, "codex", exclude_run_id=0)


def test_continue_keeps_the_history_a_full_rerun_clears(tmp_path: Path) -> None:
    # The two rerun modes read differently on purpose, and a continuation prompt follows that.
    # `--continue` exists to reuse the work already done, so a re-entered node is still continuing
    # its own conversation. A plain rerun is the "start over" path: it deletes the node runs, so
    # the same node states its rules in full again.
    store = _store(tmp_path)
    run_id = store.record_node_run(
        NodeRunRow(
            task_id="t1",
            node_id="fixing",
            node_kind="agent",
            route_primary="codex",
            route_source="config",
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    store.complete_node_run(
        run_id,
        status="passed",
        outcome="done",
        provider_used="codex",
        stage_attempts=1,
        finished_at="2026-01-01T00:01:00+00:00",
    )
    assert store.has_prior_provider_run("t1", "fixing", None, "codex", exclude_run_id=0)

    store.revive_task_for_continue("t1", Status.RUNNING)
    assert store.has_prior_provider_run("t1", "fixing", None, "codex", exclude_run_id=0)

    store.reset_task_for_rerun("t1")
    assert not store.has_prior_provider_run("t1", "fixing", None, "codex", exclude_run_id=0)
