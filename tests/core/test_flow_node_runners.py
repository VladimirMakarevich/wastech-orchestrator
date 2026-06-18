"""Core-owned node runners (agent / evaluator / checks) as thin adapters (P1.3).

Each runner is exercised with fake collaborators (router / store / check runner) so we can assert it
calls the collaborator and maps the result exactly like the direct orchestrator call would.
"""

from __future__ import annotations

import json
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
    HitlSettings,
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


def _stage_outcome(route: ResolvedRoute, result: AgentRunResult | None) -> StageOutcome:
    return StageOutcome(
        route=route,
        result=result,
        provider_used=ProviderId.CODEX if result else None,
        stage_attempts=1,
        terminal_error=None,
        attempts=(),
    )


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
        return _stage_outcome(route, self._result)


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
        self.provider_attempts: list[Any] = []
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

    def record_provider_attempt(self, attempt: Any, conn: Any = None) -> None:
        self.provider_attempts.append(attempt)


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
    artifacts_root: str = "/art",
) -> Any:
    return NodeServices(
        router=router,
        check_runner=check_runner,
        store=store,
        repo_dir="/repo",
        artifacts_root=artifacts_root,
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


def test_agent_node_equals_direct_router_call(tmp_path: Path) -> None:
    # The node builds the AgentRunRequest a direct router.run_stage call would receive (prompt from
    # role_file + injected paths, node-sourced permission/model) and passes the router's outcome
    # through unchanged (unconditional "done") — i.e. the wrapper adds no behavior of its own.
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

    assert result.outcome.kind == "done"  # outcome passed through, not transformed
    assert len(router.requests) == 1  # exactly one router call, like a direct invocation
    req = router.requests[0]
    assert req.prompt == "Implement /t/task.md in /repo"
    assert req.permission_profile == "workspace-write"
    assert req.model == "gpt-x"
    assert req.task_path == "/t/task.md"
    assert req.plan_path == "/t/plan.md"
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


def test_agent_workspace_write_writes_diff(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(id="impl", kind="agent", role_file="r.md",
                     permission_profile=PermissionProfile.WORKSPACE_WRITE)
    router, store, git = FakeRouter(_result()), FakeStore(), FakeGit()
    services = _services(router, store, {"impl": Stage.IMPLEMENTATION},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())), git=git)
    inputs = _inputs(tmp_path)
    AgentNodeRunner(services, inputs).run(node, _ctx(node))
    assert inputs.diff_path == "/art/current.diff"
    assert ("write_current_diff", "task-1") in git.calls


def test_agent_dangerous_diff_goes_manual(tmp_path: Path) -> None:
    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
    from wastech_orchestrator.git_manager import ChangedPath

    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(id="impl", kind="agent", role_file="r.md",
                     permission_profile=PermissionProfile.WORKSPACE_WRITE)
    git = FakeGit(changed=(ChangedPath(status="D", path="src/core.py"),))
    services = _services(FakeRouter(_result()), FakeStore(), {"impl": Stage.IMPLEMENTATION},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())), git=git)
    with pytest.raises(NodeManualRequired):
        AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


def _ws_node() -> AgentNode:
    return AgentNode(id="impl", kind="agent", role_file="r.md",
                     permission_profile=PermissionProfile.WORKSPACE_WRITE)


def _guard_services(tmp_path: Path, git: Any, notifier: Any) -> Any:
    return NodeServices(
        router=FakeRouter(_result()),
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        stage_for_node={"impl": Stage.IMPLEMENTATION},
        clock=lambda: "ts",
        git=git,
        notifier=notifier,
        ask_timeout_s=60,
    )


def test_agent_dangerous_diff_approved_proceeds(tmp_path: Path) -> None:
    from wastech_orchestrator.git_manager import ChangedPath
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    git = FakeGit(changed=(ChangedPath(status="D", path="src/x.py"),))
    notifier = FakeNotifier(AskResult(answered=True, approved=True))
    result = AgentNodeRunner(_guard_services(tmp_path, git, notifier), _inputs(tmp_path)).run(
        _ws_node(), _ctx(_ws_node())
    )
    assert result.outcome.kind == "done"
    assert notifier.asks  # an approval was requested


def test_agent_dangerous_diff_denied_reconsiders_clean(tmp_path: Path) -> None:
    from wastech_orchestrator.git_manager import ChangedPath
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    # first classify is dangerous; after the denial-driven reconsider re-run, the diff is clean.
    git = FakeGit(changed_seq=[(ChangedPath(status="D", path="src/x.py"),), ()])
    notifier = FakeNotifier(AskResult(answered=True, approved=False))
    result = AgentNodeRunner(_guard_services(tmp_path, git, notifier), _inputs(tmp_path)).run(
        _ws_node(), _ctx(_ws_node())
    )
    assert result.outcome.kind == "done"


def test_agent_dangerous_diff_denied_still_dangerous_goes_manual(tmp_path: Path) -> None:
    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
    from wastech_orchestrator.git_manager import ChangedPath
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    git = FakeGit(changed=(ChangedPath(status="D", path="src/x.py"),))  # always dangerous
    notifier = FakeNotifier(AskResult(answered=True, approved=False))
    with pytest.raises(NodeManualRequired):
        AgentNodeRunner(_guard_services(tmp_path, git, notifier), _inputs(tmp_path)).run(
            _ws_node(), _ctx(_ws_node())
        )


def test_agent_read_only_node_skips_diff_guard(tmp_path: Path) -> None:
    # summary is a read-only agent node (non-HITL stage) -> simple path, no diff guard.
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(id="summary", kind="agent", role_file="r.md",
                     permission_profile=PermissionProfile.READ_ONLY)
    git = FakeGit()
    services = _services(FakeRouter(_result()), FakeStore(), {"summary": Stage.SUMMARY},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())), git=git)
    inputs = _inputs(tmp_path)
    AgentNodeRunner(services, inputs).run(node, _ctx(node))
    assert inputs.diff_path is None
    assert git.calls == []


def _audit_services(tmp_path: Path, *, prompt_audit: bool, registered: list[Any]) -> NodeServices:
    return NodeServices(
        router=FakeRouter(_result()),
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        stage_for_node={"implementation": Stage.IMPLEMENTATION},
        clock=lambda: "ts",
        prompt_audit=prompt_audit,
        register_artifact=lambda t, k, p: registered.append((t, k, p)),
    )


def test_agent_node_writes_prompt_audit_when_enabled(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(id="implementation", kind="agent", role_file="r.md",
                     permission_profile=PermissionProfile.READ_ONLY)
    registered: list[Any] = []
    AgentNodeRunner(_audit_services(tmp_path, prompt_audit=True, registered=registered), _inputs(
        tmp_path
    )).run(node, _ctx(node))
    kinds = {k for _, k, _ in registered}
    assert {"rendered_prompt", "prompt_audit", "prompt_audit_timeline"} <= kinds


def test_agent_node_no_prompt_audit_when_disabled(tmp_path: Path) -> None:
    # prompt_audit off: rendered-prompt still written (audit-independent), but no prompt-audit JSON.
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(id="implementation", kind="agent", role_file="r.md",
                     permission_profile=PermissionProfile.READ_ONLY)
    registered: list[Any] = []
    AgentNodeRunner(_audit_services(tmp_path, prompt_audit=False, registered=registered), _inputs(
        tmp_path
    )).run(node, _ctx(node))
    kinds = {k for _, k, _ in registered}
    assert "rendered_prompt" in kinds
    assert "prompt_audit" not in kinds


# -- embedded HITL (refinement / planning) ------------------------------------


class FakeNotifier:
    """Records the prompt and returns a programmed answer; satisfies NotifierPort."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.asks: list[str] = []

    def start_ask(self, *, question: str, context: str, task_id: str, kind: str,
                  timeout_s: int, interaction_id: str, contacts: tuple[str, ...] = ()) -> Any:
        from wastech_orchestrator.notify import AskHandle

        self.asks.append(question)
        return AskHandle(interaction_id=interaction_id, kind=kind, expires_at=1.0, message_id=1)

    def wait_for_answer(self, handle: Any) -> Any:
        return self._result


def _refinement_node() -> AgentNode:
    # Refinement opts into HITL by declaring `hitl` (data-driven dispatch, not the stage name).
    return AgentNode(id="refinement", kind="agent", role_file="r.md",
                     permission_profile=PermissionProfile.READ_ONLY,
                     hitl=HitlSettings(allow_question=True))


def test_agent_hitl_no_signal_proceeds(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("refine", "utf-8")
    node = _refinement_node()
    router = FakeRouter(_result({"content": "done", "human_input": None}))
    services = _services(router, FakeStore(), {"refinement": Stage.REFINEMENT},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())),
                         artifacts_root=str(tmp_path))
    result = AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "done"


def test_agent_hitl_question_round_trip(tmp_path: Path) -> None:
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("refine", "utf-8")
    node = _refinement_node()

    class TwoShotRouter(FakeRouter):
        """First run asks a question; the re-run (with the answer) proceeds cleanly."""

        def __init__(self) -> None:
            super().__init__(None)
            self._n = 0

        def run_stage(self, request: Any, route: Any, *, snapshot: Any = None) -> Any:
            self._n += 1
            signal = (
                {"kind": "question", "question": "Which API?", "context": "",
                 "risk": "clarification", "paths": []}
                if self._n == 1
                else None
            )
            return _stage_outcome(route, _result({"content": "ok", "human_input": signal}))

        @property
        def calls(self) -> int:
            return self._n

    router = TwoShotRouter()
    notifier = FakeNotifier(AskResult(answered=True, text="use v2"))
    services = NodeServices(
        router=router,
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        stage_for_node={"refinement": Stage.REFINEMENT},
        clock=lambda: "ts",
        notifier=notifier,
        ask_timeout_s=60,
    )
    result = AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "done"
    assert notifier.asks == ["Which API?"]
    assert router.calls == 2  # initial run + re-run with the answer


def test_agent_hitl_dispatch_is_data_driven_not_stage(tmp_path: Path) -> None:
    # A node on the REFINEMENT stage but WITHOUT a declared `hitl` must NOT do a round-trip even if
    # the agent emits a signal — dispatch is by node.hitl, not the stage name (flow-contract §2.1).
    (tmp_path / "r.md").write_text("refine", "utf-8")
    node = AgentNode(id="refinement", kind="agent", role_file="r.md",
                     permission_profile=PermissionProfile.READ_ONLY)  # no hitl declared
    signal = {"kind": "question", "question": "ignored?", "context": "",
              "risk": "clarification", "paths": []}
    router = FakeRouter(_result({"content": "ok", "human_input": signal}))
    notifier = FakeNotifier(None)
    services = NodeServices(
        router=router,
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        stage_for_node={"refinement": Stage.REFINEMENT},
        clock=lambda: "ts",
        notifier=notifier,
        ask_timeout_s=60,
    )
    result = AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "done"
    assert notifier.asks == []  # no human round-trip despite the signal + refinement stage


def test_agent_hitl_timeout_goes_manual(tmp_path: Path) -> None:
    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("refine", "utf-8")
    node = _refinement_node()
    signal = {"kind": "question", "question": "?", "context": "", "risk": "clarification",
              "paths": []}
    router = FakeRouter(_result({"content": "ok", "human_input": signal}))
    notifier = FakeNotifier(AskResult(answered=False, timed_out=True, failure="timeout"))
    services = NodeServices(
        router=router,
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        stage_for_node={"refinement": Stage.REFINEMENT},
        clock=lambda: "ts",
        notifier=notifier,
        ask_timeout_s=60,
    )
    with pytest.raises(NodeManualRequired):
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
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())),
                         artifacts_root=str(tmp_path))
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == expected


def test_evaluator_non_blocking_always_accepts(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _evaluator("tq", blocking=False)
    router, store = FakeRouter(_result({"findings": [{"severity": "critical"}]})), FakeStore()
    services = _services(router, store, {"tq": Stage.REVIEW},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())),
                         artifacts_root=str(tmp_path))
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "accept"


def test_evaluator_review_writes_findings_artifact(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _evaluator("review")
    router = FakeRouter(_result({"findings": [{"title": "x", "severity": "low"}]}))
    store = FakeStore()
    inputs = _inputs(tmp_path)
    services = _services(router, store, {"review": Stage.REVIEW},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())),
                         artifacts_root=str(tmp_path))
    EvaluatorNodeRunner(services, inputs).run(node, _ctx(node))
    findings_file = Path(inputs.review_path)  # type: ignore[arg-type]
    assert findings_file.name == "findings.json"
    assert json.loads(findings_file.read_text("utf-8")) == {
        "findings": [{"title": "x", "severity": "low"}]
    }


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
    def __init__(
        self, changed: tuple[Any, ...] = (), changed_seq: list[tuple[Any, ...]] | None = None
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._changed = changed
        self._changed_seq = changed_seq

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

    def write_current_diff(self, task_id: str) -> str:
        self.calls.append(("write_current_diff", task_id))
        return "/art/current.diff"

    def changed_code_entries(self) -> tuple[Any, ...]:
        if self._changed_seq:
            return self._changed_seq.pop(0) if len(self._changed_seq) > 1 else self._changed_seq[0]
        return self._changed


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


def test_publish_pull_request_requires_body_path(tmp_path: Path) -> None:
    # branch present but no summary body path: refuse rather than open a PR with an empty body.
    node = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    git = FakeGit()
    services = _services(FakeRouter(_result()), FakeStore(), {},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())), git=git)
    inputs = _inputs(tmp_path, branch="agent/task-1-x")  # summary_body_path is None
    with pytest.raises(PublishConfigError):
        PublishNodeRunner(services, inputs).run(node, _ctx(node))
    assert git.calls == []  # nothing committed/pushed/PR'd


def test_publish_none_policy_writes_no_git(tmp_path: Path) -> None:
    node = PublishNode(id="store", kind="publish", policy=PublishingPolicy.NONE)
    git, store = FakeGit(), FakeStore()
    services = _services(FakeRouter(_result()), store, {},
                         FakeCheckRunner(CheckOutcome(passed=True, runs=())), git=git)
    result = PublishNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "done"
    assert git.calls == []
