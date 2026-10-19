"""A non-blocking evaluator on a named fix loop: it never parks, and a fix budget means fix rounds.

Reproduces the ``enrich-07b`` run on the smallest flow that has its shape — an author, a
non-blocking gate (``max_rework_per_stage: 2``) whose ``rework`` edge is a named loop with a budget
of 2, a fixing node, and publish — driven by the real engine, the real evaluator runner and the
real state store. The gate never stops finding a gating issue, which is what made that run park in
``manual_action_required`` (``max_fix_cycles``) after a single fix round, and then, once the
operator reset the fix budget, accept at once without any fix round at all.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

from wastech_orchestrator.config.schema import AgentsConfig, DecompositionConfig
from wastech_orchestrator.core.flow.contracts import (
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
)
from wastech_orchestrator.core.flow.engine import (
    FlowEngine,
    FlowRunResult,
    NodeContext,
    NodeOutcome,
    NodeResult,
)
from wastech_orchestrator.core.flow.nodes import NodeInputs, NodeServices
from wastech_orchestrator.core.flow.nodes.evaluator import EvaluatorNodeRunner
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import (
    AgentNode,
    Edge,
    EvaluatorNode,
    FlowDoc,
    FlowNode,
    PublishNode,
)
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.providers.base import AgentRunResult, ProviderId, RunStatus
from wastech_orchestrator.routing.router import ResolvedRoute, RouteSource, StageOutcome
from wastech_orchestrator.state_store import StateStore, TaskRow

_TASK = "enrich"


class _GatingRouter:
    """Every evaluator pass returns the same gating finding — the lens is never satisfied."""

    def resolve_route(self, node_id: str, override: Any = None) -> ResolvedRoute:
        return ResolvedRoute(
            node_id=node_id, primary=ProviderId.CLAUDE, fallback=None, source=RouteSource.CONFIG
        )

    def route_grants_shell(self, route: ResolvedRoute, **_kwargs: Any) -> bool:
        return False

    def run_stage(self, request: Any, route: ResolvedRoute, *, snapshot: Any = None) -> Any:
        result = AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider="claude",
            node_id=request.node_id,
            attempt=1,
            exit_code=0,
            started_at="t0",
            finished_at="t1",
            structured_output={
                "findings": [
                    {"severity": "high", "path": "report.md", "what": "still thin", "fix": None}
                ]
            },
        )
        return StageOutcome(
            route=route,
            result=result,
            provider_used=ProviderId.CLAUDE,
            stage_attempts=1,
            terminal_error=None,
            attempts=(),
        )


class _CountingRunner:
    """Stand-in for the agent and publish nodes: takes the forward edge and counts the calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        self.calls.append(node.id)
        return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=0)


class _Recorder:
    def __init__(self) -> None:
        self.failure_reports: list[tuple[str, str | None, str]] = []

    def record_skip(self, node: FlowNode, *, reason: str, subtask_order: int | None) -> None:
        pass

    def save_checkpoint(self, run_state: FlowRunState) -> None:
        pass

    def write_failure_report(
        self, *, node_id: str, loop: str | None, limit_name: str, **_: Any
    ) -> str:
        self.failure_reports.append((node_id, loop, limit_name))
        return "failure_report.json"


def _snapshot(*, loop_budget: int, max_rework_per_stage: int) -> FlowSnapshot:
    nodes: tuple[FlowNode, ...] = (
        AgentNode(id="author", kind="agent", role_file="r.md"),
        EvaluatorNode(
            id="gate",
            kind="evaluator",
            role="verifier",
            role_file="r.md",
            permission_profile=PermissionProfile.READ_ONLY,
            blocking=False,
            max_rework_per_stage=max_rework_per_stage,
        ),
        AgentNode(id="fixing", kind="agent", role_file="r.md"),
        PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST),
    )
    edges = (
        Edge("author", "gate"),
        Edge("gate", "publish", outcome="accept"),
        Edge("gate", "fixing", outcome="rework", loop="gate_fix"),
        Edge("fixing", "gate"),
    )
    adjacency: dict[str, list[Edge]] = {}
    for edge in edges:
        adjacency.setdefault(edge.from_node, []).append(edge)
    doc = FlowDoc(
        name="mini_enrich",
        task_type="mini_enrich",
        permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
        output_policy=OutputPolicy.CODE_CHANGE,
        publishing=PublishingPolicy.PULL_REQUEST,
        nodes=nodes,
        edges=edges,
        budgets=MappingProxyType({"gate_fix": loop_budget}),
    )
    return FlowSnapshot(
        doc=doc,
        nodes_by_id=MappingProxyType({n.id: n for n in nodes}),
        adjacency=MappingProxyType({k: tuple(v) for k, v in adjacency.items()}),
        flow_fingerprint="fp",
    )


class _Harness:
    def __init__(self, tmp_path: Path, snapshot: FlowSnapshot) -> None:
        (tmp_path / "r.md").write_text("Judge the material.", encoding="utf-8")
        self.store = StateStore.open(tmp_path / "state.db")
        self.store.insert_task(TaskRow(task_id=_TASK, title="T", status=Status.VALIDATED))
        self.snapshot = snapshot
        self.agents = _CountingRunner()
        self.recorder = _Recorder()
        self.outcomes: list[NodeOutcome] = []
        services = NodeServices(
            router=_GatingRouter(),  # type: ignore[arg-type]
            check_runner=None,  # type: ignore[arg-type]
            store=self.store,
            repo_dir=str(tmp_path),
            artifacts_root=str(tmp_path / "artifacts"),
            clock=lambda: "2026-10-19T00:00:00+00:00",
        )
        self.evaluator = EvaluatorNodeRunner(services, NodeInputs(flow_dir=tmp_path))

    def run(self, run_state: FlowRunState) -> FlowRunResult:
        runners = {"agent": self.agents, "publish": self.agents, "evaluator": self.evaluator}
        return FlowEngine(
            self.snapshot,
            run_state,
            runners,  # type: ignore[arg-type]
            self.recorder,  # type: ignore[arg-type]
            facts=lambda _fact: False,
            agents=AgentsConfig(
                allowed=(ProviderId.CLAUDE,),
                max_stage_attempts=3,
                max_fix_cycles=15,
                max_total_fix_iterations=30,
                decomposition=DecompositionConfig(enabled=False, max_subtasks=8),
                providers={},
            ),
            task_id=_TASK,
            post_node=lambda _node, outcome, _run_id: self.outcomes.append(outcome),
        ).run()

    def gate_verdicts(self) -> list[str]:
        return [
            row.verdict
            for row in self.store.get_evaluations(_TASK)
            if row.kind == "in_flow_verdict" and row.node_id == "gate"
        ]


def test_a_non_blocking_gate_spends_its_whole_loop_budget_and_accepts(tmp_path: Path) -> None:
    # Loop budget 2 and max_rework_per_stage 2: two fix rounds, then accept with ⚠️. Before the fix
    # the second rework verdict hit the loop cap first — one fix round, then manual_action_required.
    harness = _Harness(tmp_path, _snapshot(loop_budget=2, max_rework_per_stage=2))
    result = harness.run(FlowRunState(flow_fingerprint="fp"))

    assert result.status is Status.DONE
    assert harness.recorder.failure_reports == []
    assert harness.agents.calls.count("fixing") == 2
    assert harness.gate_verdicts() == ["rework", "rework", "accept"]
    last_gate = [o for o in harness.outcomes if o.kind in ("accept", "rework")][-1]
    assert last_gate.rework_exhausted is True
    assert last_gate.rework_limit is not None


def test_a_loop_budget_below_max_rework_per_stage_ends_in_accept_not_manual(
    tmp_path: Path,
) -> None:
    # The loop budget is the tighter limit here. A non-blocking gate still never parks: it takes
    # the one round the loop allows and then accepts, and the warning names the loop, not the
    # per-stage budget the operator would otherwise look at first.
    harness = _Harness(tmp_path, _snapshot(loop_budget=1, max_rework_per_stage=2))
    result = harness.run(FlowRunState(flow_fingerprint="fp"))

    assert result.status is Status.DONE
    assert harness.recorder.failure_reports == []
    assert harness.agents.calls.count("fixing") == 1
    assert harness.gate_verdicts() == ["rework", "accept"]
    last_gate = [o for o in harness.outcomes if o.kind in ("accept", "rework")][-1]
    assert last_gate.rework_exhausted is True
    assert "gate_fix" in (last_gate.rework_limit or "")


def _resume_at_gate(harness: _Harness, *, reset: bool) -> FlowRunResult:
    """What `rerun --continue [--reset-fix-budget]` hands the engine: a checkpoint at the gate."""
    run_state = FlowRunState(
        flow_fingerprint="fp",
        current_node="gate",
        loop_counters={"gate_fix": 2, "global_fix_iterations": 2, "total_fix:gate_fix": 2},
    )
    if reset:
        run_state.reset_consecutive_fix_budget()
        harness.store.record_rework_budget_reset(_TASK)
    harness.agents.calls.clear()
    return harness.run(run_state)


def test_reset_fix_budget_gives_a_spent_gate_new_fix_rounds(tmp_path: Path) -> None:
    # The second half of the run: with the per-stage count spent, a reset that reached only the loop
    # counters let the gate accept on its first pass — no fix round, a full paid evaluator run.
    harness = _Harness(tmp_path, _snapshot(loop_budget=2, max_rework_per_stage=2))
    harness.run(FlowRunState(flow_fingerprint="fp"))

    result = _resume_at_gate(harness, reset=True)
    assert result.status is Status.DONE
    assert harness.agents.calls.count("fixing") == 2


def test_without_a_reset_a_spent_gate_accepts_without_a_fix_round(tmp_path: Path) -> None:
    # The documented other side: no grant, no rounds. This is what `rerun --dry-run` now says.
    harness = _Harness(tmp_path, _snapshot(loop_budget=2, max_rework_per_stage=2))
    harness.run(FlowRunState(flow_fingerprint="fp"))

    result = _resume_at_gate(harness, reset=False)
    assert result.status is Status.DONE
    assert harness.agents.calls.count("fixing") == 0
