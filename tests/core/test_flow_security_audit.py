"""P3.4 — the packaged ``security_audit`` flow executes, and the co-design abstraction gate.

The audit flow drives through the generic engine with the real dependency_scan checker, the real
non-blocking finding-verification evaluator, and the real private-report publish (git untouched).
The final gate, :func:`test_codesign_all_three_flows_generic`, drives **all three** packaged flows
through one engine with one generic runner registry — proof that the palette carries the flows as
data with no domain knowledge in the engine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import AgentsConfig, DecompositionConfig
from wastech_orchestrator.core.flow.engine import FlowEngine, NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes import NodeInputs, NodeServices
from wastech_orchestrator.core.flow.nodes.checks import ChecksNodeRunner
from wastech_orchestrator.core.flow.nodes.evaluator import EvaluatorNodeRunner
from wastech_orchestrator.core.flow.nodes.publish import PublishNodeRunner
from wastech_orchestrator.core.flow.registry import FlowRegistry
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import ChecksNode, EvaluatorNode, FlowNode
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.providers.base import AgentRunResult, ProviderId, RunStatus
from wastech_orchestrator.providers.process import ProcessResult
from wastech_orchestrator.routing.router import ResolvedRoute, RouteSource, StageOutcome

_REGISTRY = FlowRegistry()
SECURITY_AUDIT = _REGISTRY.resolve("security_audit")


# -- fakes --------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.evaluations: list[Any] = []
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

    def record_evaluation(self, row: Any, conn: Any = None) -> int:
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

    def get_node_lineage(self, *a: Any, **k: Any) -> None:
        return None

    def upsert_node_lineage(self, *a: Any, **k: Any) -> None:
        pass


class _Router:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self._findings = findings

    def resolve_route(self, node_id: str, override: Any = None) -> ResolvedRoute:
        return ResolvedRoute(
            node_id=node_id, primary=ProviderId.CODEX, fallback=None, source=RouteSource.CONFIG
        )

    def run_stage(
        self, request: Any, route: ResolvedRoute, *, snapshot: Any = None
    ) -> StageOutcome:
        result = AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider="codex",
            node_id=request.node_id,
            attempt=1,
            exit_code=0,
            started_at="t0",
            finished_at="t1",
            structured_output={"findings": list(self._findings)},
        )
        return StageOutcome(
            route=route,
            result=result,
            provider_used=ProviderId.CODEX,
            stage_attempts=1,
            terminal_error=None,
            attempts=(),
        )


class _Git:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def changed_code_entries(self) -> tuple[Any, ...]:
        return ()  # .worc/ is gitignored → the private report never shows as a tracked change

    def commit_code(self, *a: Any, **k: Any) -> str | None:
        self.calls.append("commit_code")
        return "sha"

    def commit_audit(self, *a: Any, **k: Any) -> str | None:
        self.calls.append("commit_audit")
        return "sha"

    def push(self, *a: Any, **k: Any) -> bool:
        self.calls.append("push")
        return True

    def create_pr(self, *a: Any, **k: Any) -> str | None:
        self.calls.append("create_pr")
        return "url"


class _FakeAgent:
    def __init__(self, repo_dir: Path, task_id: str) -> None:
        self._repo = repo_dir
        self._task_id = task_id
        self.calls: list[str] = []

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        self.calls.append(node.id)
        if node.id == "report":
            out = self._repo / ".worc" / "security-reports" / self._task_id
            out.mkdir(parents=True, exist_ok=True)
            (out / "report.md").write_text("# Audit\n\nNo critical findings.\n", encoding="utf-8")
        return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=0)


def _clean_process(*args: Any, **kwargs: Any) -> ProcessResult:
    # A scanner that ran clean (no vulnerabilities); the runner created the stdout_path logs dir.
    return ProcessResult(0, False, None, 0.1, str(kwargs["stdout_path"]), "")


class _Recorder:
    def record_skip(self, node: FlowNode, *, reason: str, subtask_order: int | None) -> None:
        pass

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
        decomposition=DecompositionConfig(
            enabled=False, max_subtasks=8, min_size_signal="large", commit_per_subtask=True
        ),
        providers={},
    )


def _drive_audit(
    tmp_path: Path, *, findings: list[dict[str, Any]]
) -> tuple[Any, _Store, _FakeAgent, _Git]:
    store, git = _Store(), _Git()
    services = NodeServices(
        router=_Router(findings),  # type: ignore[arg-type]
        check_runner=None,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        repo_dir=str(tmp_path),
        artifacts_root=str(tmp_path / "art"),
        clock=lambda: "ts",
        git=git,  # type: ignore[arg-type]
        run_process=_clean_process,
        scan_timeout_s=60,
    )
    inputs = NodeInputs(flow_dir=SECURITY_AUDIT.source_path.parent)  # type: ignore[union-attr]
    agent = _FakeAgent(tmp_path, "t")
    runners = {
        "agent": agent,
        "evaluator": EvaluatorNodeRunner(services, inputs),
        "checks": ChecksNodeRunner(services, inputs),
        "hitl": agent,
        "publish": PublishNodeRunner(services, inputs),
    }
    engine = FlowEngine(
        SECURITY_AUDIT,
        FlowRunState(flow_fingerprint=SECURITY_AUDIT.flow_fingerprint),
        runners,  # type: ignore[arg-type]
        _Recorder(),
        facts=lambda fact: False,
        agents=_agents(),
        task_id="t",
    )
    return engine.run(), store, agent, git


# -- tests --------------------------------------------------------------------


def test_audit_happy_path_writes_private_report(tmp_path: Path) -> None:
    result, _, _, _ = _drive_audit(tmp_path, findings=[])
    assert result.status is Status.DONE
    assert result.final_node == "private_storage"
    report = tmp_path / ".worc" / "security-reports" / "t" / "report.md"
    assert report.is_file()


def test_audit_repo_unchanged(tmp_path: Path) -> None:
    # publishing: none → the private_storage node touches git not at all (repo byte-for-byte).
    _, _, _, git = _drive_audit(tmp_path, findings=[])
    assert git.calls == []


def test_finding_verification_marks_false_positives_non_blocking(tmp_path: Path) -> None:
    # The verifier keeps flagging threats; being non-blocking it self-caps at its budget (2) and
    # accepts, so the audit still reaches its private report — never manual_action_required.
    result, store, _, _ = _drive_audit(tmp_path, findings=[{"severity": "high", "reason": "fp?"}])
    assert result.status is Status.DONE
    assert result.final_node == "private_storage"
    assert store.count_rework_verdicts("t", node_id="finding_verification") == 2


# -- the co-design abstraction gate (P3.4) ------------------------------------


class _GenericRunner:
    """One runner for every kind: returns each kind's generic pass-through outcome — no node names,
    no flow knowledge. The same instance drives all three packaged flows."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        self.calls.append(node.id)
        if isinstance(node, ChecksNode):
            kind = "pass"
        elif isinstance(node, EvaluatorNode):
            kind = "accept"
        else:  # agent / hitl / publish
            kind = "done"
        return NodeResult(node_id=node.id, outcome=NodeOutcome(kind), node_run_id=0)


def test_codesign_all_three_flows_generic() -> None:
    # The final abstraction gate: all three packaged flows are carried by the same engine + the same
    # generic per-kind runner registry, with a permissive fact resolver, and each drives to a clean
    # terminal node. The palette expresses three very different flows as data — no `if task_type`.
    for task_type in ("implementation", "deep_research", "security_audit"):
        snapshot = _REGISTRY.resolve(task_type)
        runner = _GenericRunner()
        registry = dict.fromkeys(("agent", "evaluator", "checks", "hitl", "publish"), runner)
        engine = FlowEngine(
            snapshot,
            FlowRunState(flow_fingerprint=snapshot.flow_fingerprint),
            registry,  # type: ignore[arg-type]
            _Recorder(),
            facts=lambda fact: True,  # permissive: run every optional node
            agents=_agents(),
            task_id="t",
        )
        result = engine.run()
        assert result.status is Status.DONE, f"{task_type} did not reach a clean terminal"


def test_engine_has_no_domain_knowledge() -> None:
    # The genericness is structural too: the engine module names no flow, no task_type, and no
    # checker/role — it only follows declared edges and budgets.
    import wastech_orchestrator.core.flow.engine as engine_mod

    source = Path(engine_mod.__file__).read_text(encoding="utf-8")
    for token in (
        "deep_research",
        "security_audit",
        "task_type",
        "citation",
        "dependency_scan",
        "synthesis",
        "threat_analysis",
    ):
        assert token not in source, f"engine.py must not name {token!r}"
