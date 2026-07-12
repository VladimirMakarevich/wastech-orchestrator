"""Unit tests for the budget-cap and fix-loop-exhaustion primitives (rerun drift/budget-confirm)."""

from __future__ import annotations

from types import MappingProxyType

from wastech_orchestrator.config.schema import AgentsConfig, DecompositionConfig
from wastech_orchestrator.core.flow.contracts import (
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
)
from wastech_orchestrator.core.flow.engine import FlowEngine
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import AgentNode, Edge, EvaluatorNode, FlowDoc, FlowNode
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.loop_control import (
    ExhaustedLoop,
    exhausted_fix_loops,
    global_backstop_exhausted,
    global_cap,
    loop_cap,
)
from wastech_orchestrator.providers.base import ProviderId


def _agents(*, max_fix_cycles: int = 99, max_total: int = 99) -> AgentsConfig:
    return AgentsConfig(
        allowed=(ProviderId.CLAUDE, ProviderId.CODEX),
        max_stage_attempts=3,
        max_fix_cycles=max_fix_cycles,
        max_total_fix_iterations=max_total,
        decomposition=DecompositionConfig(enabled=False, max_subtasks=8),
        providers={},
    )


def _agent(node_id: str) -> AgentNode:
    return AgentNode(id=node_id, kind="agent", role_file=f"roles/{node_id}.md")


def _evaluator(node_id: str) -> EvaluatorNode:
    return EvaluatorNode(id=node_id, kind="evaluator", role="review", role_file="roles/r.md")


def _snapshot(
    nodes: list[FlowNode], edges: list[Edge], budgets: dict[str, int] | None = None
) -> FlowSnapshot:
    doc = FlowDoc(
        name="t",
        task_type="t",
        permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
        output_policy=OutputPolicy.CODE_CHANGE,
        publishing=PublishingPolicy.PULL_REQUEST,
        nodes=tuple(nodes),
        edges=tuple(edges),
        budgets=MappingProxyType(budgets or {}),
    )
    adj: dict[str, list[Edge]] = {}
    for edge in edges:
        adj.setdefault(edge.from_node, []).append(edge)
    return FlowSnapshot(
        doc=doc,
        nodes_by_id=MappingProxyType({n.id: n for n in nodes}),
        adjacency=MappingProxyType({k: tuple(v) for k, v in adj.items()}),
        flow_fingerprint="fp",
    )


# -- loop_cap / global_cap ------------------------------------------------------------------


def test_loop_cap_clamps_to_flow_budget() -> None:
    assert loop_cap({"review_fix": 5}, max_fix_cycles=99, loop="review_fix") == 5


def test_loop_cap_clamps_to_config_ceiling() -> None:
    assert loop_cap({"review_fix": 99}, max_fix_cycles=3, loop="review_fix") == 3


def test_loop_cap_absent_budget_falls_back_to_config_ceiling() -> None:
    assert loop_cap({}, max_fix_cycles=7, loop="review_fix") == 7


def test_global_cap_clamps_to_config_ceiling() -> None:
    assert global_cap({"global_fix_iterations": 30}, max_total_fix_iterations=10) == 10


# -- exhausted_fix_loops ---------------------------------------------------------------------


def _review_fix_flow(budgets: dict[str, int] | None = None) -> FlowSnapshot:
    """implementation -> review --rework(review_fix)--> review; review --pass--> done."""
    nodes: list[FlowNode] = [_agent("implementation"), _evaluator("review"), _agent("done")]
    edges = [
        Edge(from_node="implementation", to="review", outcome="done"),
        Edge(from_node="review", to="implementation", outcome="rework", loop="review_fix"),
        Edge(from_node="review", to="done", outcome="pass"),
    ]
    return _snapshot(nodes, edges, budgets or {"review_fix": 15})


def test_exhausted_fix_loops_empty_when_under_cap() -> None:
    snapshot = _review_fix_flow()
    result = exhausted_fix_loops(snapshot, {"review_fix": 14}, max_fix_cycles=99, start="review")
    assert result == []


def test_exhausted_fix_loops_boundary_counter_equal_cap() -> None:
    """counter == cap is already exhausted (the engine bumps then compares >=, so a park always
    persists the counter already at the cap value)."""
    snapshot = _review_fix_flow()
    result = exhausted_fix_loops(snapshot, {"review_fix": 15}, max_fix_cycles=99, start="review")
    assert result == [ExhaustedLoop(loop="review_fix", node="review", counter=15, cap=15)]


def test_exhausted_fix_loops_scopes_to_forward_reachable_path() -> None:
    """A loop exhausted downstream of the resume node is still caught (broad scope) — e.g. --from
    an upstream node with a downstream loop already at cap."""
    snapshot = _review_fix_flow()
    result = exhausted_fix_loops(
        snapshot, {"review_fix": 15}, max_fix_cycles=99, start="implementation"
    )
    assert result == [ExhaustedLoop(loop="review_fix", node="review", counter=15, cap=15)]


def test_exhausted_fix_loops_ignores_loops_not_reachable_from_start() -> None:
    """A node with its own exhausted loop that is NOT reachable from the resume node is not
    reported — starting at 'done' cannot reach 'review' backwards."""
    snapshot = _review_fix_flow()
    result = exhausted_fix_loops(snapshot, {"review_fix": 15}, max_fix_cycles=99, start="done")
    assert result == []


def test_exhausted_fix_loops_respects_effective_cap_not_just_flow_budget() -> None:
    snapshot = _review_fix_flow(budgets={"review_fix": 99})
    result = exhausted_fix_loops(snapshot, {"review_fix": 5}, max_fix_cycles=5, start="review")
    assert result == [ExhaustedLoop(loop="review_fix", node="review", counter=5, cap=5)]


def test_global_backstop_exhausted_boundary() -> None:
    budgets = {"global_fix_iterations": 30}
    assert global_backstop_exhausted(budgets, 30, {"global_fix_iterations": 30}) is True
    assert global_backstop_exhausted(budgets, 30, {"global_fix_iterations": 29}) is False


# -- engine delegation regression -----------------------------------------------------------


def test_engine_loop_cap_delegates_to_loop_control() -> None:
    snapshot = _review_fix_flow(budgets={"review_fix": 5, "global_fix_iterations": 20})
    agents = _agents(max_fix_cycles=99, max_total=99)
    engine = FlowEngine(
        snapshot,
        FlowRunState(flow_fingerprint=snapshot.flow_fingerprint),
        {"agent": None, "evaluator": None},  # type: ignore[dict-item]
        recorder=None,  # type: ignore[arg-type]
        facts=lambda _fact: False,
        agents=agents,
        task_id="task-1",
    )
    assert engine._loop_cap("review_fix") == loop_cap(  # noqa: SLF001
        snapshot.doc.budgets, agents.max_fix_cycles, "review_fix"
    )
    assert engine._global_cap() == global_cap(  # noqa: SLF001
        snapshot.doc.budgets, agents.max_total_fix_iterations
    )
