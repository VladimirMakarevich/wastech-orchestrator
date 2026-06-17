"""StateStoreRunRecorder + resume hydration (flow-engine P1.2)."""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.core.flow.recorder import StateStoreRunRecorder, hydrate_run_state
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import AgentNode
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.state_store import NodeRunRow, StateStore, TaskRow


def _store(tmp_path: Path) -> StateStore:
    store = StateStore.open(tmp_path / "state.db")
    store.insert_task(TaskRow(task_id="t1", title="T", status=Status.RUNNING))
    return store


def _agent(node_id: str) -> AgentNode:
    return AgentNode(id=node_id, kind="agent", role_file=f"roles/{node_id}.md")


def test_recorder_records_node_skip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recorder = StateStoreRunRecorder(store, "t1", artifacts_root=tmp_path)
    recorder.record_skip(_agent("refinement"), reason="when false", subtask_order=None)
    runs = store.get_node_runs("t1")
    assert [r.node_id for r in runs] == ["refinement"]
    assert runs[0].skipped is True


def test_recorder_saves_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recorder = StateStoreRunRecorder(store, "t1", artifacts_root=tmp_path)
    state = FlowRunState(
        flow_fingerprint="fp", current_node="review", loop_counters={"test_fix": 1}
    )
    recorder.save_checkpoint(state)
    current_node, counters_json, fingerprint = store.get_flow_checkpoint("t1")
    assert current_node == "review"
    assert fingerprint == "fp"
    assert json.loads(counters_json or "{}") == {"test_fix": 1}


def test_recorder_writes_flow_neutral_failure_report(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recorder = StateStoreRunRecorder(store, "t1", artifacts_root=tmp_path)
    state = FlowRunState(
        flow_fingerprint="fp", current_node="review", loop_counters={"global_fix_iterations": 30}
    )
    path = recorder.write_failure_report(
        node_id="review", loop="review_fix", limit_name="max_fix_cycles", run_state=state
    )
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    assert report["node_id"] == "review"
    assert report["loop"] == "review_fix"
    assert report["limit_exhausted"] == "max_fix_cycles"
    assert report["counters"] == {"global_fix_iterations": 30}
    # Implementation-specific sections stay empty for a flow-neutral report.
    assert report["last_check_log"] is None
    assert report["last_review_findings"] == []
    assert store.get_task("t1").failure_report_path == path  # type: ignore[union-attr]


def test_hydrate_returns_none_when_flow_never_ran(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert hydrate_run_state(store, "t1") is None


def test_hydrate_rebuilds_checkpoint_from_saved_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recorder = StateStoreRunRecorder(store, "t1", artifacts_root=tmp_path)
    recorder.record_skip(_agent("refinement"), reason="when false", subtask_order=None)
    store.record_node_run(  # an executed node in the trace
        NodeRunRow(task_id="t1", node_id="planning", node_kind="agent")
    )
    saved = FlowRunState(
        flow_fingerprint="fp-saved", current_node="implementation", loop_counters={"test_fix": 2}
    )
    recorder.save_checkpoint(saved)

    # Recovery trusts the persisted fingerprint — no live config is consulted.
    hydrated = hydrate_run_state(store, "t1")
    assert hydrated is not None
    assert hydrated.flow_fingerprint == "fp-saved"
    assert hydrated.current_node == "implementation"
    assert hydrated.loop_counters == {"test_fix": 2}
    assert hydrated.completed_nodes == ["refinement", "planning"]


def test_recovery_does_not_rereresolve_flow(tmp_path: Path) -> None:
    # hydrate_run_state takes only the store + task id — no config/registry — so resume can never
    # re-resolve the flow from live config; it returns exactly the persisted snapshot fingerprint
    # (the P1.2 recovery invariant; the orchestrator-level recovery dispatch lands in P1.4).
    store = _store(tmp_path)
    StateStoreRunRecorder(store, "t1", artifacts_root=tmp_path).save_checkpoint(
        FlowRunState(flow_fingerprint="snap-abc123", current_node="review")
    )
    hydrated = hydrate_run_state(store, "t1")
    assert hydrated is not None and hydrated.flow_fingerprint == "snap-abc123"
