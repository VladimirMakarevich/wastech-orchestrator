"""Core-owned node runners (agent / evaluator / checks) as thin adapters (P1.3).

Each runner is exercised with fake collaborators (router / store / check runner) so we can assert it
calls the collaborator and maps the result exactly like the direct orchestrator call would.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from wastech_orchestrator.check_runner import CheckOutcome, CheckRunResult
from wastech_orchestrator.core.flow.contracts import (
    EvaluationKind,
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
    SessionScope,
)
from wastech_orchestrator.core.flow.engine import NodeContext
from wastech_orchestrator.core.flow.nodes import (
    AgentNodeRunner,
    CheckLaunchError,
    ChecksNodeRunner,
    EvaluatorNodeRunner,
    NodeInputs,
    NodeServices,
    PublishConfigError,
    PublishNodeRunner,
)
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import (
    AgentNode,
    ChecksNode,
    EvaluatorNode,
    FlowDoc,
    FlowNode,
    PublishNode,
)
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.providers.base import (
    AgentRunResult,
    ProviderId,
    RunStatus,
    Stage,
)
from wastech_orchestrator.routing.router import ResolvedRoute, RouteSource, StageOutcome

# -- fakes & builders ---------------------------------------------------------


class FakeRouter:
    def __init__(self, result: AgentRunResult | None) -> None:
        self._result = result
        self.requests: list[Any] = []

    def resolve_route(self, stage: Stage, override: Any = None) -> ResolvedRoute:
        return ResolvedRoute(
            stage=stage, primary=ProviderId.CODEX, fallback=None, source=RouteSource.CONFIG
        )

    def run_stage(
        self, request: Any, route: ResolvedRoute, *, snapshot: Any = None
    ) -> StageOutcome:
        self.requests.append(request)
        return StageOutcome(
            route=route,
            result=self._result,
            provider_used=ProviderId.CODEX if self._result else None,
            stage_attempts=1,
            terminal_error=None,
            attempts=(),
        )


class FakeCheckRunner:
    def __init__(self, outcome: CheckOutcome) -> None:
        self._outcome = outcome

    def run(self, **kwargs: Any) -> CheckOutcome:
        return self._outcome


class FakeStore:
    def __init__(self) -> None:
        self.recorded: list[Any] = []
        self.completed: list[dict[str, Any]] = []
        self.check_runs: list[Any] = []
        self._next = 1

    def record_node_run(self, run: Any, conn: Any = None) -> int:
        rid = self._next
        self._next += 1
        self.recorded.append(run)
        return rid

    def complete_node_run(self, run_id: int, **kwargs: Any) -> None:
        self.completed.append({"run_id": run_id, **kwargs})

    def record_check_run(self, run: Any, conn: Any = None) -> None:
        self.check_runs.append(run)


def _result(structured: dict[str, Any] | None = None) -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="codex",
        stage=Stage.IMPLEMENTATION,
        attempt=1,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        structured_output=structured,
    )


def _snapshot(node: FlowNode) -> FlowSnapshot:
    doc = FlowDoc(
        name="t",
        task_type="t",
        permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
        output_policy=OutputPolicy.CODE_CHANGE,
        publishing=PublishingPolicy.PULL_REQUEST,
        nodes=(node,),
        edges=(),
        budgets=MappingProxyType({}),
    )
    return FlowSnapshot(
        doc=doc,
        nodes_by_id=MappingProxyType({node.id: node}),
        adjacency=MappingProxyType({}),
        flow_fingerprint="fp",
    )


def _services(
    router: Any,
    store: FakeStore,
    stage_map: dict[str, Stage],
    check_runner: Any,
    git: Any = None,
) -> Any:
    return NodeServices(
        router=router,
        check_runner=check_runner,
        store=store,
        repo_dir="/repo",
        artifacts_root="/art",
        stage_for_node=stage_map,
        clock=lambda: "ts",
        default_timeout_seconds=100,
        git=git,
    )


def _ctx(node: FlowNode) -> NodeContext:
    return NodeContext(
        snapshot=_snapshot(node),
        run_state=FlowRunState(flow_fingerprint="fp"),
        node=node,
        task_id="task-1",
    )


def _inputs(flow_dir: Path, **kw: Any) -> NodeInputs:
    return NodeInputs(flow_dir=flow_dir, **kw)


# -- agent --------------------------------------------------------------------


def test_agent_node_builds_request_and_returns_done(tmp_path: Path) -> None:
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "impl.md").write_text("Implement {task_path} in {repo}", "utf-8")
    node = AgentNode(
        id="impl",
        kind="agent",
        role_file="roles/impl.md",
        session_scope=SessionScope.EDITING_LINEAGE,
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
        model="gpt-x",
    )
    router, store = FakeRouter(_result()), FakeStore()
    services = _services(router, store, {"impl": Stage.IMPLEMENTATION}, FakeCheckRunner(
        CheckOutcome(passed=True, runs=())
    ))
    inputs = _inputs(tmp_path, task_path="/t/task.md", plan_path="/t/plan.md")
    result = AgentNodeRunner(services, inputs).run(node, _ctx(node))

    assert result.outcome.kind == "done"
    req = router.requests[0]
    assert req.prompt == "Implement /t/task.md in /repo"
    assert req.permission_profile == "workspace-write"
    assert req.model == "gpt-x"
    assert req.task_path == "/t/task.md"
    assert req.working_directory == "/repo"
    assert store.completed[-1]["outcome"] == "done"


def test_agent_node_infra_exhaustion_raises(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(id="impl", kind="agent", role_file="r.md",
                     permission_profile=PermissionProfile.WORKSPACE_WRITE)
    router, store = FakeRouter(None), FakeStore()  # result None => infra-exhausted
    services = _services(router, store, {"impl": Stage.IMPLEMENTATION},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())))
    from wastech_orchestrator.core.flow.nodes.base import NodeInfraError

    with pytest.raises(NodeInfraError):
        AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


# -- evaluator ----------------------------------------------------------------


def _evaluator(node_id: str, *, blocking: bool = True,
               kind: EvaluationKind = EvaluationKind.STAGE_OUTPUT) -> EvaluatorNode:
    return EvaluatorNode(
        id=node_id,
        kind="evaluator",
        role="review",
        role_file="r.md",
        permission_profile=PermissionProfile.READ_ONLY,
        evaluation_kind=kind,
        blocking=blocking,
    )


@pytest.mark.parametrize(
    ("structured", "expected"),
    [
        ({"findings": [{"title": "x", "severity": "high"}]}, "rework"),
        ({"findings": [{"title": "x", "severity": "low"}]}, "accept"),
        ({"findings": []}, "accept"),
    ],
)
def test_evaluator_maps_blocking_findings(tmp_path: Path, structured: dict[str, Any],
                                          expected: str) -> None:
    (tmp_path / "r.md").write_text("review {diff_path}", "utf-8")
    node = _evaluator("review")
    router, store = FakeRouter(_result(structured)), FakeStore()
    services = _services(router, store, {"review": Stage.REVIEW},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())))
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == expected


def test_evaluator_non_blocking_always_accepts(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _evaluator("tq", blocking=False)
    router, store = FakeRouter(_result({"findings": [{"severity": "critical"}]})), FakeStore()
    services = _services(router, store, {"tq": Stage.REVIEW},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())))
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "accept"


def test_evaluator_final_handoff_is_done(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("summary", "utf-8")
    node = _evaluator("summary", blocking=False, kind=EvaluationKind.FINAL_HANDOFF)
    router, store = FakeRouter(_result({"findings": [{"severity": "high"}]})), FakeStore()
    services = _services(router, store, {"summary": Stage.SUMMARY},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())))
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "done"


# -- checks -------------------------------------------------------------------


def _checks_node() -> ChecksNode:
    return ChecksNode(id="testing", kind="checks", checker="command_profile")


def _run(passed: bool) -> CheckRunResult:
    return CheckRunResult(command="pytest", exit_code=0 if passed else 1,
                          timed_out=False, passed=passed, log_path="/l")


def test_checks_pass_outcome(tmp_path: Path) -> None:
    node = _checks_node()
    store = FakeStore()
    services = _services(FakeRouter(_result()), store, {},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=(_run(True),))))
    result = ChecksNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "pass"
    assert len(store.check_runs) == 1


def test_checks_fail_outcome(tmp_path: Path) -> None:
    node = _checks_node()
    store = FakeStore()
    services = _services(FakeRouter(_result()), store, {},
                         FakeCheckRunner(CheckOutcome(passed=False, runs=(_run(False),),
                                                      first_failure_log="/log")))
    result = ChecksNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "fail"


def test_checks_launch_failure_is_infra(tmp_path: Path) -> None:
    node = _checks_node()
    store = FakeStore()
    services = _services(FakeRouter(_result()), store, {},
                         FakeCheckRunner(CheckOutcome(passed=False, runs=(),
                                                      launch_failed=True,
                                                      first_launch_error="boom")))
    with pytest.raises(CheckLaunchError):
        ChecksNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


# -- publish ------------------------------------------------------------------


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def commit_code(self, task_id: str, message: str) -> str | None:
        self.calls.append(("commit_code", task_id, message))
        return "sha-code"

    def commit_audit(self, task_id: str) -> str | None:
        self.calls.append(("commit_audit", task_id))
        return "sha-audit"

    def push(self, task_id: str, branch: str) -> bool:
        self.calls.append(("push", task_id, branch))
        return True

    def create_pr(self, task_id: str, branch: str, *, title: str, body_path: str) -> str | None:
        self.calls.append(("create_pr", task_id, branch, title, body_path))
        return "https://example/pr/1"


def test_publish_pull_request_runs_git_sequence(tmp_path: Path) -> None:
    node = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    git, store = FakeGit(), FakeStore()
    services = _services(FakeRouter(_result()), store, {},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())), git=git)
    inputs = _inputs(tmp_path, branch="agent/task-1-x", pr_title="My PR",
                     summary_body_path="/s/summary.md")
    result = PublishNodeRunner(services, inputs).run(node, _ctx(node))
    assert result.outcome.kind == "done"
    assert [c[0] for c in git.calls] == ["commit_code", "commit_audit", "push", "create_pr"]
    assert git.calls[-1] == ("create_pr", "task-1", "agent/task-1-x", "My PR", "/s/summary.md")
    assert store.completed[-1]["commit_sha_after"] == "https://example/pr/1"


def test_publish_pull_request_requires_branch(tmp_path: Path) -> None:
    node = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    services = _services(FakeRouter(_result()), FakeStore(), {},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())), git=FakeGit())
    with pytest.raises(PublishConfigError):
        PublishNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


def test_publish_none_policy_writes_no_git(tmp_path: Path) -> None:
    node = PublishNode(id="store", kind="publish", policy=PublishingPolicy.NONE)
    git, store = FakeGit(), FakeStore()
    services = _services(FakeRouter(_result()), store, {},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())), git=git)
    result = PublishNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "done"
    assert git.calls == []
