"""The packaged `implementation` flow's `documentation` node (docs/backlog/documentation-node.md).

A whole-task docs-update agent inserted on the `review --accept-->` exit, before `publish`. These
tests pin its graph shape: it loads/validates, routes soundly, resumes the implementation editing
lineage, runs exactly once (even when the task is decomposed), and can be disabled per task. The
runtime affinity mechanism it reuses is covered generically by
``test_flow_node_runners.test_affinity_resumes_declared_node_session``; the real-pipeline edit-is-
committed integration test lives in ``test_orchestrator.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.core.flow.contracts import PermissionProfile, SessionScope
from wastech_orchestrator.core.flow.engine_driver import partition_decomposition
from wastech_orchestrator.core.flow.schema import AgentNode
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot, load_flow
from wastech_orchestrator.core.flow.validator import validate_disabled_nodes, validate_flow

_PACKAGED = (
    Path(__file__).resolve().parents[2] / "src" / "wastech_orchestrator" / "packaged" / "flows"
)


@pytest.fixture(scope="module")
def impl_snap() -> FlowSnapshot:
    snap = load_flow(_PACKAGED / "implementation.yaml")
    validate_flow(snap)  # the new node + edges must pass routing-soundness + ceiling unchanged
    return snap


def test_implementation_flow_loads_with_documentation_node(impl_snap: FlowSnapshot) -> None:
    node = impl_snap.nodes_by_id["documentation"]
    assert isinstance(node, AgentNode)
    assert node.kind == "agent"
    assert node.role_file == "roles/documentation.md"
    # An editing node, not an evaluator/check: it edits the working tree at the flow ceiling.
    assert node.permission_profile == PermissionProfile.WORKSPACE_WRITE
    # No persisted artifact slot and no human gate / when-skip (consistent with fixing).
    assert node.output_artifact is None
    assert node.hitl is None
    assert node.when is None
    # Its role prompt ships in the packaged flow tree (seeded into .worc/flows/roles by install).
    assert (_PACKAGED / "roles" / "documentation.md").is_file()


def test_documentation_role_prompt_only_interpolates_allowed_vars() -> None:
    from wastech_orchestrator.core.prompts import ALLOWED_PROMPT_VARS, render_prompt

    template = (_PACKAGED / "roles" / "documentation.md").read_text(encoding="utf-8")
    rendered = render_prompt(template, {"plan_path": "/p", "diff_path": "/d", "skills_path": None})
    # The plan/diff path tokens are substituted; no unresolved allowlisted token leaks through.
    assert "{plan_path}" not in rendered and "/p" in rendered
    assert "{diff_path}" not in rendered and "/d" in rendered
    assert not any(f"{{{name}}}" in rendered for name in ALLOWED_PROMPT_VARS)


def test_documentation_routing_soundness(impl_snap: FlowSnapshot) -> None:
    # review --accept--> documentation --> publish; rework still goes to fixing.
    review_edges = {e.outcome: e.to for e in impl_snap.adjacency["review"]}
    assert review_edges["accept"] == "documentation"
    assert review_edges["rework"] == "fixing"
    # documentation has a single unconditional forward edge to publish (emits the `done` outcome).
    doc_edges = impl_snap.adjacency["documentation"]
    assert len(doc_edges) == 1
    assert doc_edges[0].outcome is None and doc_edges[0].to == "publish"
    # publish stays the only terminal and is still reachable.
    sources = {e.from_node for e in impl_snap.doc.edges}
    assert set(impl_snap.nodes_by_id) - sources == {"publish"}


def test_documentation_resumes_implementation_lineage(impl_snap: FlowSnapshot) -> None:
    # Same durable-session wiring as `fixing`: resumes the implementation editing lineage so the
    # docs agent has the full context of what shipped (incl. fix-loop changes).
    doc = impl_snap.nodes_by_id["documentation"]
    fixing = impl_snap.nodes_by_id["fixing"]
    assert isinstance(doc, AgentNode) and isinstance(fixing, AgentNode)
    assert doc.session_scope is SessionScope.EDITING_LINEAGE
    assert doc.lineage_affinity == "implementation"
    assert doc.session_scope == fixing.session_scope
    assert doc.lineage_affinity == fixing.lineage_affinity


def test_documentation_runs_once_after_decomposition(impl_snap: FlowSnapshot) -> None:
    # documentation is a whole-task concern: kept OUT of the sub_flow region so it runs once in the
    # post-region phase, not once per subtask. The forward exit of the region (review --accept-->)
    # lands on documentation, which the driver runs once after the last subtask.
    dec = impl_snap.doc.decomposition
    assert dec is not None
    assert "documentation" not in dec.sub_flow
    assert dec.sub_flow == ("implementation", "testing", "review", "fixing")
    regions = partition_decomposition(impl_snap)
    assert regions.post_entry == "documentation"
    assert "documentation" not in regions.region


def test_documentation_disabled_per_task_skips(impl_snap: FlowSnapshot) -> None:
    # PRE.3 per-task disable: `nodes.documentation.enabled: false` is routing-sound — the node's
    # `done` skip-outcome takes its single forward edge straight to publish (no mid-run crash).
    validate_disabled_nodes(impl_snap, frozenset({"documentation"}))  # must not raise
