"""Data-driven post-processing mechanics (P1.4): output_artifact slots + decomposition contract.

Both are exercised directly (no engine) so the slot write and the decomposition gate are pinned as
flow-neutral functions before the driver wires them into the engine's post-node hook.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.flow.engine import NodeOutcome
from wastech_orchestrator.core.flow.nodes.base import NodeInputs
from wastech_orchestrator.core.flow.postprocess import apply_output_artifact, read_decomposition
from wastech_orchestrator.core.flow.schema import AgentNode


def _agent(node_id: str, *, output_artifact: str | None = None) -> AgentNode:
    return AgentNode(id=node_id, kind="agent", role_file="r.md", output_artifact=output_artifact)


def _recorder() -> tuple[list[tuple[str, str, str]], object]:
    calls: list[tuple[str, str, str]] = []
    return calls, lambda t, k, p: calls.append((t, k, p))


# -- output_artifact slots ----------------------------------------------------


def test_plan_slot_writes_file_threads_path_and_registers(tmp_path: Path) -> None:
    node = _agent("planning", output_artifact="plan")
    outcome = NodeOutcome("done", structured_output={"content": "PLAN BODY"})
    inputs = NodeInputs(flow_dir=tmp_path)
    calls, register = _recorder()

    path = apply_output_artifact(
        node, outcome, artifacts_root=tmp_path, task_id="task-1", inputs=inputs, register=register
    )

    assert path is not None and path.endswith("plan.md")
    assert Path(path).read_text("utf-8") == "PLAN BODY"
    assert inputs.plan_path == path  # threaded downstream as {plan_path}
    assert calls == [("task-1", "plan", path)]


def test_summary_slot_falls_back_to_final_message(tmp_path: Path) -> None:
    # The summary agent is free-form (no structured content), so the body is its final_message.
    node = _agent("summary", output_artifact="summary")
    outcome = NodeOutcome("done", structured_output=None, final_message="SUMMARY BODY")
    inputs = NodeInputs(flow_dir=tmp_path)
    calls, register = _recorder()

    path = apply_output_artifact(
        node, outcome, artifacts_root=tmp_path, task_id="task-1", inputs=inputs, register=register
    )

    assert path is not None and path.endswith("summary.md")
    assert Path(path).read_text("utf-8") == "SUMMARY BODY"
    assert inputs.summary_body_path == path
    assert calls == [("task-1", "summary_md", path)]


def test_enriched_slot_is_audit_only_no_inputs_field(tmp_path: Path) -> None:
    node = _agent("refinement", output_artifact="enriched_spec")
    outcome = NodeOutcome("done", structured_output={"content": "ENRICHED"})
    inputs = NodeInputs(flow_dir=tmp_path)
    calls, register = _recorder()

    path = apply_output_artifact(
        node, outcome, artifacts_root=tmp_path, task_id="task-1", inputs=inputs, register=register
    )

    assert path is not None and path.endswith("task.enriched.md")
    assert Path(path).read_text("utf-8") == "ENRICHED"
    # enriched has no prompt variable in the legacy, so no NodeInputs field is updated.
    assert inputs.plan_path is None and inputs.summary_body_path is None
    assert calls == [("task-1", "enriched", path)]


def test_no_slot_is_noop(tmp_path: Path) -> None:
    node = _agent("implementation")  # no output_artifact
    outcome = NodeOutcome("done", final_message="ignored")
    inputs = NodeInputs(flow_dir=tmp_path)
    calls, register = _recorder()

    path = apply_output_artifact(
        node, outcome, artifacts_root=tmp_path, task_id="task-1", inputs=inputs, register=register
    )

    assert path is None
    assert calls == []
    assert inputs.plan_path is None


# -- decomposition contract ---------------------------------------------------


def _subtasks() -> list[dict[str, object]]:
    return [
        {"order": 1, "title": "First", "slug": "first", "acceptance_criteria": ["x"],
         "depends_on": []},
        {"order": 2, "title": "Second", "slug": "second", "acceptance_criteria": ["y"],
         "depends_on": [1]},
    ]


def test_read_decomposition_accepts_valid_contract() -> None:
    outcome = NodeOutcome("done", structured_output={"decompose": True, "subtasks": _subtasks()})
    decision = read_decomposition(outcome, gate_on=True, max_subtasks=8)
    assert decision.accepted is True
    assert decision.n == 2


def test_read_decomposition_gate_off_is_single_unit() -> None:
    outcome = NodeOutcome("done", structured_output={"decompose": True, "subtasks": _subtasks()})
    decision = read_decomposition(outcome, gate_on=False, max_subtasks=8)
    assert decision.accepted is False
    assert decision.n == 1


def test_read_decomposition_not_recommended_is_single_unit() -> None:
    outcome = NodeOutcome("done", structured_output={"decompose": False, "subtasks": []})
    decision = read_decomposition(outcome, gate_on=True, max_subtasks=8)
    assert decision.accepted is False
