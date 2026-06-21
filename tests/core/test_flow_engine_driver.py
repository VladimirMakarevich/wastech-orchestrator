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
    Stage,
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
    def resolve_route(self, stage: Stage, override: Any = None) -> ResolvedRoute:
        return ResolvedRoute(
            stage=stage, primary=ProviderId.CODEX, fallback=None, source=RouteSource.CONFIG
        )

    def run_stage(
        self, request: Any, route: ResolvedRoute, *, snapshot: Any = None
    ) -> StageOutcome:
        result = AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider="codex",
            stage=request.stage,
            attempt=1,
            exit_code=0,
            started_at="t0",
            finished_at="t1",
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

    def push(self, task_id: str, branch: str) -> bool:
        return True

    def create_pr(self, task_id: str, branch: str, *, title: str, body_path: str) -> str | None:
        return "https://example/pr/1"

    def write_current_diff(self, task_id: str) -> str:
        return "/art/current.diff"

    def changed_code_entries(self) -> tuple[Any, ...]:
        return ()


def _agents() -> AgentsConfig:
    return AgentsConfig(
        allowed=(ProviderId.CODEX,),
        max_stage_attempts=3,
        max_fix_cycles=10,
        max_total_fix_iterations=30,
        decomposition=DecompositionConfig(
            enabled=False, max_subtasks=8, min_size_signal="large", commit_per_subtask=True
        ),
        providers={},
    )


def test_drive_flow_runs_tiny_flow_to_done(tmp_path: Path) -> None:
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

    services = NodeServices(
        router=_FakeRouter(),
        check_runner=_FakeChecks(),
        store=store,
        repo_dir=str(tmp_path / "repo"),
        artifacts_root=str(tmp_path),
        stage_for_node={"implementation": Stage.IMPLEMENTATION, "review": Stage.REVIEW},
        clock=lambda: "ts",
        git=_FakeGit(),
    )
    inputs = NodeInputs(
        flow_dir=flow_dir,
        task_path="/t/task.md",
        branch="agent/task-1-x",
        pr_title="PR",
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
    )

    assert result.status is Status.DONE
    assert result.final_node == "publish"
    recorded = [r.node_id for r in store.get_node_runs("task-1")]
    assert recorded == ["implementation", "testing", "review", "publish"]
    # The checkpoint persisted the terminal node + the flow fingerprint.
    current_node, _counters, fingerprint = store.get_flow_checkpoint("task-1")
    assert current_node == "publish"
    assert fingerprint == snapshot.flow_fingerprint
