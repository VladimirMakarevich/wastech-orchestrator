"""Unit tests for the flow execution engine.

The engine is exercised with stub node runners and an in-memory recorder; real core-owned node
wrappers and persistence are layered on top later. Snapshots are built directly from
the schema dataclasses so each test crafts an exact graph shape.
"""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType

import pytest

from wastech_orchestrator.config.schema import AgentsConfig, DecompositionConfig
from wastech_orchestrator.core.flow.contracts import (
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
)
from wastech_orchestrator.core.flow.engine import (
    EngineInternalError,
    FlowCancelled,
    FlowEngine,
    FlowRunResult,
    NodeContext,
    NodeOutcome,
    NodeResult,
)
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import (
    AgentNode,
    ChecksNode,
    Edge,
    EvaluatorNode,
    FlowDoc,
    FlowNode,
    PublishNode,
    WhenPredicate,
)
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.providers.base import ProviderId

# -- builders -----------------------------------------------------------------


def _agents(*, max_fix_cycles: int = 99, max_total: int = 99) -> AgentsConfig:
    return AgentsConfig(
        allowed=(ProviderId.CLAUDE, ProviderId.CODEX),
        max_stage_attempts=3,
        max_fix_cycles=max_fix_cycles,
        max_total_fix_iterations=max_total,
        decomposition=DecompositionConfig(enabled=False, max_subtasks=8),
        providers={},
    )


def _agent(node_id: str, *, when: WhenPredicate | None = None) -> AgentNode:
    return AgentNode(id=node_id, kind="agent", role_file=f"roles/{node_id}.md", when=when)


def _evaluator(node_id: str, *, when: WhenPredicate | None = None) -> EvaluatorNode:
    return EvaluatorNode(
        id=node_id, kind="evaluator", role="review", role_file="roles/r.md", when=when
    )


def _checks(node_id: str) -> ChecksNode:
    return ChecksNode(id=node_id, kind="checks", checker="command_profile")


def _publish(node_id: str) -> PublishNode:
    return PublishNode(id=node_id, kind="publish", policy=PublishingPolicy.PULL_REQUEST)


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


class StubRunner:
    """One runner registered for every node kind; dispatches programmed outcomes by node id."""

    def __init__(self, outcomes: dict[str, list[str]] | None = None) -> None:
        self._outcomes = {k: list(v) for k, v in (outcomes or {}).items()}
        self.calls: list[str] = []

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        self.calls.append(node.id)
        seq = self._outcomes.get(node.id)
        kind = seq.pop(0) if seq else "done"
        return NodeResult(node_id=node.id, outcome=NodeOutcome(kind), node_run_id=0)


class RecordingRecorder:
    def __init__(self) -> None:
        self.skips: list[tuple[str, str]] = []
        self.checkpoints = 0
        self.failure_reports: list[tuple[str, str | None, str]] = []

    def record_skip(self, node: FlowNode, *, reason: str, subtask_order: int | None) -> None:
        self.skips.append((node.id, reason))

    def save_checkpoint(self, run_state: FlowRunState) -> None:
        self.checkpoints += 1

    def write_failure_report(
        self,
        *,
        node_id: str,
        loop: str | None,
        limit_name: str,
        run_state: FlowRunState,
        subtask_order: int | None = None,
    ) -> str:
        self.failure_reports.append((node_id, loop, limit_name))
        return "/artifacts/failure_report.json"


def _engine(
    snapshot: FlowSnapshot,
    runner: StubRunner,
    recorder: RecordingRecorder,
    *,
    facts: dict[str, bool] | None = None,
    agents: AgentsConfig | None = None,
    diff_fingerprint: Callable[[], str] | None = None,  # EXPERIMENTAL(no-work-infra)
    is_cancelled: Callable[[], bool] = lambda: False,
) -> FlowEngine:
    facts = facts or {}
    registry = dict.fromkeys(("agent", "evaluator", "checks", "hitl", "publish"), runner)
    return FlowEngine(
        snapshot,
        FlowRunState(flow_fingerprint=snapshot.flow_fingerprint),
        registry,
        recorder,
        facts=lambda fact: facts.get(fact, False),
        agents=agents or _agents(),
        task_id="task-1",
        diff_fingerprint=diff_fingerprint,
        is_cancelled=is_cancelled,
    )


# -- tests --------------------------------------------------------------------


def test_engine_follows_declared_edge() -> None:
    snap = _snapshot(
        [_agent("a"), _agent("b"), _publish("c")],
        [Edge("a", "b"), Edge("b", "c")],
    )
    runner, recorder = StubRunner(), RecordingRecorder()
    result = _engine(snap, runner, recorder).run()
    assert result == FlowRunResult(status=Status.DONE, final_node="c")
    assert runner.calls == ["a", "b", "c"]


def test_engine_resumes_at_current_node() -> None:
    # A hydrated run_state (current_node already set) resumes mid-graph; prior nodes are not re-run.
    snap = _snapshot(
        [_agent("a"), _agent("b"), _publish("c")],
        [Edge("a", "b"), Edge("b", "c")],
    )
    runner, recorder = StubRunner(), RecordingRecorder()
    registry = dict.fromkeys(("agent", "evaluator", "checks", "hitl", "publish"), runner)
    engine = FlowEngine(
        snap,
        FlowRunState(flow_fingerprint="fp", current_node="b"),
        registry,
        recorder,
        facts=lambda fact: False,
        agents=_agents(),
        task_id="task-1",
    )
    result = engine.run()
    assert result.final_node == "c"
    assert runner.calls == ["b", "c"]  # 'a' (already completed before the interruption) is skipped


def test_engine_cancellation_stops_before_next_node_and_checkpoints_resume_point() -> None:
    snap = _snapshot(
        [_agent("a"), _agent("b"), _publish("c")],
        [Edge("a", "b"), Edge("b", "c")],
    )
    runner, recorder = StubRunner(), RecordingRecorder()
    checks = iter((False, True))  # let a finish; stop at the next clean node boundary
    engine = _engine(snap, runner, recorder, is_cancelled=lambda: next(checks))

    with pytest.raises(FlowCancelled) as raised:
        engine.run()

    assert raised.value.node_id == "b"
    assert runner.calls == ["a"]
    assert engine.run_state.current_node == "b"
    assert engine.run_state.completed_nodes == ["a"]
    assert recorder.checkpoints == 2  # a→b transition, then the idempotent cancellation save


def test_engine_outcome_not_in_edges_is_internal_error() -> None:
    # checks node may emit only pass/fail, but the stub returns an undeclared outcome.
    snap = _snapshot(
        [_checks("t"), _publish("ok"), _agent("fix")],
        [Edge("t", "ok", outcome="pass"), Edge("t", "fix", outcome="fail", budget=1)],
    )
    runner = StubRunner({"t": ["rework"]})
    with pytest.raises(EngineInternalError):
        _engine(snap, runner, RecordingRecorder()).run()


def test_engine_applies_transitions_not_nodes() -> None:
    """A runner cannot pick the next node: the engine resolves the edge from the snapshot.

    Each node's runner tries to hijack ``current_node`` to a non-existent node. If that took effect
    the engine would ``KeyError`` on the next iteration; instead it routes by the declared edges, so
    the run follows ``a -> b -> c`` to completion.
    """

    class HijackRunner(StubRunner):
        def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
            ctx.run_state.current_node = "nonexistent"  # ignored — engine owns the transition
            return super().run(node, ctx)

    snap = _snapshot([_agent("a"), _agent("b"), _publish("c")], [Edge("a", "b"), Edge("b", "c")])
    runner, recorder = HijackRunner(), RecordingRecorder()
    result = _engine(snap, runner, recorder).run()
    assert result.status is Status.DONE
    assert result.final_node == "c"
    assert runner.calls == ["a", "b", "c"]


def test_engine_when_skip_takes_single_edge() -> None:
    snap = _snapshot(
        [_agent("a", when=WhenPredicate(fact="derived.flag")), _publish("b")],
        [Edge("a", "b")],
    )
    runner, recorder = StubRunner(), RecordingRecorder()
    result = _engine(snap, runner, recorder, facts={"derived.flag": False}).run()
    assert result.status is Status.DONE
    assert runner.calls == ["b"]  # 'a' was skipped, never executed
    assert [nid for nid, _ in recorder.skips] == ["a"]


def test_engine_post_node_hook_runs_for_executed_nodes_only() -> None:
    # The post-node hook fires after each *executed* node with (node, outcome); a skipped node
    # (when=false) never fires it — slot writes / decomposition only apply to nodes that ran.
    snap = _snapshot(
        [_agent("a", when=WhenPredicate(fact="derived.flag")), _agent("b"), _publish("c")],
        [Edge("a", "b"), Edge("b", "c")],
    )
    runner, recorder = StubRunner(), RecordingRecorder()
    seen: list[str] = []
    registry = dict.fromkeys(("agent", "evaluator", "checks", "hitl", "publish"), runner)
    engine = FlowEngine(
        snap,
        FlowRunState(flow_fingerprint="fp"),
        registry,
        recorder,
        facts=lambda fact: False,  # derived.flag false => 'a' is skipped
        agents=_agents(),
        task_id="task-1",
        post_node=lambda node, outcome, node_run_id: seen.append(node.id),
    )
    result = engine.run()
    assert result.status is Status.DONE
    assert seen == ["b", "c"]  # 'a' skipped => no hook; only executed nodes


def test_engine_region_terminates_at_region_exit() -> None:
    # region = {impl, ev}; the forward edge ev--accept-->pub leaves the region, so the run stops at
    # the boundary (pub is the post-region phase the driver runs separately). The internal rework
    # back-edge ev-->impl stays in-region.
    snap = _snapshot(
        [_agent("pre"), _agent("impl"), _evaluator("ev"), _publish("pub")],
        [
            Edge("pre", "impl"),
            Edge("impl", "ev"),
            Edge("ev", "pub", outcome="accept"),
            Edge("ev", "impl", outcome="rework", budget=2),
        ],
    )
    runner, recorder = StubRunner({"ev": ["accept"]}), RecordingRecorder()
    registry = dict.fromkeys(("agent", "evaluator", "checks", "hitl", "publish"), runner)
    run_state = FlowRunState(flow_fingerprint="fp", current_node="impl")
    engine = FlowEngine(
        snap,
        run_state,
        registry,
        recorder,
        facts=lambda fact: False,
        agents=_agents(),
        task_id="task-1",
        region=frozenset({"impl", "ev"}),
    )
    result = engine.run()
    assert result.status is Status.DONE
    assert runner.calls == ["impl", "ev"]  # 'pub' (post-region) is not run by this region pass
    assert run_state.current_node == "pub"  # exited to the post-region node


def test_engine_single_fix_iterations_increment() -> None:
    # ev: rework once then accept; one rework traversal => fix_iterations increments exactly once.
    snap = _snapshot(
        [_agent("s"), _evaluator("ev"), _agent("fix"), _publish("done")],
        [
            Edge("s", "ev"),
            Edge("ev", "done", outcome="accept"),
            Edge("ev", "fix", outcome="rework", budget=5),
            Edge("fix", "ev"),
        ],
    )
    runner = StubRunner({"ev": ["rework", "accept"]})
    recorder = RecordingRecorder()
    engine = _engine(snap, runner, recorder)
    result = engine.run()
    assert result.status is Status.DONE
    assert engine.run_state.fix_iterations == 1


def test_engine_budget_exhaustion_goes_manual() -> None:
    # The named-loop case. The global-cap and inline-budget cases are the two tests
    # below. test_fix budget 2 => second consecutive fail is stuck (>= semantics).
    snap = _snapshot(
        [_agent("s"), _checks("t"), _publish("done"), _agent("fix")],
        [
            Edge("s", "t"),
            Edge("t", "done", outcome="pass"),
            Edge("t", "fix", outcome="fail", loop="test_fix"),
            Edge("fix", "t"),
        ],
        budgets={"test_fix": 2, FlowRunState.GLOBAL_FIX_KEY: 99},
    )
    runner = StubRunner({"t": ["fail", "fail", "fail"]})
    recorder = RecordingRecorder()
    result = _engine(snap, runner, recorder).run()
    assert result.status is Status.MANUAL_ACTION_REQUIRED
    assert result.stuck_loop == "test_fix"
    assert result.limit_name == "max_fix_cycles"
    assert recorder.failure_reports == [("t", "test_fix", "max_fix_cycles")]


# EXPERIMENTAL(no-work-infra): the three stall-guard tests below + this helper — remove as a unit.
def _fix_loop_snapshot() -> FlowSnapshot:
    return _snapshot(
        [_agent("s"), _checks("t"), _publish("done"), _agent("fix")],
        [
            Edge("s", "t"),
            Edge("t", "done", outcome="pass"),
            Edge("t", "fix", outcome="fail", loop="test_fix"),
            Edge("fix", "t"),
        ],
        budgets={"test_fix": 99, FlowRunState.GLOBAL_FIX_KEY: 99},
    )


def test_engine_stall_guard_aborts_on_frozen_tree() -> None:
    # A fix loop whose working tree never changes across consecutive cycles is aborted as a
    # no-effective-work stall at N=2 — BEFORE the (much larger) fix budget could be exhausted.
    runner = StubRunner({"t": ["fail", "fail", "fail", "fail"]})
    recorder = RecordingRecorder()
    result = _engine(
        _fix_loop_snapshot(), runner, recorder, diff_fingerprint=lambda: "frozen"
    ).run()
    assert result.status is Status.MANUAL_ACTION_REQUIRED
    assert result.stuck_loop == "test_fix"
    assert result.limit_name == "no_file_change"
    assert recorder.failure_reports == [("t", "test_fix", "no_file_change")]


def test_engine_stall_guard_resets_when_tree_changes() -> None:
    # A cycle that changes the tree resets the streak, so a productive loop is never mislabeled a
    # stall — here it runs normally to convergence on the third check.
    runner = StubRunner({"t": ["fail", "fail", "pass"]})
    recorder = RecordingRecorder()
    seq = iter(["d1", "d2", "d3", "d4"])
    result = _engine(
        _fix_loop_snapshot(), runner, recorder, diff_fingerprint=lambda: next(seq)
    ).run()
    assert result.status is Status.DONE
    assert recorder.failure_reports == []


def test_engine_stall_guard_inert_without_fingerprint() -> None:
    # No fingerprint callable => the stall guard is off (opt-in): the same frozen loop falls through
    # to normal budget accounting (max_fix_cycles), not the no_file_change abort.
    runner = StubRunner({"t": ["fail", "fail", "fail"]})
    recorder = RecordingRecorder()
    result = _engine(_fix_loop_snapshot(), runner, recorder, agents=_agents(max_fix_cycles=2)).run()
    assert result.status is Status.MANUAL_ACTION_REQUIRED
    assert result.limit_name == "max_fix_cycles"


def test_engine_global_cap_is_hard_stop() -> None:
    # Large per-loop budget but small global cap: the run still terminates at the global limit.
    snap = _snapshot(
        [_agent("s"), _checks("t"), _publish("done"), _agent("fix")],
        [
            Edge("s", "t"),
            Edge("t", "done", outcome="pass"),
            Edge("t", "fix", outcome="fail", loop="test_fix"),
            Edge("fix", "t"),
        ],
        budgets={"test_fix": 99, FlowRunState.GLOBAL_FIX_KEY: 2},
    )
    runner = StubRunner({"t": ["fail", "fail", "fail"]})
    result = _engine(snap, runner, RecordingRecorder()).run()
    assert result.status is Status.MANUAL_ACTION_REQUIRED
    assert result.limit_name == "max_total_fix_iterations"


def test_engine_inline_budget_allows_n_reworks() -> None:
    # budget:1 allows exactly one rework; the second is stuck.
    snap = _snapshot(
        [_agent("s"), _evaluator("ev"), _agent("fix"), _publish("done")],
        [
            Edge("s", "ev"),
            Edge("ev", "done", outcome="accept"),
            Edge("ev", "fix", outcome="rework", budget=1),
            Edge("fix", "ev"),
        ],
        budgets={FlowRunState.GLOBAL_FIX_KEY: 99},
    )
    # ev reworks every time => first rework allowed, second exhausts the inline budget.
    runner = StubRunner({"ev": ["rework", "rework", "rework"]})
    recorder = RecordingRecorder()
    result = _engine(snap, runner, recorder).run()
    assert result.status is Status.MANUAL_ACTION_REQUIRED
    assert recorder.failure_reports[0][0] == "ev"


def test_engine_inline_budget_resets_after_forward_edge() -> None:
    # An accept resolves the inline rework budget at ev, so a later rework is allowed again.
    snap = _snapshot(
        [_agent("s"), _evaluator("ev"), _agent("fix"), _checks("t"), _publish("done")],
        [
            Edge("s", "ev"),
            Edge("ev", "t", outcome="accept"),
            Edge("ev", "fix", outcome="rework", budget=1),
            Edge("fix", "ev"),
            Edge("t", "done", outcome="pass"),
            Edge("t", "ev", outcome="fail", loop="retest"),
        ],
        budgets={"retest": 99, FlowRunState.GLOBAL_FIX_KEY: 99},
    )
    # ev: rework, accept; t: fail (back to ev), then ev rework again (budget reset), accept; t pass.
    runner = StubRunner({"ev": ["rework", "accept", "rework", "accept"], "t": ["fail", "pass"]})
    result = _engine(snap, runner, RecordingRecorder()).run()
    assert result.status is Status.DONE


def test_named_loop_cumulative_total_survives_forward_edge_reset() -> None:
    # The consecutive `review_fix` counter resets to 0 on accept, but the cumulative total
    # keeps every rework — so a converged loop is attributed N reworks in the audit trail, not 0.
    snap = _snapshot(
        [_agent("s"), _evaluator("ev"), _agent("fix"), _publish("done")],
        [
            Edge("s", "ev"),
            Edge("ev", "done", outcome="accept"),
            Edge("ev", "fix", outcome="rework", loop="review_fix"),
            Edge("fix", "ev"),
        ],
        budgets={"review_fix": 99, FlowRunState.GLOBAL_FIX_KEY: 99},
    )
    runner = StubRunner({"ev": ["rework", "rework", "accept"]})
    engine = _engine(snap, runner, RecordingRecorder())
    assert engine.run().status is Status.DONE
    assert engine.run_state.counter("review_fix") == 0  # consecutive: zeroed on the accept edge
    assert engine.run_state.total("review_fix") == 2  # cumulative: both reworks retained
    assert engine.run_state.fix_iterations == 2  # global equals the single loop's total here


# -- per-node field overrides (task nodes.<id>.{model,reasoning,provider}) -----


class CapturingRunner(StubRunner):
    """Records the exact node object each runner call received (post-override)."""

    def __init__(self, outcomes: dict[str, list[str]] | None = None) -> None:
        super().__init__(outcomes)
        self.nodes: dict[str, FlowNode] = {}

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        self.nodes[node.id] = node
        return super().run(node, ctx)


def test_engine_applies_node_field_overlay() -> None:
    # The overlay patches model/reasoning/provider on the fetched node before the runner sees it.
    snap = _snapshot([_agent("impl"), _publish("c")], [Edge("impl", "c")])
    runner, recorder = CapturingRunner(), RecordingRecorder()
    registry = dict.fromkeys(("agent", "evaluator", "checks", "hitl", "publish"), runner)
    engine = FlowEngine(
        snap,
        FlowRunState(flow_fingerprint="fp"),
        registry,
        recorder,
        facts=lambda fact: False,
        agents=_agents(),
        task_id="task-1",
        node_overrides={
            "impl": {"model": "claude-opus-5", "reasoning": "high", "provider": ProviderId.CODEX}
        },
    )
    assert engine.run().status is Status.DONE
    patched = runner.nodes["impl"]
    assert isinstance(patched, AgentNode)
    assert (patched.model, patched.reasoning, patched.provider) == (
        "claude-opus-5",
        "high",
        ProviderId.CODEX,
    )


def test_engine_overlay_only_patches_matching_node() -> None:
    snap = _snapshot([_agent("a"), _agent("b"), _publish("c")], [Edge("a", "b"), Edge("b", "c")])
    runner, recorder = CapturingRunner(), RecordingRecorder()
    registry = dict.fromkeys(("agent", "evaluator", "checks", "hitl", "publish"), runner)
    engine = FlowEngine(
        snap,
        FlowRunState(flow_fingerprint="fp"),
        registry,
        recorder,
        facts=lambda fact: False,
        agents=_agents(),
        task_id="task-1",
        node_overrides={"a": {"model": "x"}},
    )
    assert engine.run().status is Status.DONE
    assert runner.nodes["a"].model == "x"
    assert runner.nodes["b"].model is None  # untouched


def test_engine_empty_overlay_is_noop() -> None:
    snap = _snapshot([_agent("a"), _publish("c")], [Edge("a", "c")])
    runner, recorder = CapturingRunner(), RecordingRecorder()
    # The default (no node_overrides) leaves nodes exactly as declared.
    result = _engine(snap, runner, recorder).run()
    assert result.status is Status.DONE
    assert runner.nodes["a"].model is None and runner.nodes["a"].provider is None
