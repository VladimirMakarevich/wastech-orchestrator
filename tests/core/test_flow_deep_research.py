"""P3.3 — the packaged ``deep_research`` flow executes on the generic engine.

Drives the real packaged snapshot through the :class:`FlowEngine` with the **real** checks /
evaluator node runners (citation validation, the non-blocking critic self-cap, resume_own_lineage)
and fake agent / publish runners. No ``if task_type`` anywhere — the engine just follows the graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.conftest import BUILTIN_FLOWS_DIR

from wastech_orchestrator.config.schema import AgentsConfig, DecompositionConfig
from wastech_orchestrator.core.flow.engine import FlowEngine, NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes import NodeInputs, NodeServices
from wastech_orchestrator.core.flow.nodes.checks import ChecksNodeRunner
from wastech_orchestrator.core.flow.nodes.evaluator import EvaluatorNodeRunner
from wastech_orchestrator.core.flow.registry import FlowRegistry
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import FlowNode
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.providers.base import AgentRunResult, ProviderId, RunStatus
from wastech_orchestrator.routing.router import ResolvedRoute, RouteSource, StageOutcome
from wastech_orchestrator.state_store import EvaluationRow, NodeLineageRow

# Built-ins resolve only from a delivered `.worc/flows/` (no packaged fallback); point the registry
# at the packaged flows tree, which is what `worc install` copies there.
DEEP_RESEARCH = FlowRegistry(operator_flows_dir=BUILTIN_FLOWS_DIR).resolve("deep_research")


# -- store / router / runner fakes --------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.evaluations: list[EvaluationRow] = []
        self.node_lineage: dict[tuple[str, str, int], NodeLineageRow] = {}
        self._next = 1

    def record_node_run(self, run: Any, conn: Any = None) -> int:
        rid = self._next
        self._next += 1
        return rid

    def complete_node_run(self, run_id: int, **kwargs: Any) -> None:
        pass

    def record_check_run(self, run: Any, conn: Any = None) -> None:
        pass

    def record_provider_attempt(self, attempt: Any, conn: Any = None) -> None:
        pass

    def record_evaluation(self, row: EvaluationRow, conn: Any = None) -> int:
        self.evaluations.append(row)
        return len(self.evaluations)

    def count_rework_verdicts(
        self, task_id: str, *, node_id: str | None = None, subtask_order: int | None = None
    ) -> int:
        return sum(
            1
            for e in self.evaluations
            if e.kind == "in_flow_verdict"
            and e.verdict == "rework"
            and (node_id is None or e.node_id == node_id)
        )

    def get_node_lineage(
        self, task_id: str, node_id: str, subtask_order: int | None = None
    ) -> NodeLineageRow | None:
        return self.node_lineage.get((task_id, node_id, subtask_order or -1))

    def upsert_node_lineage(self, row: NodeLineageRow, conn: Any = None) -> None:
        self.node_lineage[(row.task_id, row.node_id, row.subtask_order or -1)] = row


class _Router:
    """Returns a fixed verdict for every evaluator pass; records each request's session_id."""

    def __init__(self, *, findings: list[dict[str, Any]], session_id: str | None = None) -> None:
        self._findings = findings
        self._session_id = session_id
        self.requests: list[Any] = []
        self.session_counter = 0

    def resolve_route(self, node_id: str, override: Any = None) -> ResolvedRoute:
        return ResolvedRoute(
            node_id=node_id, primary=ProviderId.CODEX, fallback=None, source=RouteSource.CONFIG
        )

    def run_stage(
        self, request: Any, route: ResolvedRoute, *, snapshot: Any = None
    ) -> StageOutcome:
        self.requests.append(request)
        # A fresh session id per pass when the node did not resume one (so resume can be observed).
        if request.session_id is not None:
            sid = request.session_id
        else:
            self.session_counter += 1
            sid = self._session_id and f"{self._session_id}-{self.session_counter}"
        result = AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider="codex",
            node_id=request.node_id,
            attempt=1,
            exit_code=0,
            started_at="t0",
            finished_at="t1",
            structured_output={"findings": list(self._findings)},
            session_id=sid,
        )
        return StageOutcome(
            route=route,
            result=result,
            provider_used=ProviderId.CODEX,
            stage_attempts=1,
            terminal_error=None,
            attempts=(),
        )


class _FakeAgent:
    """Agent runner stand-in: writes the research deliverable when it runs the synthesis node."""

    def __init__(self, repo_dir: Path, task_id: str, *, sources: list[dict[str, Any]]) -> None:
        self._repo = repo_dir
        self._task_id = task_id
        self._sources = sources
        self.calls: list[str] = []

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        self.calls.append(node.id)
        if node.id == "synthesis":
            import json

            out = self._repo / "docs" / "research" / self._task_id
            out.mkdir(parents=True, exist_ok=True)
            (out / "report.md").write_text("# Report\n\nFindings.\n", encoding="utf-8")
            (out / "sources.json").write_text(
                json.dumps({"sources": self._sources}), encoding="utf-8"
            )
        return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=0)


class _FakePassthrough:
    """A runner that returns the pass-through outcome for its kind (publish/hitl)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        self.calls.append(node.id)
        return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=0)


class _Recorder:
    def __init__(self) -> None:
        self.skips: list[str] = []

    def record_skip(self, node: FlowNode, *, reason: str, subtask_order: int | None) -> None:
        self.skips.append(node.id)

    def save_checkpoint(self, run_state: FlowRunState) -> None:
        pass

    def write_failure_report(self, **kwargs: Any) -> str:
        return "/artifacts/failure_report.json"


def _agents() -> AgentsConfig:
    return AgentsConfig(
        allowed=(ProviderId.CLAUDE, ProviderId.CODEX),
        max_stage_attempts=3,
        max_fix_cycles=99,
        max_total_fix_iterations=99,
        decomposition=DecompositionConfig(enabled=False, max_subtasks=8),
        providers={},
    )


def _services(tmp_path: Path, store: _Store, router: _Router) -> NodeServices:
    return NodeServices(
        router=router,  # type: ignore[arg-type]
        check_runner=None,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        repo_dir=str(tmp_path),
        artifacts_root=str(tmp_path / "art"),
        clock=lambda: "ts",
    )


def _drive(
    tmp_path: Path,
    *,
    findings: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    facts: dict[str, bool] | None = None,
    session_id: str | None = None,
) -> tuple[Any, _Store, _FakeAgent, _Router]:
    store = _Store()
    router = _Router(findings=findings, session_id=session_id)
    services = _services(tmp_path, store, router)
    inputs = NodeInputs(flow_dir=DEEP_RESEARCH.source_path.parent)  # type: ignore[union-attr]
    agent = _FakeAgent(tmp_path, "t", sources=sources)
    publish = _FakePassthrough()
    runners = {
        "agent": agent,
        "evaluator": EvaluatorNodeRunner(services, inputs),
        "checks": ChecksNodeRunner(services, inputs),
        "hitl": _FakePassthrough(),
        "publish": publish,
    }
    resolved_facts = {"derived.needs_refinement": False, "config.external_research": True}
    resolved_facts.update(facts or {})
    engine = FlowEngine(
        DEEP_RESEARCH,
        FlowRunState(flow_fingerprint=DEEP_RESEARCH.flow_fingerprint),
        runners,  # type: ignore[arg-type]
        _Recorder(),
        facts=lambda fact: resolved_facts.get(fact, False),
        agents=_agents(),
        task_id="t",
    )
    return engine.run(), store, agent, router


_GOOD_SOURCE = [{"id": "s1", "claim": "exists", "path": "docs/research/t/report.md"}]


# -- tests --------------------------------------------------------------------


def test_citation_loop_pinned_one() -> None:
    # The citation_check → synthesis (fail) edge is pinned to a single rework (v1).
    edge = next(
        e
        for e in DEEP_RESEARCH.doc.edges
        if e.from_node == "citation_check" and e.to == "synthesis" and e.outcome == "fail"
    )
    assert edge.budget == 1


def test_research_external_research_optional_skip(tmp_path: Path) -> None:
    # config.external_research=False → the node is skipped and the flow proceeds straight through.
    result, _, agent, _ = _drive(
        tmp_path, findings=[], sources=_GOOD_SOURCE, facts={"config.external_research": False}
    )
    assert result.status is Status.DONE
    assert "external_research" not in agent.calls
    assert "architecture_design" in agent.calls  # the skip passed straight through to the next node


def test_research_happy_path_produces_report_and_sources(tmp_path: Path) -> None:
    # Clean evaluators (no findings) + a valid citation manifest → straight to publish, files made.
    result, _, agent, _ = _drive(tmp_path, findings=[], sources=_GOOD_SOURCE)
    assert result.status is Status.DONE
    assert result.final_node == "publish"
    report_dir = tmp_path / "docs" / "research" / "t"
    assert (report_dir / "report.md").is_file()
    assert (report_dir / "sources.json").is_file()


def test_research_non_blocking_exhaustion_publishes_with_open_questions(tmp_path: Path) -> None:
    # The critic keeps finding issues; being non-blocking it self-caps at its budget and ACCEPTS,
    # so the flow reaches publish (DONE) — never manual_action_required.
    blocking = [{"severity": "high", "reason": "needs more depth"}]
    result, store, _, _ = _drive(tmp_path, findings=blocking, sources=_GOOD_SOURCE)
    assert result.status is Status.DONE
    assert result.final_node == "publish"
    # critical_review reworked up to its per-instance budget (3), then accepted (not manual).
    critic_reworks = store.count_rework_verdicts("t", node_id="critical_review")
    assert critic_reworks == 3


def test_research_medium_finding_gates_and_self_caps(tmp_path: Path) -> None:
    # P0.1/DR-1 end to end: the run this campaign came from had `critical_review` file a correct
    # `medium` finding and the engine returned `accept` on the spot, because gate_severity defaulted
    # to `high`. With the flow pinning `medium` the same finding drives rework rounds up to the
    # critic's max_rework_per_stage (3) and only then accepts — reaching publish, never manual.
    medium = [{"severity": "medium", "reason": "uneven audit depth"}]
    result, store, _, _ = _drive(tmp_path, findings=medium, sources=_GOOD_SOURCE)
    assert result.status is Status.DONE
    assert result.final_node == "publish"
    assert store.count_rework_verdicts("t", node_id="critical_review") == 3
    # fact_verification is non-blocking with max_rework_per_stage 1, so it spends its single round.
    assert store.count_rework_verdicts("t", node_id="fact_verification") == 1


def test_research_low_finding_stays_advisory(tmp_path: Path) -> None:
    # The other half of the gate: `medium` is the floor, so a `low` finding is still advisory and
    # costs no rework round. Without this, lowering the gate would read as "any finding blocks".
    result, store, _, _ = _drive(
        tmp_path, findings=[{"severity": "low", "reason": "nit"}], sources=_GOOD_SOURCE
    )
    assert result.status is Status.DONE
    assert store.count_rework_verdicts("t", node_id="critical_review") == 0


def test_research_broken_citation_fails_then_reworks(tmp_path: Path) -> None:
    # A hallucinated citation makes citation_check fail → synthesis rework (budget 1). The fake
    # synthesis rewrites the same (still-broken) manifest, so the pinned single rework is spent and
    # the flow stops manual — proving the loop is bounded at one.
    bad = [{"id": "ghost", "path": "src/does_not_exist.py"}]
    result, _, _, _ = _drive(tmp_path, findings=[], sources=bad)
    assert result.status is Status.MANUAL_ACTION_REQUIRED
    assert result.final_node == "citation_check"


def test_critic_resume_own_lineage_across_rounds(tmp_path: Path) -> None:
    # The critic's resume_own_lineage session is stored on round 1 and resumed on round 2 (it
    # remembers what it flagged). Driving the whole flow with a blocking critic exercises multiple
    # critic rounds; the second round's request must carry the first round's session id.
    _, store, _, router = _drive(
        tmp_path,
        findings=[{"severity": "high", "reason": "again"}],
        sources=_GOOD_SOURCE,
        session_id="critic-sess",
    )
    critic_requests = [
        r for r in router.requests if getattr(r, "node_id", None) == "critical_review"
    ]
    # The critic node persisted its own lineage, and a later critic pass resumed that session id.
    assert ("t", "critical_review", -1) in store.node_lineage
    resumed = [r.session_id for r in critic_requests if r.session_id is not None]
    assert resumed, "a later critic round should resume the stored session id"
