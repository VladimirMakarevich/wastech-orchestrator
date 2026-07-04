"""End-to-end engine driver: real engine + node runners + state-store recorder (P1.4 step A).

Exercises a tiny but valid packaged-shape flow (refine -> impl -> testing -> publish) through
``drive_flow`` with fake provider/check/git collaborators and the real
``StateStoreRunRecorder`` over a real SQLite store, asserting the run reaches a terminal ``DONE``
and every node is recorded in ``node_runs``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import AgentsConfig, DecompositionConfig
from wastech_orchestrator.core.flow.engine_driver import drive_flow
from wastech_orchestrator.core.flow.nodes import NodeInputs, NodeServices
from wastech_orchestrator.core.flow.recorder import StateStoreRunRecorder
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.snapshot import load_flow
from wastech_orchestrator.core.flow.validator import validate_flow
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.providers.base import (
    AgentRunResult,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.routing.router import ResolvedRoute, RouteSource, StageOutcome
from wastech_orchestrator.state_store import StateStore, TaskRow

_FLOW = """
flow:
  name: tiny
  task_type: tiny
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - { id: implementation, kind: agent, role_file: roles/implementation.md,
        permission_profile: workspace-write }
    - { id: testing, kind: checks, checker: command_profile }
    - { id: review, kind: evaluator, role: review, role_file: roles/review.md,
        permission_profile: read-only }
    - { id: publish, kind: publish, policy: pull_request }
  edges:
    - { from: implementation, to: testing }
    - { from: testing, to: review, outcome: pass }
    - { from: review, to: publish, outcome: accept }
"""


class _FakeRouter:
    def __init__(self) -> None:
        self.route_overrides: dict[str, Any] = {}  # node_id -> provider override seen
        self.requests: dict[str, Any] = {}  # node_id -> AgentRunRequest built for it

    def resolve_route(self, node_id: str, override: Any = None) -> ResolvedRoute:
        self.route_overrides[node_id] = override
        return ResolvedRoute(
            node_id=node_id, primary=ProviderId.CODEX, fallback=None, source=RouteSource.CONFIG
        )

    def run_stage(
        self, request: Any, route: ResolvedRoute, *, snapshot: Any = None
    ) -> StageOutcome:
        self.requests[request.node_id] = request
        # F19: the review evaluator requires a well-formed findings array; a well-formed empty one
        # is a clean, accepting verdict.
        structured = {"findings": []} if request.node_id == "review" else None
        result = AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider="codex",
            node_id=request.node_id,
            attempt=1,
            exit_code=0,
            started_at="t0",
            finished_at="t1",
            structured_output=structured,
        )
        return StageOutcome(
            route=route,
            result=result,
            provider_used=ProviderId.CODEX,
            stage_attempts=1,
            terminal_error=None,
            attempts=(),
        )


class _FakeChecks:
    def run(self, **kwargs: Any) -> Any:
        from wastech_orchestrator.check_runner import CheckOutcome

        return CheckOutcome(passed=True, runs=())


class _FakeGit:
    def commit_code(self, task_id: str, message: str) -> str | None:
        return "sha"

    def commit_audit(self, task_id: str) -> str | None:
        return "sha-audit"

    def push(self, task_id: str, branch: str, **_: object) -> bool:
        return True

    def create_pr(self, task_id: str, branch: str, *, title: str, body_path: str) -> str | None:
        return "https://example/pr/1"

    def write_current_diff(self, task_id: str) -> str:
        return "/art/current.diff"

    def changed_code_entries(self) -> tuple[Any, ...]:
        return ()

    def changed_code_paths(self) -> list[str]:
        return ["src/x.py"]  # a non-empty diff so the checks node runs its command sets

    def changed_code_paths_since_base(self) -> list[str]:
        return ["src/x.py"]  # base-inclusive selection set (what the checks node now reads)


def _agents() -> AgentsConfig:
    return AgentsConfig(
        allowed=(ProviderId.CODEX,),
        max_stage_attempts=3,
        max_fix_cycles=10,
        max_total_fix_iterations=30,
        decomposition=DecompositionConfig(enabled=False, max_subtasks=8),
        providers={},
    )


def _drive(tmp_path: Path, *, node_overrides: Any = None) -> tuple[Any, _FakeRouter, StateStore]:
    flow_dir = tmp_path / "flow"
    (flow_dir / "roles").mkdir(parents=True)
    (flow_dir / "flow.yaml").write_text(_FLOW, encoding="utf-8")
    (flow_dir / "roles" / "implementation.md").write_text("implement {task_path}", encoding="utf-8")
    (flow_dir / "roles" / "review.md").write_text("review {diff_path}", encoding="utf-8")

    snapshot = load_flow(flow_dir / "flow.yaml")
    validate_flow(snapshot)

    store = StateStore.open(tmp_path / "state.db")
    store.insert_task(TaskRow(task_id="task-1", title="T", status=Status.RUNNING))
    recorder = StateStoreRunRecorder(store, "task-1", artifacts_root=tmp_path)

    router = _FakeRouter()
    services = NodeServices(
        router=router,
        check_runner=_FakeChecks(),
        store=store,
        repo_dir=str(tmp_path / "repo"),
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        git=_FakeGit(),
    )
    inputs = NodeInputs(
        flow_dir=flow_dir,
        task_path="/t/task.md",
        branch="worc/task-1-x",
        pull_request_title="PR",
        summary_body_path="/s.md",
    )

    result = drive_flow(
        snapshot=snapshot,
        run_state=FlowRunState(flow_fingerprint=snapshot.flow_fingerprint),
        recorder=recorder,
        services=services,
        inputs=inputs,
        facts=lambda fact: True,
        agents=_agents(),
        task_id="task-1",
        **({"node_overrides": node_overrides} if node_overrides is not None else {}),
    )
    return result, router, store


def test_drive_flow_runs_tiny_flow_to_done(tmp_path: Path) -> None:
    result, _router, store = _drive(tmp_path)

    assert result.status is Status.DONE
    assert result.final_node == "publish"
    recorded = [r.node_id for r in store.get_node_runs("task-1")]
    assert recorded == ["implementation", "testing", "review", "publish"]
    # The checkpoint persisted the terminal node + the flow fingerprint.
    current_node, _counters, fingerprint = store.get_flow_checkpoint("task-1")
    assert current_node == "publish"
    fingerprint_snapshot = load_flow(tmp_path / "flow" / "flow.yaml").flow_fingerprint
    assert fingerprint == fingerprint_snapshot


def test_drive_flow_forwards_node_overrides_to_request(tmp_path: Path) -> None:
    # The resolved overlay must reach the AgentRunRequest (model/reasoning) and the route resolution
    # (provider), without any change to the runners or router.
    result, router, _store = _drive(
        tmp_path,
        node_overrides={
            "implementation": {
                "model": "claude-opus-4-8",
                "reasoning": "high",
                "provider": ProviderId.CODEX,
            }
        },
    )
    assert result.status is Status.DONE
    impl_request = router.requests["implementation"]
    assert impl_request.model == "claude-opus-4-8"
    assert impl_request.reasoning == "high"
    # The route was resolved from the overridden provider, not the flow default (None).
    assert router.route_overrides["implementation"] is ProviderId.CODEX
    # A node with no override is untouched.
    assert router.requests["review"].model is None
