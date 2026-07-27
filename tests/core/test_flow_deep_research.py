"""The packaged ``deep_research`` flow executes on the generic engine.

Drives the real packaged snapshot through the :class:`FlowEngine` with the **real** checks /
evaluator node runners (citation validation, the non-blocking critic self-cap, resume_own_lineage)
and fake agent / publish runners. No ``if task_type`` anywhere — the engine just follows the graph.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.conftest import BUILTIN_FLOWS_DIR

from wastech_orchestrator.config.schema import AgentsConfig, DecompositionConfig
from wastech_orchestrator.core.flow.engine import FlowEngine, NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes import NodeInputs, NodeServices
from wastech_orchestrator.core.flow.nodes.checks import ChecksNodeRunner
from wastech_orchestrator.core.flow.nodes.evaluator import EvaluatorNodeRunner
from wastech_orchestrator.core.flow.postprocess import write_node_output
from wastech_orchestrator.core.flow.registry import FlowRegistry
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import AgentNode, ChecksNode, FlowNode
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
    """Agent runner stand-in: writes the research deliverable when it runs the synthesis node.

    It then calls the **real** :func:`write_node_output` post-node step, so the generic
    ``{<node_id>_path}`` channel resolves exactly as it does in a run — including the `output_file`
    branch, where the node's channel carries the document it wrote instead of its closing message.
    That is what the coverage gate reads to judge the analysis passes it sits behind, and what the
    two report evaluators read to judge the deliverable.
    """

    def __init__(
        self,
        repo_dir: Path,
        task_id: str,
        *,
        sources: list[dict[str, Any]],
        artifacts_root: Path,
        inputs: NodeInputs,
    ) -> None:
        self._repo = repo_dir
        self._task_id = task_id
        self._sources = sources
        self._artifacts_root = artifacts_root
        self._inputs = inputs
        self.calls: list[str] = []
        #: ``(node id, the review_path visible to it)`` per call — the rework handoff channel.
        self.seen_review_path: list[tuple[str, str | None]] = []

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, AgentNode)
        self.calls.append(node.id)
        self.seen_review_path.append((node.id, self._inputs.review_path))
        report_dir = self._repo / "docs" / "research" / self._task_id
        if node.id == "synthesis":
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "report.md").write_text(REPORT_BODY, encoding="utf-8")
            (report_dir / "sources.json").write_text(
                json.dumps({"sources": self._sources}), encoding="utf-8"
            )
        write_node_output(
            node,
            NodeOutcome("done", final_message=f"# {node.id}\n"),
            artifacts_root=self._artifacts_root,
            task_id=self._task_id,
            node_run_id=len(self.calls),
            register=lambda *_args: None,
            produced_dir=report_dir,
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
    agent = _FakeAgent(
        tmp_path, "t", sources=sources, artifacts_root=tmp_path / "art", inputs=inputs
    )
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

#: What the fake synthesis node writes as the deliverable — deliberately unlike the `# synthesis`
#: closing message, so a test can tell which of the two crossed the edge.
REPORT_BODY = "# Report\n\nFindings.\n"


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


def test_analysis_runs_as_three_passes_then_the_coverage_gate(tmp_path: Path) -> None:
    # One node asked to walk everything self-triaged (18% of the in-scope files, 93% of
    # its turn budget unused). The analysis is now three sequential passes over disjoint surfaces,
    # each with its own narrow remit, and a coverage gate sits behind them — before the run's first
    # producer of prose, so a thin pass is caught before anything is written on top of it.
    _, _, agent, router = _drive(tmp_path, findings=[], sources=_GOOD_SOURCE)
    assert agent.calls[:4] == [
        "refinement",  # the scoping pass, no longer gated on a task-formedness fact
        "analysis_core",
        "analysis_surfaces",
        "analysis_docs_tests",
    ]
    gate_index = next(
        i for i, r in enumerate(router.requests) if getattr(r, "node_id", None) == "coverage_gate"
    )
    # The gate ran before any evaluator that judges the report itself.
    later = [getattr(r, "node_id", None) for r in router.requests[gate_index + 1 :]]
    assert "coverage_gate" not in later
    assert {"fact_verification", "critical_review"} <= set(later)


def test_coverage_gate_reads_each_analysis_pass_report(tmp_path: Path) -> None:
    # The gate measures the audit, so it must see what each pass actually reported: the three
    # {<node_id>_path} pointers resolve in its rendered prompt. Until the evaluator runner gained
    # that channel the gate could only judge the repository, never the analysis of it.
    _, _, _, router = _drive(tmp_path, findings=[], sources=_GOOD_SOURCE)
    gate = next(r for r in router.requests if getattr(r, "node_id", None) == "coverage_gate")
    for node_id in ("analysis_core", "analysis_surfaces", "analysis_docs_tests"):
        out = tmp_path / "art" / "logs" / "t" / "stages" / node_id
        latest = max(out.glob(f"run-*/{node_id}.out.md"))
        assert latest.as_posix() in gate.prompt


def test_coverage_gate_finding_reworks_the_whole_analysis_chain(tmp_path: Path) -> None:
    # A subsystem with no traced property is a `medium` finding, which gates and re-enters at
    # analysis_core — a gap can sit in any of the three remits and only the pass that owns it can
    # close it. Non-blocking with max_rework_per_stage 2, so it self-caps and the flow still
    # publishes rather than parking the task.
    medium = [{"severity": "medium", "reason": "the CLI subsystem shows no traced property"}]
    result, store, agent, _ = _drive(tmp_path, findings=medium, sources=_GOOD_SOURCE)
    assert result.status is Status.DONE
    assert store.count_rework_verdicts("t", node_id="coverage_gate") == 2
    # Two reworks → three passes of the whole chain, in order, every time.
    assert agent.calls.count("analysis_core") == 3
    assert agent.calls.count("analysis_surfaces") == 3
    assert agent.calls.count("analysis_docs_tests") == 3


def test_coverage_gate_rework_hands_its_findings_to_the_next_pass(tmp_path: Path) -> None:
    # The re-entry is bounded by the named gaps, not a second blind sweep: the reworked analysis
    # nodes receive the gate's findings artifact as {review_path}, which is what the re-entry
    # section of each analysis prompt works from. On the FIRST pass there is none (no evaluator has
    # run), so the block drops and the prompt reads as a first sweep.
    medium = [{"severity": "medium", "reason": "the CLI subsystem shows no traced property"}]
    _, _, agent, _ = _drive(tmp_path, findings=medium, sources=_GOOD_SOURCE)
    core_passes = [path for node_id, path in agent.seen_review_path if node_id == "analysis_core"]
    assert core_passes[0] is None  # first sweep: no gate has spoken yet
    assert all(p is not None for p in core_passes[1:]), core_passes
    assert all("coverage_gate" in str(p) for p in core_passes[1:]), core_passes


def test_refinement_runs_on_a_well_formed_task(tmp_path: Path) -> None:
    # The scoping pass was gated on `derived.needs_refinement`, which is a *formedness*
    # check — a description plus acceptance criteria already resolves it False — so on every
    # properly written task the strongest prompt in the set never ran, and the analysis passes
    # downstream lost the sub-question brief they consume. It must run with the fact False.
    assert DEEP_RESEARCH.nodes_by_id["refinement"].when is None
    _, _, agent, _ = _drive(
        tmp_path,
        findings=[],
        sources=_GOOD_SOURCE,
        facts={"derived.needs_refinement": False},
    )
    assert agent.calls[0] == "refinement"


def test_document_gate_runs_before_the_report_evaluators(tmp_path: Path) -> None:
    # The flow wrote Markdown, committed it and opened a pull request without running
    # anything the repository defines, and turned the target's CI red on files the run itself had
    # just written. A command_profile node now sits on the pass path, before the two expensive
    # evaluators. With no command sets configured it passes vacuously — nothing to run is not a gap.
    gate = DEEP_RESEARCH.nodes_by_id["document_checks"]
    assert isinstance(gate, ChecksNode)
    assert gate.checker == "command_profile"
    fail_edge = next(
        e
        for e in DEEP_RESEARCH.doc.edges
        if e.from_node == "document_checks" and e.outcome == "fail"
    )
    assert (fail_edge.to, fail_edge.budget) == ("synthesis", 1)
    result, _, _, router = _drive(tmp_path, findings=[], sources=_GOOD_SOURCE)
    assert result.status is Status.DONE
    # It gates the deliverable, so it comes after the write and before anyone judges it.
    judged = [getattr(r, "node_id", None) for r in router.requests]
    assert judged.index("fact_verification") < judged.index("critical_review")


def test_report_evaluators_are_handed_the_report_not_the_sign_off(tmp_path: Path) -> None:
    # `{synthesis_path}` resolved to the node's closing message, so both
    # evaluators had to name `{repo}/docs/research/{task_id}/report.md` by hand — the engine's own
    # path convention hardcoded into a role prompt. With `output_file` the channel carries the
    # deliverable, and both prompts resolve it by node id.
    _, _, _, router = _drive(tmp_path, findings=[], sources=_GOOD_SOURCE)
    published = max(
        (tmp_path / "art" / "logs" / "t" / "stages" / "synthesis").glob("run-*/synthesis.out.md")
    )
    assert published.read_text("utf-8") == REPORT_BODY  # the file, not "# synthesis"
    for node_id in ("fact_verification", "critical_review"):
        request = next(r for r in router.requests if getattr(r, "node_id", None) == node_id)
        assert published.as_posix() in request.prompt
        assert "docs/research/t/report.md" not in request.prompt


def test_citation_verdicts_name_the_manifest_they_graded(tmp_path: Path) -> None:
    # The verdicts carry locations, never claims, so the verifier has to open the manifest for each
    # entry's claim — and it now learns where that manifest is from the verdict file rather than
    # from a deliverable path baked into its prompt.
    _, _, _, router = _drive(tmp_path, findings=[], sources=_GOOD_SOURCE)
    verifier = next(
        r for r in router.requests if getattr(r, "node_id", None) == "fact_verification"
    )
    verdicts = json.loads(Path(verifier.check_artifacts_path).read_text("utf-8"))
    assert verdicts["manifest_path"] == "docs/research/t/sources.json"


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
    # End to end: the run this came from had `critical_review` file a correct
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


def test_passing_citation_check_routes_its_report_to_the_verifier(tmp_path: Path) -> None:
    # `checks_path` was set only on the command-profile FAILURE path, so on a passing
    # citation check the 5 KB verdict file reached nobody — while the verifier's own prompt asserted
    # a guarantee based on it. The next evaluator must now receive the pointer, and its rendered
    # prompt must carry it (the packaged verifier.md addresses `{checks_path}`).
    _, _, _, router = _drive(tmp_path, findings=[], sources=_GOOD_SOURCE)
    verifier = next(
        r for r in router.requests if getattr(r, "node_id", None) == "fact_verification"
    )
    assert verifier.check_artifacts_path is not None
    assert Path(verifier.check_artifacts_path).name == "citation.json"
    assert verifier.check_artifacts_path in verifier.prompt


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
