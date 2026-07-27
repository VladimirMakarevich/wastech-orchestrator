"""Data-driven post-processing mechanics: output_artifact slots + decomposition contract.

Both are exercised directly (no engine) so the slot write and the decomposition gate are pinned as
flow-neutral functions before the driver wires them into the engine's post-node hook.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.flow.engine import NodeOutcome
from wastech_orchestrator.core.flow.nodes.base import NodeInputs
from wastech_orchestrator.core.flow.postprocess import (
    apply_output_artifact,
    read_decomposition,
    write_node_output,
)
from wastech_orchestrator.core.flow.schema import AgentNode


def _agent(
    node_id: str, *, output_artifact: str | None = None, output_file: str | None = None
) -> AgentNode:
    return AgentNode(
        id=node_id,
        kind="agent",
        role_file="r.md",
        output_artifact=output_artifact,
        output_file=output_file,
    )


def _recorder() -> tuple[list[tuple[str, str, str]], object]:
    calls: list[tuple[str, str, str]] = []
    return calls, lambda t, k, p: calls.append((t, k, p))


# -- generic node-output channel ({<node_id>_path}) ---------------------------


def test_node_output_written_redacted_and_registered(tmp_path: Path) -> None:
    node = _agent("scan")  # no output_artifact → generic channel applies
    outcome = NodeOutcome("done", structured_output={"content": "found key ghp_" + "a" * 36})
    calls, register = _recorder()

    path = write_node_output(
        node, outcome, artifacts_root=tmp_path, task_id="task-1", node_run_id=1, register=register
    )

    assert path is not None and path.endswith("scan.out.md")
    # Per-run: under stages/<node>/run-<id>/ (keyed by the reserved node_run_id).
    assert "stages/scan/run-000001/scan.out.md" in Path(path).as_posix()
    body = Path(path).read_text("utf-8")
    assert "ghp_" not in body and "[REDACTED]" in body  # structured output is redaction-scrubbed
    assert calls == [("task-1", "node_output", path)]


def test_node_output_extra_secret_scrubbed(tmp_path: Path) -> None:
    node = _agent("analyze")
    outcome = NodeOutcome("done", final_message="the token is s3cr3t-value-here")
    _, register = _recorder()

    path = write_node_output(
        node,
        outcome,
        artifacts_root=tmp_path,
        task_id="t",
        node_run_id=1,
        register=register,
        extra_secrets=("s3cr3t-value-here",),
    )
    assert path is not None
    assert "s3cr3t-value-here" not in Path(path).read_text("utf-8")


def test_node_output_skipped_for_special_slot_node(tmp_path: Path) -> None:
    # A node filling a special slot uses that slot as its channel — no duplicate .out.md.
    node = _agent("planning", output_artifact="plan")
    outcome = NodeOutcome("done", structured_output={"content": "PLAN"})
    calls, register = _recorder()

    assert (
        write_node_output(
            node, outcome, artifacts_root=tmp_path, task_id="t", node_run_id=1, register=register
        )
        is None
    )
    assert calls == []
    assert not (tmp_path / "logs" / "t" / "planning.out.md").exists()


def test_node_output_noop_when_empty(tmp_path: Path) -> None:
    node = _agent("scan")
    outcome = NodeOutcome("done", structured_output=None, final_message=None)
    calls, register = _recorder()
    assert (
        write_node_output(
            node, outcome, artifacts_root=tmp_path, task_id="t", node_run_id=1, register=register
        )
        is None
    )
    assert calls == []


# -- output_file: the produced file is the channel -----------------------------


def test_node_output_carries_the_declared_produced_file(tmp_path: Path) -> None:
    # The writing node published its closing message, so a 19 821-byte
    # blueprint reached the next node as a 4 042-byte pointer to a summary of itself. With
    # `output_file` the file crosses the edge instead.
    produced = tmp_path / "report-dir"
    produced.mkdir()
    (produced / "report.md").write_text("THE WHOLE REPORT\n", encoding="utf-8")
    node = _agent("synthesis", output_file="report.md")
    outcome = NodeOutcome("done", final_message="Wrote the report; see the file.")
    calls, register = _recorder()

    path = write_node_output(
        node,
        outcome,
        artifacts_root=tmp_path,
        task_id="t",
        node_run_id=1,
        register=register,
        produced_dir=produced,
    )

    assert path is not None
    # Still the same channel filename, so {<node_id>_path} resolution is untouched.
    assert path.endswith("synthesis.out.md")
    assert Path(path).read_text("utf-8") == "THE WHOLE REPORT\n"
    assert calls == [("t", "node_output", path)]


def test_produced_file_is_redaction_scrubbed(tmp_path: Path) -> None:
    # The agent wrote this file, so it is no more trusted than structured output: a secret echoed
    # into the deliverable must not reach the exchange copy unredacted.
    produced = tmp_path / "report-dir"
    produced.mkdir()
    (produced / "report.md").write_text("leaked ghp_" + "a" * 36 + "\n", encoding="utf-8")
    node = _agent("synthesis", output_file="report.md")
    _, register = _recorder()

    path = write_node_output(
        node,
        outcome=NodeOutcome("done", final_message="done"),
        artifacts_root=tmp_path,
        task_id="t",
        node_run_id=1,
        register=register,
        produced_dir=produced,
    )

    assert path is not None
    body = Path(path).read_text("utf-8")
    assert "ghp_" not in body and "[REDACTED]" in body


def test_missing_produced_file_falls_back_to_the_message_and_warns(tmp_path: Path) -> None:
    # Losing the channel entirely would be worse than carrying the message — but a silent
    # fallback is how a declared handoff becomes a phantom one, so it is warned about.
    produced = tmp_path / "report-dir"
    produced.mkdir()
    node = _agent("synthesis", output_file="report.md")
    outcome = NodeOutcome("done", final_message="I could not write it.")
    _, register = _recorder()
    warnings: list[str] = []

    path = write_node_output(
        node,
        outcome,
        artifacts_root=tmp_path,
        task_id="t",
        node_run_id=1,
        register=register,
        produced_dir=produced,
        warn=warnings.append,
    )

    assert path is not None
    assert Path(path).read_text("utf-8") == "I could not write it."
    assert len(warnings) == 1
    assert "output_file" in warnings[0] and "report.md" in warnings[0]


def test_empty_produced_file_falls_back_to_the_message_and_warns(tmp_path: Path) -> None:
    # An empty file is "not produced" as far as the handoff goes, and gets the same warning.
    produced = tmp_path / "report-dir"
    produced.mkdir()
    (produced / "report.md").write_text("", encoding="utf-8")
    node = _agent("synthesis", output_file="report.md")
    _, register = _recorder()
    warnings: list[str] = []

    path = write_node_output(
        node,
        outcome=NodeOutcome("done", final_message="fallback body"),
        artifacts_root=tmp_path,
        task_id="t",
        node_run_id=1,
        register=register,
        produced_dir=produced,
        warn=warnings.append,
    )

    assert path is not None
    assert Path(path).read_text("utf-8") == "fallback body"
    assert "it is empty" in warnings[0]


def test_unreadable_produced_file_falls_back_to_the_message(tmp_path: Path) -> None:
    # A binary or otherwise non-text product cannot be published as a redacted text copy.
    produced = tmp_path / "report-dir"
    produced.mkdir()
    (produced / "report.md").write_bytes(b"\xff\xfe\x00binary")
    node = _agent("synthesis", output_file="report.md")
    _, register = _recorder()
    warnings: list[str] = []

    path = write_node_output(
        node,
        outcome=NodeOutcome("done", final_message="fallback body"),
        artifacts_root=tmp_path,
        task_id="t",
        node_run_id=1,
        register=register,
        produced_dir=produced,
        warn=warnings.append,
    )

    assert path is not None
    assert Path(path).read_text("utf-8") == "fallback body"
    assert "could not be read as text" in warnings[0]


def test_produced_file_ignored_without_a_resolved_directory(tmp_path: Path) -> None:
    # No report dir and no repo root resolved (a unit harness): the declaration is inert, never a
    # crash and never a read relative to the process cwd.
    node = _agent("synthesis", output_file="report.md")
    _, register = _recorder()

    path = write_node_output(
        node,
        outcome=NodeOutcome("done", final_message="message"),
        artifacts_root=tmp_path,
        task_id="t",
        node_run_id=1,
        register=register,
    )

    assert path is not None
    assert Path(path).read_text("utf-8") == "message"


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


def test_report_slot_writes_redacted_into_private_report_dir(tmp_path: Path) -> None:
    # The security_audit report node is read-only — the agent returns the report as its
    # structured output and the orchestrator captures it (redacted) into the flow's PRIVATE
    # output_policy report dir. It is never written to the task artifact dir and never published to
    # the agent-readable exchange (the slot is inputs_field=None, exchange=False).
    node = _agent("report", output_artifact="report")
    secret = "ghp_" + "a" * 36
    outcome = NodeOutcome("done", structured_output={"content": f"# Audit\n\nleaked {secret}\n"})
    inputs = NodeInputs(flow_dir=tmp_path)
    calls, register = _recorder()
    report_dir = tmp_path / ".worc" / "security-reports" / "task-1"

    path = apply_output_artifact(
        node,
        outcome,
        artifacts_root=tmp_path,
        task_id="task-1",
        inputs=inputs,
        register=register,
        report_dir=report_dir,
    )

    assert path is not None and Path(path) == report_dir / "report.md"
    body = Path(path).read_text("utf-8")
    assert secret not in body and "[REDACTED]" in body  # captured report content is redacted
    assert calls == [("task-1", "report", path)]
    # Audit-only downstream: no prompt variable is threaded (so nothing is routed to the exchange).
    assert inputs.plan_path is None and inputs.summary_body_path is None


def test_report_slot_noop_without_report_dir(tmp_path: Path) -> None:
    # Defensive: a report slot with no report output_policy resolves to nothing — it never falls
    # back to writing into the (agent-readable) task artifact dir.
    node = _agent("report", output_artifact="report")
    outcome = NodeOutcome("done", structured_output={"content": "BODY"})
    inputs = NodeInputs(flow_dir=tmp_path)
    calls, register = _recorder()
    assert (
        apply_output_artifact(
            node, outcome, artifacts_root=tmp_path, task_id="t", inputs=inputs, register=register
        )
        is None
    )
    assert calls == []


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
        {
            "order": 1,
            "title": "First",
            "slug": "first",
            "acceptance_criteria": ["x"],
            "depends_on": [],
        },
        {
            "order": 2,
            "title": "Second",
            "slug": "second",
            "acceptance_criteria": ["y"],
            "depends_on": [1],
        },
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
