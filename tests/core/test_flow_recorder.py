"""StateStoreRunRecorder, the deterministic step record, and resume hydration."""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.core.flow.recorder import (
    StateStoreRunRecorder,
    collect_step_facts,
    fell_back_from,
    hydrate_run_state,
)
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import AgentNode
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.providers.artifacts import node_run_dir
from wastech_orchestrator.state_store import EvaluationRow, NodeRunRow, StateStore, TaskRow


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


def test_recorder_failure_report_carries_findings_and_diff(tmp_path: Path) -> None:
    # The stuck report must carry the REAL last in-flow review findings and working-tree diff,
    # not the old hardcoded (none)/(empty) — so a terminal is diagnosable without hand-recovery.
    store = _store(tmp_path)
    store.record_evaluation(
        EvaluationRow(
            task_id="t1",
            kind="in_flow_verdict",
            verdict="rework",
            findings_json=json.dumps(
                [{"severity": "blocking", "reason": "schema drift", "paths": ["a.ts"]}]
            ),
            node_id="review",
        )
    )
    diff_dir = tmp_path / "logs" / "t1"
    diff_dir.mkdir(parents=True)
    (diff_dir / "current.diff").write_text("diff --git a/a.ts b/a.ts\n+changed\n", encoding="utf-8")

    recorder = StateStoreRunRecorder(store, "t1", artifacts_root=tmp_path)
    state = FlowRunState(flow_fingerprint="fp", current_node="review", loop_counters={})
    path = recorder.write_failure_report(
        node_id="review", loop="review_fix", limit_name="max_fix_cycles", run_state=state
    )
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    assert report["last_review_findings"] == [
        {"severity": "blocking", "reason": "schema drift", "paths": ["a.ts"]}
    ]
    assert "changed" in report["final_diff"]
    stuck = (Path(path).parent / "stuck.md").read_text(encoding="utf-8")
    assert "schema drift" in stuck


def _run(**overrides: object) -> NodeRunRow:
    fields: dict = {"task_id": "t1", "node_id": "implementation", "node_kind": "agent"}
    fields.update(overrides)
    return NodeRunRow(**fields)


def test_fallback_is_derived_only_from_a_real_route_divergence() -> None:
    # The one derivation of "this step did not land where it was routed": the finalize packet's step
    # record and the observation cadence's `fallback` trigger both read it here.
    assert fell_back_from(_run(provider_used="codex", route_primary="claude")) == "claude"
    assert fell_back_from(_run(provider_used="claude", route_primary="claude")) is None
    # Non-agent kinds leave both route columns NULL — that is not a fallback.
    assert fell_back_from(_run(provider_used=None, route_primary=None)) is None
    assert fell_back_from(_run(provider_used="codex", route_primary=None)) is None
    assert fell_back_from(_run(provider_used=None, route_primary="claude")) is None


def test_step_record_carries_each_run_in_execution_order_with_its_own_message(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = store.record_node_run(
        NodeRunRow(task_id="t1", node_id="planning", node_kind="agent", status="passed")
    )
    second = store.record_node_run(
        NodeRunRow(
            task_id="t1",
            node_id="implementation",
            node_kind="agent",
            status="passed",
            outcome="done",
            stage_attempts=2,
            provider_used="codex",
            route_primary="claude",
            subtask_order=1,
        )
    )
    out = node_run_dir(tmp_path, "t1", "implementation", second)
    out.mkdir(parents=True)
    (out / "implementation.out.md").write_text("wired the adapter", encoding="utf-8")

    facts = collect_step_facts(store.get_node_runs("t1"), tmp_path, "t1")
    assert [f.node_id for f in facts] == ["planning", "implementation"]
    assert facts[0].message is None  # the run wrote no closing message
    assert facts[1].message == "wired the adapter"
    assert facts[1].fallback_from == "claude"
    assert facts[1].stage_attempts == 2
    assert facts[1].subtask_order == 1
    assert first != second


def test_step_record_message_is_verbatim_so_bounding_stays_with_the_renderer(
    tmp_path: Path,
) -> None:
    # The record is the fact; truncating here would make the truncation permanent for every reader.
    store = _store(tmp_path)
    run_id = store.record_node_run(
        NodeRunRow(task_id="t1", node_id="planning", node_kind="agent", status="passed")
    )
    out = node_run_dir(tmp_path, "t1", "planning", run_id)
    out.mkdir(parents=True)
    (out / "planning.out.md").write_text("x" * 900, encoding="utf-8")

    facts = collect_step_facts(store.get_node_runs("t1"), tmp_path, "t1")
    assert facts[0].message == "x" * 900


def test_step_record_survives_a_run_with_no_id(tmp_path: Path) -> None:
    # A row with no id has no artifact directory to name, and the packet build swallows exceptions —
    # so an unguarded read here would degrade a finalize to "unseeded" with only a log line.
    facts = collect_step_facts([_run(status="passed")], tmp_path, "t1")
    assert len(facts) == 1
    assert facts[0].message is None


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
    # (the recorder's recovery invariant; the orchestrator dispatches recovery itself).
    store = _store(tmp_path)
    StateStoreRunRecorder(store, "t1", artifacts_root=tmp_path).save_checkpoint(
        FlowRunState(flow_fingerprint="snap-abc123", current_node="review")
    )
    hydrated = hydrate_run_state(store, "t1")
    assert hydrated is not None and hydrated.flow_fingerprint == "snap-abc123"
