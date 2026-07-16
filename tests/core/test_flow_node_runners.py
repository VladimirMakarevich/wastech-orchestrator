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
from wastech_orchestrator.checks.model import ResolvedCheck, ResolvedCheckSet
from wastech_orchestrator.config.schema import BranchMode, PublishScope
from wastech_orchestrator.core.flow.contracts import (
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
    SessionScope,
)
from wastech_orchestrator.core.flow.engine import NodeContext
from wastech_orchestrator.core.flow.nodes import (
    AgentNodeRunner,
    ChecksNodeRunner,
    EvaluatorNodeRunner,
    HitlNodeRunner,
    NodeInputs,
    NodeServices,
    PublishConfigError,
    PublishNodeRunner,
)
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import (
    AgentNode,
    ChecksNode,
    Edge,
    EvaluatorNode,
    FlowDoc,
    FlowNode,
    HitlNode,
    HitlSettings,
    PublishNode,
)
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.providers.artifacts import node_run_dir
from wastech_orchestrator.providers.base import (
    AgentRunResult,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.routing.router import ResolvedRoute, RouteSource, StageOutcome
from wastech_orchestrator.state_store import EditingLineageRow

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

    def resolve_route(self, node_id: str, override: Any = None) -> ResolvedRoute:
        return ResolvedRoute(
            node_id=node_id, primary=ProviderId.CODEX, fallback=None, source=RouteSource.CONFIG
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
        self.evaluations: list[Any] = []
        self.editing_lineage: dict[tuple[str, int | None, str], EditingLineageRow] = {}
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

    def get_editing_lineage(
        self, task_id: str, lineage_key: str, subtask_order: int | None = None
    ) -> EditingLineageRow | None:
        return self.editing_lineage.get((task_id, subtask_order, lineage_key))

    def upsert_editing_lineage(self, row: EditingLineageRow, conn: Any = None) -> None:
        self.editing_lineage[(row.task_id, row.subtask_order, row.lineage_key)] = row


def _result(structured: dict[str, Any] | None = None) -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="codex",
        node_id="implementation",
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
    check_runner: Any,
    git: Any = None,
    artifacts_root: str = "/art",
    snapshot: Any = None,
    packet_builder: Any = None,
) -> Any:
    return NodeServices(
        router=router,
        check_runner=check_runner,
        store=store,
        repo_dir="/repo",
        artifacts_root=artifacts_root,
        clock=lambda: "ts",
        default_timeout_seconds=100,
        git=git,
        snapshot=snapshot,
        packet_builder=packet_builder,
    )


class FakePacketBuilder:
    """Records the packet-build call and writes a stub packet to ``dest`` (the read-path seam)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def write_packet(
        self, *, node_id: str, task_type: str | None, touched_paths: Any, dest: Path
    ) -> Path | None:
        self.calls.append(
            {"node_id": node_id, "task_type": task_type, "touched_paths": list(touched_paths)}
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("# Repository memory\n", encoding="utf-8")
        return dest


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
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
    )
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


def test_node_output_path_variable_resolves_downstream(tmp_path: Path) -> None:
    # The generic {<id>_path} channel: a node references an upstream agent node's persisted output
    # by id, and it resolves to that node's <id>.out.md once the file exists (node-output ADR).
    (tmp_path / "impl.md").write_text(
        "Build.{?scan_path} Read the scan at {scan_path}.{/scan_path}", "utf-8"
    )
    scan = AgentNode(id="scan", kind="agent", role_file="scan.md")
    impl = AgentNode(
        id="implement",
        kind="agent",
        role_file="impl.md",
        permission_profile=PermissionProfile.READ_ONLY,
    )
    doc = FlowDoc(
        name="t",
        task_type="t",
        permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
        output_policy=OutputPolicy.CODE_CHANGE,
        publishing=PublishingPolicy.PULL_REQUEST,
        nodes=(scan, impl),
        edges=(),
        budgets=MappingProxyType({}),
    )
    snap = FlowSnapshot(
        doc=doc,
        nodes_by_id=MappingProxyType({"scan": scan, "implement": impl}),
        adjacency=MappingProxyType({}),
        flow_fingerprint="fp",
    )
    # The generic channel is now per-run: place scan's output under stages/scan/run-<id>/.
    scan_out = tmp_path / "logs" / "task-1" / "stages" / "scan" / "run-000001" / "scan.out.md"
    scan_out.parent.mkdir(parents=True, exist_ok=True)
    scan_out.write_text("SCAN RESULT", "utf-8")

    router, store = FakeRouter(_result()), FakeStore()
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    ctx = NodeContext(
        snapshot=snap, run_state=FlowRunState(flow_fingerprint="fp"), node=impl, task_id="task-1"
    )
    AgentNodeRunner(services, _inputs(tmp_path)).run(impl, ctx)

    prompt = router.requests[0].prompt
    assert f"Read the scan at {scan_out.as_posix()}." in prompt


def test_node_output_var_empty_before_upstream_runs(tmp_path: Path) -> None:
    # Before the upstream node has produced output, {?scan_path} drops cleanly (no dangling text).
    (tmp_path / "impl.md").write_text(
        "Build.{?scan_path} Read the scan at {scan_path}.{/scan_path}", "utf-8"
    )
    scan = AgentNode(id="scan", kind="agent", role_file="scan.md")
    impl = AgentNode(id="implement", kind="agent", role_file="impl.md")
    doc = FlowDoc(
        name="t",
        task_type="t",
        permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
        output_policy=OutputPolicy.CODE_CHANGE,
        publishing=PublishingPolicy.PULL_REQUEST,
        nodes=(scan, impl),
        edges=(),
        budgets=MappingProxyType({}),
    )
    snap = FlowSnapshot(
        doc=doc,
        nodes_by_id=MappingProxyType({"scan": scan, "implement": impl}),
        adjacency=MappingProxyType({}),
        flow_fingerprint="fp",
    )
    router, store = FakeRouter(_result()), FakeStore()
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    ctx = NodeContext(
        snapshot=snap, run_state=FlowRunState(flow_fingerprint="fp"), node=impl, task_id="task-1"
    )
    AgentNodeRunner(services, _inputs(tmp_path)).run(impl, ctx)
    assert router.requests[0].prompt == "Build."


def test_predecessor_context_rendered_when_set_and_referenced(tmp_path: Path) -> None:
    # The subtask handoff brief is injected as {predecessor_context} into the region's edit node
    # when a decompose region is active (subtask_order set), the orchestrator assembled a path, AND
    # the template references it (node-driven, mirrors {memory_path}).
    (tmp_path / "impl.md").write_text(
        "Build.{?predecessor_context} Handoff: {predecessor_context}.{/predecessor_context}",
        "utf-8",
    )
    node = AgentNode(id="implementation", kind="agent", role_file="impl.md")
    router, store = FakeRouter(_result()), FakeStore()
    services = _services(router, store, FakeCheckRunner(CheckOutcome(passed=True, runs=())))
    inputs = _inputs(tmp_path, predecessor_context_path="logs/task-1/subtasks/02-x.handoff.md")
    ctx = NodeContext(
        snapshot=_snapshot(node),
        run_state=FlowRunState(flow_fingerprint="fp"),
        node=node,
        task_id="task-1",
        subtask_order=2,
    )
    AgentNodeRunner(services, inputs).run(node, ctx)
    assert "Handoff: logs/task-1/subtasks/02-x.handoff.md." in router.requests[0].prompt


def test_predecessor_context_dropped_when_no_path(tmp_path: Path) -> None:
    # A subtask with no assembled handoff (no depends_on) leaves {?predecessor_context} empty.
    (tmp_path / "impl.md").write_text(
        "Build.{?predecessor_context} X {predecessor_context}{/predecessor_context}", "utf-8"
    )
    node = AgentNode(id="implementation", kind="agent", role_file="impl.md")
    router, store = FakeRouter(_result()), FakeStore()
    services = _services(router, store, FakeCheckRunner(CheckOutcome(passed=True, runs=())))
    ctx = NodeContext(
        snapshot=_snapshot(node),
        run_state=FlowRunState(flow_fingerprint="fp"),
        node=node,
        task_id="task-1",
        subtask_order=2,
    )
    AgentNodeRunner(services, _inputs(tmp_path)).run(node, ctx)  # predecessor_context_path=None
    assert router.requests[0].prompt == "Build."


def test_predecessor_context_dropped_outside_decompose_region(tmp_path: Path) -> None:
    # Not a subtask (subtask_order is None) → the variable is never assembled, so the block drops.
    (tmp_path / "impl.md").write_text(
        "Build.{?predecessor_context} X {predecessor_context}{/predecessor_context}", "utf-8"
    )
    node = AgentNode(id="implementation", kind="agent", role_file="impl.md")
    router, store = FakeRouter(_result()), FakeStore()
    services = _services(router, store, FakeCheckRunner(CheckOutcome(passed=True, runs=())))
    inputs = _inputs(tmp_path, predecessor_context_path="logs/task-1/subtasks/02-x.handoff.md")
    AgentNodeRunner(services, inputs).run(node, _ctx(node))  # _ctx has subtask_order=None
    assert router.requests[0].prompt == "Build."


def test_agent_node_builds_memory_packet_when_role_references_it(tmp_path: Path) -> None:
    # Node-driven (FR4): a node whose role references {memory_path} gets a per-node packet built
    # and its PATH injected — never the memory root (AC-R1). A custom node opts in, no Core change.
    role = "Work. {?memory_path}brief: {memory_path}{/memory_path}"
    (tmp_path / "r.md").write_text(role, "utf-8")
    node = AgentNode(id="impl", kind="agent", role_file="r.md")
    router, store, builder = FakeRouter(_result()), FakeStore(), FakePacketBuilder()
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
        packet_builder=builder,
    )
    inputs = _inputs(tmp_path, task_type="implementation")
    AgentNodeRunner(services, inputs).run(node, _ctx(node))

    assert len(builder.calls) == 1  # built exactly once, for this node
    assert builder.calls[0] == {
        "node_id": "impl",
        "task_type": "implementation",
        "touched_paths": [],
    }
    dest = tmp_path / "logs" / "task-1" / "memory" / "impl.md"
    assert router.requests[0].prompt == f"Work. brief: {dest}"


def test_agent_node_skips_memory_when_role_does_not_reference_it(tmp_path: Path) -> None:
    # A node not referencing {memory_path} triggers no build and the prompt is unaffected.
    (tmp_path / "r.md").write_text("Just work.", "utf-8")
    node = AgentNode(id="impl", kind="agent", role_file="r.md")
    router, store, builder = FakeRouter(_result()), FakeStore(), FakePacketBuilder()
    services = _services(
        router, store, FakeCheckRunner(CheckOutcome(passed=True, runs=())), packet_builder=builder
    )
    AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert builder.calls == []
    assert router.requests[0].prompt == "Just work."


def test_agent_node_memory_disabled_renders_empty(tmp_path: Path) -> None:
    # Memory disabled (no packet_builder, Q10): {memory_path} renders empty, block drops cleanly.
    role = "Work.{?memory_path} brief: {memory_path}{/memory_path}"
    (tmp_path / "r.md").write_text(role, "utf-8")
    node = AgentNode(id="impl", kind="agent", role_file="r.md")
    router, store = FakeRouter(_result()), FakeStore()
    services = _services(router, store, FakeCheckRunner(CheckOutcome(passed=True, runs=())))
    AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert router.requests[0].prompt == "Work."


def test_agent_node_network_access_grant_in_policyless_flow(tmp_path: Path) -> None:
    # Per-node override: a node-level grant reaches the request even in a flow with no
    # network_policy; a sibling without the field inherits the (policy-less) default and stays off.
    (tmp_path / "r.md").write_text("go", "utf-8")
    granted = AgentNode(id="impl", kind="agent", role_file="r.md", network_access=True)
    sibling = AgentNode(id="impl", kind="agent", role_file="r.md")
    for node, expected in ((granted, True), (sibling, False)):
        router, store = FakeRouter(_result()), FakeStore()
        services = _services(router, store, FakeCheckRunner(CheckOutcome(passed=True, runs=())))
        AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
        assert router.requests[0].network_access is expected


def test_agent_node_network_access_optout_in_granting_flow(tmp_path: Path) -> None:
    # A node-level False is an opt-out: offline even when the flow's network_policy would grant it.
    from dataclasses import replace

    from wastech_orchestrator.core.flow.contracts import NetworkPolicy

    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(id="impl", kind="agent", role_file="r.md", network_access=False)
    ctx = _ctx(node)
    ctx = replace(
        ctx,
        snapshot=replace(
            ctx.snapshot, doc=replace(ctx.snapshot.doc, network_policy=NetworkPolicy.RESEARCH)
        ),
    )
    router, store = FakeRouter(_result()), FakeStore()
    services = _services(router, store, FakeCheckRunner(CheckOutcome(passed=True, runs=())))
    AgentNodeRunner(services, _inputs(tmp_path)).run(node, ctx)
    assert router.requests[0].network_access is False


def test_fresh_disposable_node_does_not_inherit_or_leak_session(tmp_path: Path) -> None:
    # F3 / MC6 (durable, P2.2): a fresh_disposable node must NOT resume the unit's editing lineage
    # and must NOT write a lineage back — otherwise it would leak into a later editing_lineage node.
    from dataclasses import replace

    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(
        id="reviewish",
        kind="agent",
        role_file="r.md",
        session_scope=SessionScope.FRESH_DISPOSABLE,
        permission_profile=PermissionProfile.READ_ONLY,
    )
    router, store = FakeRouter(replace(_result(), session_id="fresh-sess")), FakeStore()
    store.upsert_editing_lineage(  # left by a prior editing node on the same provider
        EditingLineageRow(
            task_id="task-1",
            lineage_key="implementation",
            provider="codex",
            raw_session_id="editing-sess",
        )
    )
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
    )
    AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert router.requests[0].session_id is None  # did not resume the editing lineage
    # did not overwrite the editing lineage with its own session
    assert store.get_editing_lineage("task-1", "implementation").raw_session_id == "editing-sess"  # type: ignore[union-attr]


def test_editing_lineage_node_continues_and_persists_session(tmp_path: Path) -> None:
    # F3 / MC6 (durable, P2.2): an editing_lineage node resumes the unit's durable editing session
    # (when the provider matches) and writes the new session back, so a later editing node (fixing
    # after implementation) keeps the lineage.
    from dataclasses import replace

    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(
        id="impl",
        kind="agent",
        role_file="r.md",
        session_scope=SessionScope.EDITING_LINEAGE,
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )
    router, store = FakeRouter(replace(_result(), session_id="impl-sess-2")), FakeStore()
    store.upsert_editing_lineage(  # keyed by the node's own id (affinity-less owner)
        EditingLineageRow(
            task_id="task-1", lineage_key="impl", provider="codex", raw_session_id="impl-sess-1"
        )
    )
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
    )
    AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert router.requests[0].session_id == "impl-sess-1"  # resumed the editing session
    row = store.get_editing_lineage("task-1", "impl")
    assert row is not None and row.raw_session_id == "impl-sess-2"  # persisted the new session


def test_affinity_resumes_declared_node_session(tmp_path: Path) -> None:
    # P2.2: lineage_affinity (fixing → implementation) is realized by the one editing session per
    # execution unit — implementation establishes it, fixing resumes that same session.
    from dataclasses import replace

    (tmp_path / "impl.md").write_text("impl", "utf-8")
    (tmp_path / "fix.md").write_text("fix", "utf-8")
    store = FakeStore()
    check = FakeCheckRunner(CheckOutcome(passed=True, runs=()))

    impl = AgentNode(
        id="implementation",
        kind="agent",
        role_file="impl.md",
        session_scope=SessionScope.EDITING_LINEAGE,
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )
    router_impl = FakeRouter(replace(_result(), session_id="impl-session"))
    AgentNodeRunner(
        _services(router_impl, store, check),
        _inputs(tmp_path),
    ).run(impl, _ctx(impl))
    assert router_impl.requests[0].session_id is None  # no lineage yet → fresh

    fixing = AgentNode(
        id="fixing",
        kind="agent",
        role_file="fix.md",
        session_scope=SessionScope.EDITING_LINEAGE,
        lineage_affinity="implementation",
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )
    router_fix = FakeRouter(replace(_result(), session_id="fix-session"))
    AgentNodeRunner(_services(router_fix, store, check), _inputs(tmp_path)).run(
        fixing, _ctx(fixing)
    )
    assert router_fix.requests[0].session_id == "impl-session"  # resumed implementation's session
    row = store.get_editing_lineage("task-1", "implementation")
    assert row is not None and row.raw_session_id == "fix-session"  # fixing updated the lineage


def test_two_affinity_less_editing_nodes_keep_isolated_lineages(tmp_path: Path) -> None:
    # multiple-editing-lineages: two editing_lineage nodes without lineage_affinity own distinct
    # lineages (each keyed by its own id), so the second track does NOT inherit the first's session
    # and each persists into its own lineage.
    from dataclasses import replace

    (tmp_path / "code.md").write_text("code", "utf-8")
    (tmp_path / "spec.md").write_text("spec", "utf-8")
    store = FakeStore()
    check = FakeCheckRunner(CheckOutcome(passed=True, runs=()))

    code = AgentNode(
        id="code_edit",
        kind="agent",
        role_file="code.md",
        session_scope=SessionScope.EDITING_LINEAGE,
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )
    router_code = FakeRouter(replace(_result(), session_id="code-session"))
    AgentNodeRunner(_services(router_code, store, check), _inputs(tmp_path)).run(code, _ctx(code))
    assert router_code.requests[0].session_id is None  # its own lineage is empty → fresh

    spec = AgentNode(
        id="spec_edit",
        kind="agent",
        role_file="spec.md",
        session_scope=SessionScope.EDITING_LINEAGE,
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )
    router_spec = FakeRouter(replace(_result(), session_id="spec-session"))
    AgentNodeRunner(_services(router_spec, store, check), _inputs(tmp_path)).run(spec, _ctx(spec))
    # spec_edit does NOT inherit code_edit's session — its own lineage is still empty → fresh.
    assert router_spec.requests[0].session_id is None
    # Two isolated lineages persisted, one per node id.
    assert store.get_editing_lineage("task-1", "code_edit").raw_session_id == "code-session"  # type: ignore[union-attr]
    assert store.get_editing_lineage("task-1", "spec_edit").raw_session_id == "spec-session"  # type: ignore[union-attr]


def test_evaluator_fresh_disposable_does_not_touch_lineage(tmp_path: Path) -> None:
    # P2.2: an in-flow evaluator (fresh_disposable) never resumes nor writes the author's editing
    # lineage — it gets a fresh session and the unit's editing session is left untouched.
    (tmp_path / "r.md").write_text("review", "utf-8")
    store = FakeStore()
    store.upsert_editing_lineage(
        EditingLineageRow(
            task_id="task-1",
            lineage_key="implementation",
            provider="codex",
            raw_session_id="author-session",
        )
    )
    router = FakeRouter(_result({"findings": []}))
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    node = _evaluator("review")
    EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert router.requests[0].session_id is None  # evaluator never resumes the author lineage
    row = store.get_editing_lineage("task-1", "implementation")
    assert row is not None and row.raw_session_id == "author-session"  # left untouched


def test_evaluator_node_network_access_grant_in_policyless_flow(tmp_path: Path) -> None:
    # Evaluator parity: a node-level grant reaches the request even in a policy-less flow.
    (tmp_path / "r.md").write_text("review", "utf-8")
    from dataclasses import replace

    node = replace(_evaluator("review"), network_access=True)
    router = FakeRouter(_result({"findings": []}))
    services = _services(
        router,
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert router.requests[0].network_access is True


def test_agent_node_infra_exhaustion_raises(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(
        id="impl",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )
    router, store = FakeRouter(None), FakeStore()  # result None => infra-exhausted
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
    )
    from wastech_orchestrator.core.flow.nodes.base import NodeInfraError

    with pytest.raises(NodeInfraError):
        AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


def test_evaluator_node_infra_exhaustion_raises_evaluator_infra_error(tmp_path: Path) -> None:
    # An evaluator that could not RUN (no provider) raises the dedicated EvaluatorInfraError — a
    # NodeInfraError subclass — so the orchestrator can degrade to manual (preserve the green diff)
    # instead of discarding it as failed, unlike an agent node whose infra exhaustion has no result.
    (tmp_path / "r.md").write_text("review", "utf-8")
    router, store = FakeRouter(None), FakeStore()  # result None => infra-exhausted
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    node = _evaluator("review")
    from wastech_orchestrator.core.flow.nodes.base import EvaluatorInfraError, NodeInfraError

    assert issubclass(EvaluatorInfraError, NodeInfraError)
    with pytest.raises(EvaluatorInfraError):
        EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


def test_evaluator_no_work_surfaces_infra_class_not_schema(tmp_path: Path) -> None:
    # EXPERIMENTAL(no-work-infra) — remove with the feature.
    # A no-work evaluator run now arrives as an EXHAUSTED route (result None) carrying the boundary
    # AGENT_NO_PROGRESS class. The never-ran branch fires FIRST and re-surfaces it — a dead run is
    # never mislabeled "schema not honored" (F3). This confirms there is no double-handling:
    # the schema-not-honored path is reachable only when a run actually produced a result.
    from wastech_orchestrator.core.flow.nodes.base import EvaluatorInfraError
    from wastech_orchestrator.providers.base import ErrorClass, NormalizedError
    from wastech_orchestrator.providers.errors import message_for

    (tmp_path / "r.md").write_text("review", "utf-8")

    class _ExhaustedRouter(FakeRouter):
        def run_stage(self, request: Any, route: ResolvedRoute, *, snapshot: Any = None) -> Any:
            self.requests.append(request)
            return StageOutcome(
                route=route,
                result=None,
                provider_used=None,
                stage_attempts=2,
                terminal_error=NormalizedError(
                    ErrorClass.AGENT_NO_PROGRESS, message_for(ErrorClass.AGENT_NO_PROGRESS)
                ),
                attempts=(),
            )

    services = _services(
        _ExhaustedRouter(None),
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    node = _evaluator("review")
    with pytest.raises(EvaluatorInfraError) as exc:
        EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert exc.value.error_class is ErrorClass.AGENT_NO_PROGRESS
    assert "schema not honored" not in str(exc.value)


def test_agent_workspace_write_writes_diff(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(
        id="impl",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )
    router, store, git = FakeRouter(_result()), FakeStore(), FakeGit()
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        git=git,
    )
    inputs = _inputs(tmp_path)
    AgentNodeRunner(services, inputs).run(node, _ctx(node))
    assert inputs.diff_path == "/art/current.diff"
    assert ("write_current_diff", "task-1") in git.calls


def test_agent_dangerous_diff_goes_manual(tmp_path: Path) -> None:
    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
    from wastech_orchestrator.git_manager import ChangedPath

    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(
        id="impl",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )
    git = FakeGit(changed=(ChangedPath(status="D", path="src/core.py"),))
    services = _services(
        FakeRouter(_result()),
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        git=git,
    )
    with pytest.raises(NodeManualRequired):
        AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


def _ws_node() -> AgentNode:
    return AgentNode(
        id="impl",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )


def _guard_services(
    tmp_path: Path,
    git: Any,
    notifier: Any,
    *,
    trust_level: str = "strict",
    protected_paths: tuple[str, ...] = (),
) -> Any:
    return NodeServices(
        router=FakeRouter(_result()),
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        git=git,
        notifier=notifier,
        ask_timeout_s=60,
        trust_level=trust_level,
        protected_paths=protected_paths,
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


def test_agent_auto_deletion_skips_approval(tmp_path: Path) -> None:
    # Under `trust_level: auto` the diff-shape gate is off: a routine deletion proceeds with no ask.
    from wastech_orchestrator.git_manager import ChangedPath
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    git = FakeGit(changed=(ChangedPath(status="D", path="src/x.py"),))
    notifier = FakeNotifier(AskResult(answered=True, approved=True))
    services = _guard_services(tmp_path, git, notifier, trust_level="auto")
    result = AgentNodeRunner(services, _inputs(tmp_path)).run(_ws_node(), _ctx(_ws_node()))
    assert result.outcome.kind == "done"
    assert not notifier.asks  # auto → no approval requested for a routine deletion


def test_agent_protected_path_still_asks_under_auto(tmp_path: Path) -> None:
    # `protected_paths` is the always-ask floor: a change matching it gates even under `auto`.
    from wastech_orchestrator.git_manager import ChangedPath
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    git = FakeGit(changed=(ChangedPath(status="M", path="src/security/keys.py"),))
    notifier = FakeNotifier(AskResult(answered=True, approved=True))
    services = _guard_services(
        tmp_path, git, notifier, trust_level="auto", protected_paths=("src/security/**",)
    )
    result = AgentNodeRunner(services, _inputs(tmp_path)).run(_ws_node(), _ctx(_ws_node()))
    assert result.outcome.kind == "done"
    assert notifier.asks  # protected path → approval requested even under auto


def test_agent_read_only_node_skips_diff_guard(tmp_path: Path) -> None:
    # summary is a read-only agent node (non-HITL stage) -> simple path, no diff guard.
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(
        id="summary", kind="agent", role_file="r.md", permission_profile=PermissionProfile.READ_ONLY
    )
    git = FakeGit()
    services = _services(
        FakeRouter(_result()),
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        git=git,
    )
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
        clock=lambda: "ts",
        prompt_audit=prompt_audit,
        register_artifact=lambda t, k, p: registered.append((t, k, p)),
    )


def test_agent_node_writes_prompt_audit_when_enabled(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(
        id="implementation",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.READ_ONLY,
    )
    registered: list[Any] = []
    AgentNodeRunner(
        _audit_services(tmp_path, prompt_audit=True, registered=registered), _inputs(tmp_path)
    ).run(node, _ctx(node))
    kinds = {k for _, k, _ in registered}
    assert {"rendered_prompt", "prompt_audit", "prompt_audit_timeline"} <= kinds


def test_agent_node_no_prompt_audit_when_disabled(tmp_path: Path) -> None:
    # prompt_audit off: rendered-prompt still written (audit-independent), but no prompt-audit JSON.
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(
        id="implementation",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.READ_ONLY,
    )
    registered: list[Any] = []
    AgentNodeRunner(
        _audit_services(tmp_path, prompt_audit=False, registered=registered), _inputs(tmp_path)
    ).run(node, _ctx(node))
    kinds = {k for _, k, _ in registered}
    assert "rendered_prompt" in kinds
    assert "prompt_audit" not in kinds


def test_agent_node_rendered_prompt_includes_context_footer(tmp_path: Path) -> None:
    """rendered-prompt.md must carry the context-files footer, not just the bare template — it
    is written from the same effective prompt the provider actually receives on stdin."""
    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(
        id="implementation",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.READ_ONLY,
    )
    registered: list[Any] = []
    inputs = _inputs(tmp_path, task_path="/t/task.md", plan_path="/t/plan.md")
    AgentNodeRunner(
        _audit_services(tmp_path, prompt_audit=True, registered=registered), inputs
    ).run(node, _ctx(node))
    rendered = (
        node_run_dir(str(tmp_path), "task-1", "implementation", 1) / "rendered-prompt.md"
    ).read_text()
    assert "Context files (read them as needed" in rendered
    assert "/t/task.md" in rendered
    assert "/t/plan.md" in rendered


def test_evaluator_rendered_prompt_includes_context_footer(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _evaluator("review")
    registered: list[Any] = []
    services = NodeServices(
        router=FakeRouter(_result({"findings": []})),
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        prompt_audit=True,
        register_artifact=lambda t, k, p: registered.append((t, k, p)),
    )
    inputs = _inputs(tmp_path, task_path="/t/task.md", plan_path="/t/plan.md")
    EvaluatorNodeRunner(services, inputs).run(node, _ctx(node))
    rendered = (
        node_run_dir(str(tmp_path), "task-1", "review", 1) / "rendered-prompt.md"
    ).read_text()
    assert "Context files (read them as needed" in rendered
    assert "/t/task.md" in rendered
    assert "/t/plan.md" in rendered


# -- embedded HITL (refinement / planning) ------------------------------------


class FakeNotifier:
    """Records the prompt and returns a programmed answer; satisfies NotifierPort."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.asks: list[str] = []

    def start_ask(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: str,
        timeout_s: int,
        interaction_id: str,
        contacts: tuple[str, ...] = (),
    ) -> Any:
        from wastech_orchestrator.notify import AskHandle

        self.asks.append(question)
        return AskHandle(interaction_id=interaction_id, kind=kind, expires_at=1.0, message_id=1)

    def wait_for_answer(self, handle: Any) -> Any:
        return self._result


def _refinement_node() -> AgentNode:
    # Refinement opts into HITL by declaring `hitl` (data-driven dispatch, not the stage name).
    return AgentNode(
        id="refinement",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.READ_ONLY,
        hitl=HitlSettings(allow_question=True),
    )


def test_agent_hitl_no_signal_proceeds(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("refine", "utf-8")
    node = _refinement_node()
    router = FakeRouter(_result({"content": "done", "human_input": None}))
    services = _services(
        router,
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
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
                {
                    "kind": "question",
                    "question": "Which API?",
                    "context": "",
                    "risk": "clarification",
                    "paths": [],
                }
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
        clock=lambda: "ts",
        notifier=notifier,
        ask_timeout_s=60,
    )
    result = AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "done"
    assert notifier.asks == ["Which API?"]
    assert router.calls == 2  # initial run + re-run with the answer


def test_agent_hitl_round_trip_resumes_first_run_session(tmp_path: Path) -> None:
    # P0 (operator directive): after a HITL round-trip the node must RESUME the first run's session
    # so the agent continues the same conversation with the operator's answer — it does not start a
    # fresh session and re-derive from scratch. A fresh_disposable node has no editing lineage, so
    # the only reason the re-run carries a session id is the explicit HITL resume.
    from dataclasses import replace

    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("refine", "utf-8")
    node = _refinement_node()

    class ResumeRouter(FakeRouter):
        """Run 1 asks a question (session hitl-sess-1); the answered re-run resumes that session."""

        def __init__(self) -> None:
            super().__init__(None)
            self._n = 0

        def run_stage(self, request: Any, route: Any, *, snapshot: Any = None) -> Any:
            self.requests.append(request)
            self._n += 1
            if self._n == 1:
                signal = {
                    "kind": "question",
                    "question": "Which API?",
                    "context": "",
                    "risk": "clarification",
                    "paths": [],
                }
                return _stage_outcome(
                    route,
                    replace(
                        _result({"content": "ok", "human_input": signal}), session_id="hitl-sess-1"
                    ),
                )
            return _stage_outcome(
                route,
                replace(_result({"content": "ok", "human_input": None}), session_id="hitl-sess-2"),
            )

    router = ResumeRouter()
    notifier = FakeNotifier(AskResult(answered=True, text="use v2"))
    services = NodeServices(
        router=router,
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        notifier=notifier,
        ask_timeout_s=60,
    )
    result = AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))

    assert result.outcome.kind == "done"
    assert len(router.requests) == 2
    assert router.requests[0].session_id is None  # fresh first run (fresh_disposable, no lineage)
    assert router.requests[1].session_id == "hitl-sess-1"  # the re-run RESUMES the first session


def test_agent_hitl_round_trip_no_resume_when_first_run_used_fallback(tmp_path: Path) -> None:
    # Same-provider gate: if the first HITL run succeeded only on the FALLBACK provider, its session
    # belongs to that provider. The re-run resolves the same route and leads with the primary, so
    # resuming the fallback session there would be a cross-provider resume (which fails) — the gate
    # must refuse and start fresh.
    from dataclasses import replace

    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("refine", "utf-8")
    node = _refinement_node()

    class FallbackThenPrimaryRouter(FakeRouter):
        """Run 1 succeeds only on the fallback (claude); the primary re-run must not resume it."""

        def __init__(self) -> None:
            super().__init__(None)
            self._n = 0

        def resolve_route(self, node_id: str, override: Any = None) -> ResolvedRoute:
            return ResolvedRoute(
                node_id=node_id,
                primary=ProviderId.CODEX,
                fallback=ProviderId.CLAUDE,
                source=RouteSource.CONFIG,
            )

        def run_stage(self, request: Any, route: Any, *, snapshot: Any = None) -> Any:
            self.requests.append(request)
            self._n += 1
            if self._n == 1:
                signal = {
                    "kind": "question",
                    "question": "Which API?",
                    "context": "",
                    "risk": "clarification",
                    "paths": [],
                }
                outcome = _stage_outcome(
                    route,
                    replace(
                        _result({"content": "ok", "human_input": signal}), session_id="claude-sess"
                    ),
                )
                return replace(outcome, provider_used=ProviderId.CLAUDE)  # fell back to fallback
            return _stage_outcome(
                route,
                replace(_result({"content": "ok", "human_input": None}), session_id="codex-sess"),
            )

    router = FallbackThenPrimaryRouter()
    notifier = FakeNotifier(AskResult(answered=True, text="use v2"))
    services = NodeServices(
        router=router,
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        notifier=notifier,
        ask_timeout_s=60,
    )
    AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))

    assert router.requests[1].session_id is None  # fallback-provider session not resumed on primary


def test_dangerous_diff_reconsider_resumes_editing_lineage(tmp_path: Path) -> None:
    # The denial-driven reconsider re-run of an editing node resumes the node's persisted
    # editing-lineage session (it does not start fresh) — so the agent re-thinks with full context
    # plus the denial. This comes for free from the editing-lineage machinery; the HITL round-trip
    # resume above does not need to thread anything into _reconsider.
    from dataclasses import replace

    from wastech_orchestrator.git_manager import ChangedPath
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    node = AgentNode(
        id="implementation",
        kind="agent",
        role_file="r.md",
        session_scope=SessionScope.EDITING_LINEAGE,
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )
    # first classify is dangerous; after the denial-driven reconsider re-run, the diff is clean.
    git = FakeGit(changed_seq=[(ChangedPath(status="D", path="src/x.py"),), ()])
    notifier = FakeNotifier(AskResult(answered=True, approved=False))
    router = FakeRouter(replace(_result(), session_id="edit-sess-1"))
    services = NodeServices(
        router=router,
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        git=git,
        notifier=notifier,
        ask_timeout_s=60,
    )
    result = AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))

    assert result.outcome.kind == "done"
    assert len(router.requests) == 2  # initial edit + reconsider re-run
    assert router.requests[0].session_id is None  # no lineage yet → fresh first run
    assert router.requests[1].session_id == "edit-sess-1"  # reconsider resumed the editing session


def test_agent_hitl_dispatch_is_data_driven_not_stage(tmp_path: Path) -> None:
    # A node on the REFINEMENT stage but WITHOUT a declared `hitl` must NOT do a round-trip even if
    # the agent emits a signal — dispatch is by node.hitl, not the stage name (flow-contract).
    (tmp_path / "r.md").write_text("refine", "utf-8")
    node = AgentNode(
        id="refinement",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.READ_ONLY,
    )  # no hitl declared
    signal = {
        "kind": "question",
        "question": "ignored?",
        "context": "",
        "risk": "clarification",
        "paths": [],
    }
    router = FakeRouter(_result({"content": "ok", "human_input": signal}))
    notifier = FakeNotifier(None)
    services = NodeServices(
        router=router,
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
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
    signal = {
        "kind": "question",
        "question": "?",
        "context": "",
        "risk": "clarification",
        "paths": [],
    }
    router = FakeRouter(_result({"content": "ok", "human_input": signal}))
    notifier = FakeNotifier(AskResult(answered=False, timed_out=True, failure="timeout"))
    services = NodeServices(
        router=router,
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        notifier=notifier,
        ask_timeout_s=60,
    )
    with pytest.raises(NodeManualRequired):
        AgentNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


# -- standalone hitl gate node ------------------------------------------------


def _hitl_services(tmp_path: Path, notifier: Any, store: FakeStore | None = None) -> NodeServices:
    return NodeServices(
        router=FakeRouter(None),
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=store or FakeStore(),
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        notifier=notifier,
        ask_timeout_s=60,
    )


def test_hitl_node_registered_in_runner_registry(tmp_path: Path) -> None:
    from wastech_orchestrator.core.flow.engine_driver import build_node_runners

    runners = build_node_runners(_hitl_services(tmp_path, FakeNotifier(None)), _inputs(tmp_path))
    assert isinstance(runners["hitl"], HitlNodeRunner)


def test_hitl_approval_approved_routes_approve(tmp_path: Path) -> None:
    from wastech_orchestrator.notify import AskResult

    node = HitlNode(id="gate", kind="hitl", signal="approval")
    notifier = FakeNotifier(AskResult(answered=True, approved=True))
    store = FakeStore()
    result = HitlNodeRunner(_hitl_services(tmp_path, notifier, store), _inputs(tmp_path)).run(
        node, _ctx(node)
    )
    assert result.outcome.kind == "route:approve"
    assert notifier.asks == ["Approval required to continue the flow."]
    assert store.completed[-1]["outcome"] == "route:approve"


def test_hitl_approval_denied_routes_deny(tmp_path: Path) -> None:
    from wastech_orchestrator.notify import AskResult

    node = HitlNode(id="gate", kind="hitl", signal="approval")
    notifier = FakeNotifier(AskResult(answered=True, approved=False))
    result = HitlNodeRunner(_hitl_services(tmp_path, notifier), _inputs(tmp_path)).run(
        node, _ctx(node)
    )
    assert result.outcome.kind == "route:deny"


def test_hitl_question_proceeds_done(tmp_path: Path) -> None:
    from wastech_orchestrator.notify import AskResult

    node = HitlNode(id="gate", kind="hitl", signal="question")
    notifier = FakeNotifier(AskResult(answered=True, text="proceed"))
    result = HitlNodeRunner(_hitl_services(tmp_path, notifier), _inputs(tmp_path)).run(
        node, _ctx(node)
    )
    assert result.outcome.kind == "done"


def test_hitl_timeout_goes_manual(tmp_path: Path) -> None:
    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
    from wastech_orchestrator.notify import AskResult

    node = HitlNode(id="gate", kind="hitl", signal="approval")
    notifier = FakeNotifier(AskResult(answered=False, timed_out=True, failure="timeout"))
    store = FakeStore()
    with pytest.raises(NodeManualRequired):
        HitlNodeRunner(_hitl_services(tmp_path, notifier, store), _inputs(tmp_path)).run(
            node, _ctx(node)
        )
    assert store.completed[-1]["status"] == "failed"


def test_hitl_no_notifier_goes_manual(tmp_path: Path) -> None:
    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired

    node = HitlNode(id="gate", kind="hitl", signal="approval")
    with pytest.raises(NodeManualRequired):
        HitlNodeRunner(_hitl_services(tmp_path, notifier=None), _inputs(tmp_path)).run(
            node, _ctx(node)
        )


def test_hitl_resumes_persisted_waiting_interaction(tmp_path: Path) -> None:
    from wastech_orchestrator.core.hitl import (
        HumanInputSignal,
        node_interaction_path,
        write_waiting_interaction,
    )
    from wastech_orchestrator.notify import AskHandle, AskResult

    node = HitlNode(id="gate", kind="hitl", signal="approval")
    # A previous (interrupted) run left a durable `waiting` interaction; the resume must wait on the
    # persisted handle (not start a fresh prompt).
    path = node_interaction_path(str(tmp_path), "task-1", node.id)
    write_waiting_interaction(
        path,
        task_id="task-1",
        node_id=node.id,
        subtask=None,
        signal=HumanInputSignal(
            kind="approval",
            question="Approval required to continue the flow.",
            context="hitl gate 'gate'",
            risk="other",
            paths=(),
        ),
        handle=AskHandle(interaction_id="hxyz", kind="approval", expires_at=1.0, message_id=1),
    )
    notifier = FakeNotifier(AskResult(answered=True, approved=True))
    result = HitlNodeRunner(_hitl_services(tmp_path, notifier), _inputs(tmp_path)).run(
        node, _ctx(node)
    )
    assert result.outcome.kind == "route:approve"
    assert notifier.asks == []  # resumed the persisted prompt, did not start a fresh one


# -- evaluator ----------------------------------------------------------------


def _evaluator(
    node_id: str, *, blocking: bool = True, gate_severity: str = "high"
) -> EvaluatorNode:
    return EvaluatorNode(
        id=node_id,
        kind="evaluator",
        role="review",
        role_file="r.md",
        permission_profile=PermissionProfile.READ_ONLY,
        blocking=blocking,
        gate_severity=gate_severity,
    )


@pytest.mark.parametrize(
    ("structured", "expected"),
    [
        ({"findings": [{"title": "x", "severity": "high"}]}, "rework"),
        # #8: medium is advisory (non-blocking) — routing and the carried Finding agree on this now.
        ({"findings": [{"title": "x", "severity": "medium"}]}, "accept"),
        ({"findings": [{"title": "x", "severity": "low"}]}, "accept"),
        ({"findings": []}, "accept"),
    ],
)
def test_evaluator_maps_blocking_findings(
    tmp_path: Path, structured: dict[str, Any], expected: str
) -> None:
    (tmp_path / "r.md").write_text("review {diff_path}", "utf-8")
    node = _evaluator("review")
    router, store = FakeRouter(_result(structured)), FakeStore()
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == expected


@pytest.mark.parametrize(
    ("gate_severity", "severity", "expected"),
    [
        # Lowered gate: low blocks on ANY finding (the content-quality use-case).
        ("low", "low", "rework"),
        ("low", "medium", "rework"),
        # Medium gate: medium and above block, low is advisory.
        ("medium", "medium", "rework"),
        ("medium", "low", "accept"),
        # Default high gate: unchanged — medium/low advisory, high blocks.
        ("high", "high", "rework"),
        ("high", "medium", "accept"),
        ("high", "low", "accept"),
    ],
)
def test_evaluator_gate_severity_threshold(
    tmp_path: Path, gate_severity: str, severity: str, expected: str
) -> None:
    # gate_severity sets the minimum finding severity that gates: a finding at least as severe as
    # the gate drives rework; less-severe findings are advisory (accept). Default high reproduces
    # the historical behavior (blocks high/critical/blocking only).
    (tmp_path / "r.md").write_text("review {diff_path}", "utf-8")
    node = _evaluator("review", gate_severity=gate_severity)
    router, store = (
        FakeRouter(_result({"findings": [{"what": "x", "severity": severity}]})),
        FakeStore(),
    )
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == expected


def test_evaluator_non_blocking_gate_severity_self_caps(tmp_path: Path) -> None:
    # gate_severity composes with the non-blocking self-cap: a blocking:false evaluator with a
    # lowered gate reworks on a medium finding (which the default high gate would ignore) up to its
    # max_rework_per_stage, then accepts — never manual.
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = EvaluatorNode(
        id="testing_quality",
        kind="evaluator",
        role="test_quality",
        role_file="r.md",
        permission_profile=PermissionProfile.READ_ONLY,
        blocking=False,
        max_rework_per_stage=1,
        gate_severity="medium",
    )
    store = FakeStore()
    router = FakeRouter(_result({"findings": [{"what": "x", "severity": "medium"}]}))
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    inputs = _inputs(tmp_path)
    first = EvaluatorNodeRunner(services, inputs).run(node, _ctx(node))
    assert first.outcome.kind == "rework"  # medium gates under the lowered gate
    second = EvaluatorNodeRunner(services, inputs).run(node, _ctx(node))
    assert second.outcome.kind == "accept"  # budget spent → accept, not manual


def test_evaluator_builds_memory_packet_when_role_references_it(tmp_path: Path) -> None:
    # F31: an evaluator (review) whose role references {memory_path} now gets a per-node packet
    # built and its PATH injected — the block was dead because the evaluator runner never wired it.
    role = "Review. {?memory_path}mem: {memory_path}{/memory_path}"
    (tmp_path / "r.md").write_text(role, "utf-8")
    node = _evaluator("review")
    router = FakeRouter(_result({"findings": []}))
    store, builder = FakeStore(), FakePacketBuilder()
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
        packet_builder=builder,
    )
    inputs = _inputs(tmp_path, task_type="implementation")
    EvaluatorNodeRunner(services, inputs).run(node, _ctx(node))
    assert [c["node_id"] for c in builder.calls] == ["review"]
    dest = tmp_path / "logs" / "task-1" / "memory" / "review.md"
    assert router.requests[0].prompt == f"Review. mem: {dest}"


def test_evaluator_skips_memory_when_role_does_not_reference_it(tmp_path: Path) -> None:
    # An evaluator role not referencing {memory_path} triggers no build (mirrors the agent runner).
    (tmp_path / "r.md").write_text("review {diff_path}", "utf-8")
    node = _evaluator("review")
    router = FakeRouter(_result({"findings": []}))
    store, builder = FakeStore(), FakePacketBuilder()
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
        packet_builder=builder,
    )
    EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert builder.calls == []


def test_evaluator_medium_finding_is_non_blocking_and_carried(tmp_path: Path) -> None:
    # #8: a medium finding accepts (non-blocking routing) yet is still carried for the audit trail
    # with severity "medium" and Finding.blocking False — the carried flag and routing now agree.
    (tmp_path / "r.md").write_text("review {diff_path}", "utf-8")
    node = _evaluator("review")
    router, store = (
        FakeRouter(_result({"findings": [{"title": "x", "severity": "medium"}]})),
        (FakeStore()),
    )
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "accept"
    assert len(result.outcome.findings) == 1
    finding = result.outcome.findings[0]
    assert finding.severity == "medium"
    assert finding.blocking is False


def _test_quality(max_rework_per_stage: int = 1) -> EvaluatorNode:
    return EvaluatorNode(
        id="testing_quality",
        kind="evaluator",
        role="test_quality",
        role_file="r.md",
        permission_profile=PermissionProfile.READ_ONLY,
        blocking=False,
        max_rework_per_stage=max_rework_per_stage,
    )


def test_test_quality_rework_to_fixing(tmp_path: Path) -> None:
    # P2.4: a non-blocking test_quality evaluator with a blocking finding and budget remaining
    # routes to fixing (→ rework), exactly like a blocking evaluator — the non-blocking part only
    # governs what happens at exhaustion, not the first blocking finding.
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _test_quality()
    router, store = FakeRouter(_result({"findings": [{"severity": "high"}]})), FakeStore()
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "rework"


def test_test_quality_non_blocking_exhaustion_continues(tmp_path: Path) -> None:
    # P2.4: a non-blocking evaluator self-caps via the COUNT of its own in_flow_verdict rows. With
    # budget 1, the first blocking pass reworks; the second (budget spent) ACCEPTS — flow takes the
    # accept edge (→ checks), never manual. The core never learns the role: the cap is the node's
    # declared max_rework_per_stage.
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _test_quality(max_rework_per_stage=1)
    store = FakeStore()
    router = FakeRouter(_result({"findings": [{"severity": "critical"}]}))
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    first = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert first.outcome.kind == "rework"  # budget remaining → rework
    second = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert second.outcome.kind == "accept"  # budget spent → continue, not manual


def test_test_quality_does_not_write_tests(tmp_path: Path) -> None:
    # P2.4: the evaluator judges the tests the implementation agent wrote; it never writes tests
    # itself. Realized by the read-only request it issues (validator forbids any other profile).
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _test_quality()
    router, store = FakeRouter(_result({"findings": []})), FakeStore()
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert router.requests[0].permission_profile == "read-only"


def test_evaluator_review_writes_findings_artifact(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _evaluator("review")
    router = FakeRouter(_result({"findings": [{"title": "x", "severity": "low"}]}))
    store = FakeStore()
    inputs = _inputs(tmp_path)
    services = _services(
        router,
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    EvaluatorNodeRunner(services, inputs).run(node, _ctx(node))
    findings_file = Path(inputs.review_path)  # type: ignore[arg-type]
    assert findings_file.name == "findings.json"
    # Per-run: under stages/<node>/run-<id>/ (node ran first → node_run_id 1).
    assert findings_file == node_run_dir(str(tmp_path), "task-1", "review", 1) / "findings.json"
    assert json.loads(findings_file.read_text("utf-8")) == {
        "findings": [{"title": "x", "severity": "low"}]
    }


def test_evaluator_reruns_preserve_each_passs_findings(tmp_path: Path) -> None:
    # preserve-node-run-artifact-history: a re-running evaluator (fix→review loop) keeps every
    # pass's findings.json on disk instead of clobbering the last; review_path tracks the newest.
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _evaluator("review")
    store = FakeStore()  # shared across both passes → node_run_id 1 then 2
    inputs = _inputs(tmp_path)
    services = _services(
        FakeRouter(_result({"findings": [{"severity": "low"}]})),
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    EvaluatorNodeRunner(services, inputs).run(node, _ctx(node))
    first = Path(inputs.review_path)  # type: ignore[arg-type]
    EvaluatorNodeRunner(services, inputs).run(node, _ctx(node))
    second = Path(inputs.review_path)  # type: ignore[arg-type]

    assert first == node_run_dir(str(tmp_path), "task-1", "review", 1) / "findings.json"
    assert second == node_run_dir(str(tmp_path), "task-1", "review", 2) / "findings.json"
    assert first != second and first.is_file() and second.is_file()  # both passes preserved


def test_review_is_ordinary_evaluator(tmp_path: Path) -> None:
    # P2.3: review is just ``role=review`` on the shared evaluator runner — the same verdict path,
    # immutable ``in_flow_verdict``, and blocking→rework mechanics as any in-flow evaluator (not a
    # special stage). A blocking finding routes to fixing via the flow's ``review_fix`` edge.
    (tmp_path / "r.md").write_text("review {diff_path}", "utf-8")
    node = _evaluator("review")  # kind=evaluator, role=review
    store = FakeStore()
    services = _services(
        FakeRouter(_result({"findings": [{"title": "bug", "severity": "high"}]})),
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "rework"  # blocking review → rework (→ fixing)
    # recorded an immutable in_flow_verdict, exactly like every other in-flow evaluator role
    assert [e.kind for e in store.evaluations] == ["in_flow_verdict"]
    assert store.evaluations[0].verdict == "rework" and store.evaluations[0].node_id == "review"


def test_evaluator_requests_the_mandatory_findings_schema(tmp_path: Path) -> None:
    # F19: the shared runner requests the findings schema for every evaluator role (not just
    # review) — this is what makes ``structured_output`` reliably parseable on a real provider.
    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _evaluator("review")
    router = FakeRouter(_result({"findings": []}))
    services = _services(
        router,
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    schema = router.requests[0].output_schema
    assert schema is not None
    assert schema["required"] == ["findings"]


def test_evaluator_missing_structured_output_fails_closed(tmp_path: Path) -> None:
    # F19: a provider that ignored `output_schema` entirely (structured_output=None) must never be
    # read as "no findings, accept" — it fails closed exactly like an evaluator that could not run
    # at all (EvaluatorInfraError -> the orchestrator degrades to manual, preserving the diff).
    from wastech_orchestrator.core.flow.nodes.base import EvaluatorInfraError

    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _evaluator("review")
    services = _services(
        FakeRouter(_result()),  # structured_output=None
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    with pytest.raises(EvaluatorInfraError):
        EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


def test_evaluator_findings_key_missing_fails_closed(tmp_path: Path) -> None:
    # Same fail-closed contract violation when structured_output is present but malformed (no
    # parseable `findings` array) — a provider that answered the wrong shape, not "no issues".
    from wastech_orchestrator.core.flow.nodes.base import EvaluatorInfraError

    (tmp_path / "r.md").write_text("review", "utf-8")
    node = _evaluator("review")
    services = _services(
        FakeRouter(_result({"summary": "looks fine"})),  # no "findings" key
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    with pytest.raises(EvaluatorInfraError):
        EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


def test_evaluator_empty_findings_array_still_accepts(tmp_path: Path) -> None:
    # A well-formed, empty findings array is a genuinely clean verdict, not a fail-closed one.
    node = _evaluator("review")
    (tmp_path / "r.md").write_text("review", "utf-8")
    services = _services(
        FakeRouter(_result({"findings": []})),
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        artifacts_root=str(tmp_path),
    )
    result = EvaluatorNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "accept"


# -- checks -------------------------------------------------------------------


def _checks_node() -> ChecksNode:
    return ChecksNode(id="testing", kind="checks", checker="command_profile")


def _run(passed: bool) -> CheckRunResult:
    return CheckRunResult(
        command="pytest",
        exit_code=0 if passed else 1,
        timed_out=False,
        passed=passed,
        log_path="/l",
    )


def _skipped_run() -> CheckRunResult:
    return CheckRunResult(
        command="xcodebuild test",
        exit_code=None,
        timed_out=False,
        passed=False,
        log_path="/l",
        skipped=True,
    )


def _one_set() -> tuple[ResolvedCheckSet, ...]:
    """A single always-on command set, so selection is non-empty and the runner is invoked.

    With ``git=None`` (the default in ``_services``) the diff is indeterminate → all sets run.
    """
    return (ResolvedCheckSet(name="a", paths=(), checks=(ResolvedCheck("x", ("x",)),)),)


def _checks_inputs(flow_dir: Path) -> NodeInputs:
    return _inputs(flow_dir, check_sets=_one_set())


def test_checks_pass_outcome(tmp_path: Path) -> None:
    node = _checks_node()
    store = FakeStore()
    services = _services(
        FakeRouter(_result()),
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=(_run(True),))),
    )
    result = ChecksNodeRunner(services, _checks_inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "pass"
    assert len(store.check_runs) == 1


def test_checks_fail_outcome(tmp_path: Path) -> None:
    node = _checks_node()
    store = FakeStore()
    services = _services(
        FakeRouter(_result()),
        store,
        FakeCheckRunner(
            CheckOutcome(
                passed=False, runs=(_run(False),), any_quality_failed=True, first_failure_log="/log"
            )
        ),
    )
    result = ChecksNodeRunner(services, _checks_inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "fail"


def test_checks_launch_failure_is_manual(tmp_path: Path) -> None:
    # A required toolchain that could not launch → incomplete gate → manual (no more re-resolve).
    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired

    node = _checks_node()
    store = FakeStore()
    services = _services(
        FakeRouter(_result()),
        store,
        FakeCheckRunner(CheckOutcome(passed=False, runs=(), any_launch_failed=True)),
    )
    with pytest.raises(NodeManualRequired):
        ChecksNodeRunner(services, _checks_inputs(tmp_path)).run(node, _ctx(node))
    assert store.completed[-1]["status"] == "incomplete"


def test_checks_all_skipped_is_manual(tmp_path: Path) -> None:
    # Every selected check skipped (toolchain absent) → nothing ran → incomplete gate → manual.
    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired

    node = _checks_node()
    store = FakeStore()
    services = _services(
        FakeRouter(_result()),
        store,
        FakeCheckRunner(
            CheckOutcome(passed=False, runs=(_skipped_run(),), any_skipped=True, nothing_ran=True)
        ),
    )
    with pytest.raises(NodeManualRequired):
        ChecksNodeRunner(services, _checks_inputs(tmp_path)).run(node, _ctx(node))
    assert store.completed[-1]["status"] == "incomplete"


def test_checks_partial_skip_still_passes(tmp_path: Path) -> None:
    # One set ran+passed, another skipped: the node passes, and the skip is recorded.
    node = _checks_node()
    store = FakeStore()
    outcome = CheckOutcome(passed=True, runs=(_run(True), _skipped_run()), any_skipped=True)
    services = _services(FakeRouter(_result()), store, FakeCheckRunner(outcome))
    result = ChecksNodeRunner(services, _checks_inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "pass"
    assert len(store.check_runs) == 2  # both the run and the skip are recorded


def test_checks_empty_diff_passes_without_running(tmp_path: Path) -> None:
    # Correction: a task that changed no code selects no set → vacuous pass, runner never called.
    node = _checks_node()
    store = FakeStore()
    check_runner = FakeCheckRunner(CheckOutcome(passed=False, runs=(), any_quality_failed=True))
    # FakeGit.changed_code_paths_since_base() returns [] → empty diff → no set selected.
    services = _services(FakeRouter(_result()), store, check_runner, git=FakeGit())
    result = ChecksNodeRunner(services, _checks_inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "pass"
    assert store.check_runs == []  # nothing ran


def test_checks_selects_from_committed_change_when_tree_clean(tmp_path: Path) -> None:
    # Regression (decomposed subtask): the code is already committed, so the working tree is clean
    # (changed_code_paths() == []) — but the change is still present vs base, so the base-inclusive
    # changed_code_paths_since_base() is non-empty and the checks node must run its command set
    # rather than pass vacuously.
    class CleanTreeGit(FakeGit):
        def changed_code_paths(self) -> list[str]:
            return []  # nothing uncommitted

        def changed_code_paths_since_base(self) -> list[str]:
            return ["src/x.py"]  # committed since base → still selectable

    node = _checks_node()
    store = FakeStore()
    check_runner = FakeCheckRunner(CheckOutcome(passed=True, runs=(_run(True),)))
    services = _services(FakeRouter(_result()), store, check_runner, git=CleanTreeGit())
    result = ChecksNodeRunner(services, _checks_inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "pass"
    assert len(store.check_runs) == 1  # the set ran — not a vacuous pass


# -- checks mutation guard (P2.4) --------------------------------------------


class FakeSnapshot:
    """SnapshotHook stub: capture() returns the next programmed working-tree checksum."""

    def __init__(self, checksums: list[str]) -> None:
        from wastech_orchestrator.routing.snapshots import WorkingTreeSnapshot

        self._cls = WorkingTreeSnapshot
        self._checksums = checksums
        self.captures = 0

    def capture(self) -> Any:
        cs = self._checksums[min(self.captures, len(self._checksums) - 1)]
        self.captures += 1
        return self._cls(commit_sha="sha", porcelain_status="", diff_checksum=cs, artifacts=())

    def partial_change_since(self, before: Any) -> Any:
        return None


def test_mutation_guard_active_when_checks_present(tmp_path: Path) -> None:
    # P2.4: a passing check that mutated the working tree (commit-candidate files changed across
    # the run, e.g. an auto-formatter) fails closed to manual — a green-but-dirtying check must not
    # pass silently. The guard is a checks-node property, active regardless of the rest of the flow.
    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired

    node = _checks_node()
    store = FakeStore()
    services = _services(
        FakeRouter(_result()),
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=(_run(True),))),
        snapshot=FakeSnapshot(["before", "after"]),
    )  # checksum changed → mutated
    with pytest.raises(NodeManualRequired):
        ChecksNodeRunner(services, _checks_inputs(tmp_path)).run(node, _ctx(node))
    assert store.completed[-1]["status"] == "dirtied_working_tree"


def test_mutation_guard_clean_check_still_passes(tmp_path: Path) -> None:
    # The guard does not false-positive: a passing check that left the tree untouched (same
    # checksum before/after) yields the ordinary "pass" outcome even with a snapshot hook wired.
    node = _checks_node()
    services = _services(
        FakeRouter(_result()),
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=(_run(True),))),
        snapshot=FakeSnapshot(["same"]),
    )  # capture() returns "same" both times
    result = ChecksNodeRunner(services, _checks_inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "pass"


def test_flow_without_checks_has_no_mutation_guard(tmp_path: Path) -> None:
    # P2.4: the guard belongs to the checks node — a flow without one is a valid graph shape and
    # simply has no guard (optional via graph shape, not by disabling a gate). Such a flow validates
    # and contains no checks node for the guard to attach to.
    from wastech_orchestrator.core.flow.validator import validate_flow

    impl = AgentNode(
        id="implementation",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )
    publish = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    doc = FlowDoc(
        name="t",
        task_type="t",
        permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
        output_policy=OutputPolicy.CODE_CHANGE,
        publishing=PublishingPolicy.PULL_REQUEST,
        nodes=(impl, publish),
        edges=(Edge(from_node="implementation", to="publish"),),
        budgets=MappingProxyType({}),
    )
    snap = FlowSnapshot(
        doc=doc,
        nodes_by_id=MappingProxyType({"implementation": impl, "publish": publish}),
        adjacency=MappingProxyType(
            {"implementation": (Edge(from_node="implementation", to="publish"),)}
        ),
        flow_fingerprint="fp",
    )
    validate_flow(snap)  # a checks-less flow is valid …
    assert not any(n.kind == "checks" for n in snap.nodes_by_id.values())  # … with no guard node


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

    def push(self, task_id: str, branch: str, **kw: object) -> bool:
        self.calls.append(("push", task_id, branch, kw.get("mode")))
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

    def changed_code_paths(self) -> list[str]:
        return []  # the staging set (uncommitted only); unused by the checks node now

    def changed_code_paths_since_base(self) -> list[str]:
        return []  # base-inclusive selection set: empty → the checks node passes vacuously


def test_publish_pull_request_runs_git_sequence(tmp_path: Path) -> None:
    node = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    git, store = FakeGit(), FakeStore()
    services = _services(
        FakeRouter(_result()),
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        git=git,
    )
    inputs = _inputs(
        tmp_path,
        branch="worc/task-1-x",
        pull_request_title="My PR",
        summary_body_path="/s/summary.md",
    )
    result = PublishNodeRunner(services, inputs).run(node, _ctx(node))
    assert result.outcome.kind == "done"
    assert [c[0] for c in git.calls] == ["commit_code", "commit_audit", "push", "create_pr"]
    assert git.calls[-1] == ("create_pr", "task-1", "worc/task-1-x", "My PR", "/s/summary.md")
    # commit_sha_after is the node's result reference; for a publish node that is the PR URL (an
    # intentional, documented overload — see NodeRunRow / Secondary obs 2), not a commit SHA.
    assert store.completed[-1]["commit_sha_after"] == "https://example/pr/1"


def test_publish_pull_request_requires_branch(tmp_path: Path) -> None:
    node = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    services = _services(
        FakeRouter(_result()),
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        git=FakeGit(),
    )
    with pytest.raises(PublishConfigError):
        PublishNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))


def test_publish_pull_request_requires_body_path(tmp_path: Path) -> None:
    # branch present but no summary body path: refuse rather than open a PR with an empty body.
    node = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    git = FakeGit()
    services = _services(
        FakeRouter(_result()),
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        git=git,
    )
    inputs = _inputs(tmp_path, branch="worc/task-1-x")  # summary_body_path is None
    with pytest.raises(PublishConfigError):
        PublishNodeRunner(services, inputs).run(node, _ctx(node))
    assert git.calls == []  # nothing committed/pushed/PR'd


def _publish_git(tmp_path: Path, **input_kw: Any) -> FakeGit:
    """Run a full PR publish node with the given NodeInputs overrides; return the FakeGit."""
    node = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    git = FakeGit()
    services = _services(
        FakeRouter(_result()),
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        git=git,
    )
    inputs = _inputs(tmp_path, branch="feature/x", summary_body_path="/s/summary.md", **input_kw)
    PublishNodeRunner(services, inputs).run(node, _ctx(node))
    return git


def test_publish_cap_commit_stops_after_commits(tmp_path: Path) -> None:
    # A `commit` cap stops after the code/audit commits — no push, no PR (branch-mode ADR).
    git = _publish_git(tmp_path, publish_scope=PublishScope.COMMIT)
    assert [c[0] for c in git.calls] == ["commit_code", "commit_audit"]


def test_publish_cap_commit_needs_no_body(tmp_path: Path) -> None:
    # A `commit` cap opens no PR, so a missing PR body is not an error (unlike the full path).
    node = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    services = _services(
        FakeRouter(_result()),
        FakeStore(),
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        git=FakeGit(),
    )
    inputs = _inputs(tmp_path, branch="feature/x", publish_scope=PublishScope.COMMIT)  # no body
    result = PublishNodeRunner(services, inputs).run(node, _ctx(node))  # no PublishConfigError
    assert result.outcome.kind == "done"


def test_publish_cap_push_stops_before_pr(tmp_path: Path) -> None:
    # A `push` cap runs commits + push but skips the PR.
    git = _publish_git(tmp_path, publish_scope=PublishScope.PUSH)
    assert [c[0] for c in git.calls] == ["commit_code", "commit_audit", "push"]


def test_publish_forwards_branch_mode_to_push(tmp_path: Path) -> None:
    # The effective branch mode reaches push (so it may target the base branch in current/existing).
    git = _publish_git(tmp_path, branch_mode=BranchMode.CURRENT)
    push = next(c for c in git.calls if c[0] == "push")
    assert push[-1] is BranchMode.CURRENT


def test_publish_finalize_provides_pr_body(tmp_path: Path) -> None:
    # With a finalize hook, the committed summary it returns is the PR body — no summary_body_path
    # needed, and finalize runs before the audit commit.
    node = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    git, store = FakeGit(), FakeStore()
    services = NodeServices(
        router=FakeRouter(_result()),
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=store,
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        git=git,
        finalize=lambda: "/done/task-1.summary.md",
    )
    inputs = _inputs(
        tmp_path,
        branch="worc/task-1-x",
        pull_request_title="PR",
    )  # no summary_body_path
    result = PublishNodeRunner(services, inputs).run(node, _ctx(node))
    assert result.outcome.kind == "done"
    assert git.calls[-1] == (
        "create_pr",
        "task-1",
        "worc/task-1-x",
        "PR",
        "/done/task-1.summary.md",
    )


def test_publish_none_policy_writes_no_git(tmp_path: Path) -> None:
    node = PublishNode(id="store", kind="publish", policy=PublishingPolicy.NONE)
    git, store = FakeGit(), FakeStore()
    services = _services(
        FakeRouter(_result()),
        store,
        FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        git=git,
    )
    result = PublishNodeRunner(services, _inputs(tmp_path)).run(node, _ctx(node))
    assert result.outcome.kind == "done"
    assert git.calls == []


def test_publish_git_failure_after_finalize_raises_manual(tmp_path: Path) -> None:
    # A git failure during publishing (here: push) AFTER finalize moved the task file to done/ and
    # committed the audit trail surfaces a resumable manual stop, not a terminal failure — so a
    # done-committed task is never mislabeled and its file is never stranded in done/ while marked
    # failed. The node run is recorded as failed for the audit trail. (F1 / MC2.)
    #
    # F12: the git stderr must be surfaced — logged at ERROR (daemon log) and persisted as a
    # `publish_error` node artifact — not swallowed behind a bare `publish_failed`.
    import logging

    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
    from wastech_orchestrator.git_manager import GitCommandError

    # Capture on the publish logger directly (the app logger sets propagate=False once configured,
    # so caplog's root handler is unreliable across test orderings).
    log_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_records.append(record)

    publish_logger = logging.getLogger("wastech_orchestrator.core.flow.nodes.publish")
    capture = _Capture()
    publish_logger.addHandler(capture)

    class FailingPushGit(FakeGit):
        def push(self, task_id: str, branch: str, **_: object) -> bool:
            self.calls.append(("push", task_id, branch))
            raise GitCommandError("simulated push failure")

    node = PublishNode(id="publish", kind="publish", policy=PublishingPolicy.PULL_REQUEST)
    git, store = FailingPushGit(), FakeStore()
    registered: list[tuple[str, str, str]] = []
    services = NodeServices(
        router=FakeRouter(_result()),
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=store,
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        git=git,
        finalize=lambda: "/done/task-1.summary.md",  # finalize already ran (file moved + summary)
        register_artifact=lambda t, k, p: registered.append((t, k, p)),
    )
    inputs = _inputs(tmp_path, branch="worc/task-1-x", pull_request_title="PR")
    try:
        with pytest.raises(NodeManualRequired, match="simulated push failure"):
            PublishNodeRunner(services, inputs).run(node, _ctx(node))
    finally:
        publish_logger.removeHandler(capture)
    # commit_code + commit_audit committed before push failed; create_pr never reached.
    assert [c[0] for c in git.calls] == ["commit_code", "commit_audit", "push"]
    # The node run is closed as failed (not left dangling, not "published").
    assert store.completed[-1]["status"] == "failed"
    assert store.completed[-1]["error_class"] == "publish_failed"
    # F12: the cause (git stderr) is logged at ERROR (daemon log) and persisted as an artifact —
    # not swallowed behind a bare `publish_failed`.
    assert any(
        r.levelno == logging.ERROR and "publish git operation failed" in r.getMessage()
        for r in log_records
    )
    assert registered and registered[-1][1] == "publish_error"
    written = Path(registered[-1][2]).read_text(encoding="utf-8")
    assert "simulated push failure" in written


# -- max-turns gate (idea 29) -------------------------------------------------


def _claude_route(node_id: str = "implementation") -> ResolvedRoute:
    return ResolvedRoute(
        node_id=node_id, primary=ProviderId.CLAUDE, fallback=None, source=RouteSource.CONFIG
    )


def _max_turns_result() -> AgentRunResult:
    """A claude run that exhausted its turn cap — the structured ``error_max_turns`` subtype + the
    session id the gate resumes on continue."""
    from wastech_orchestrator.providers.base import (
        MAX_TURNS_SUBTYPE,
        ErrorClass,
        NormalizedError,
    )

    return AgentRunResult(
        status=RunStatus.FAILED,
        provider="claude",
        node_id="implementation",
        attempt=1,
        exit_code=1,
        started_at="t0",
        finished_at="t1",
        session_id="sess-1",
        error=NormalizedError(
            ErrorClass.TASK_FAILURE,
            "task failure (error_max_turns)",
            failure_subtype=MAX_TURNS_SUBTYPE,
        ),
    )


def _claude_outcome(route: ResolvedRoute, result: AgentRunResult) -> StageOutcome:
    return StageOutcome(
        route=route,
        result=result,
        provider_used=ProviderId.CLAUDE,
        stage_attempts=1,
        terminal_error=None,
        attempts=(),
    )


class _GateRouter:
    """Returns a programmed sequence of claude outcomes (one per run_stage), recording requests."""

    def __init__(self, results: list[AgentRunResult]) -> None:
        self._results = results
        self.requests: list[Any] = []
        self._n = 0

    def resolve_route(self, node_id: str, override: Any = None) -> ResolvedRoute:
        return _claude_route(node_id)

    def run_stage(
        self, request: Any, route: ResolvedRoute, *, snapshot: Any = None
    ) -> StageOutcome:
        self.requests.append(request)
        result = self._results[min(self._n, len(self._results) - 1)]
        self._n += 1
        return _claude_outcome(route, result)

    @property
    def calls(self) -> int:
        return self._n


def _gate_node() -> AgentNode:
    # read-only so the post-edit dangerous-diff guard (which needs git) never engages.
    return AgentNode(
        id="implementation",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.READ_ONLY,
    )


def _gate_services(
    tmp_path: Path, router: Any, store: FakeStore, notifier: Any, *, on: bool = True
) -> NodeServices:
    return NodeServices(
        router=router,
        check_runner=FakeCheckRunner(CheckOutcome(passed=True, runs=())),
        store=store,
        repo_dir="/repo",
        artifacts_root=str(tmp_path),
        clock=lambda: "ts",
        notifier=notifier,
        ask_timeout_s=60,
        max_turns_gate=on,
    )


def _seed_waiting_turn_gate(tmp_path: Path, node: AgentNode, task_id: str) -> Path:
    """Persist a ``waiting`` turn-gate artifact, as an interrupted run mid-prompt would leave."""
    from wastech_orchestrator.core.hitl import (
        HumanInputSignal,
        turn_gate_interaction_path,
        write_waiting_interaction,
    )
    from wastech_orchestrator.notify import AskHandle

    path = turn_gate_interaction_path(str(tmp_path), task_id, node.id)
    write_waiting_interaction(
        path,
        task_id=task_id,
        node_id=node.id,
        subtask=None,
        signal=HumanInputSignal(
            kind="approval", question="continue?", context="", risk="other", paths=()
        ),
        handle=AskHandle(interaction_id="hX", kind="approval", expires_at=1.0, message_id=1),
    )
    return path


def test_max_turns_gate_continue_resumes_session(tmp_path: Path) -> None:
    # error_max_turns → operator approves → the node resumes the SAME session with a fresh grant and
    # the resumed run succeeds. The gate adds one provider call carrying the first run's session id.
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    router = _GateRouter([_max_turns_result(), _result({"content": "done"})])
    notifier = FakeNotifier(AskResult(answered=True, approved=True))
    services = _gate_services(tmp_path, router, FakeStore(), notifier)
    runner = AgentNodeRunner(services, _inputs(tmp_path))
    result = runner.run(_gate_node(), _ctx(_gate_node()))

    assert result.outcome.kind == "done"
    assert router.calls == 2  # initial max-turns run + the approved resume
    assert router.requests[1].session_id == "sess-1"  # resumed the exhausted session
    assert len(notifier.asks) == 1  # exactly one continue/stop prompt


def test_max_turns_gate_deny_stops_unchanged(tmp_path: Path) -> None:
    # Deny → STOP: the original max-turns failure flows on unchanged (no resume), exactly as the run
    # would terminate with the gate off.
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    router = _GateRouter([_max_turns_result()])
    notifier = FakeNotifier(AskResult(answered=True, approved=False))
    services = _gate_services(tmp_path, router, FakeStore(), notifier)
    runner = AgentNodeRunner(services, _inputs(tmp_path))
    result = runner.run(_gate_node(), _ctx(_gate_node()))

    assert result.outcome.kind == "done"  # failed result flows on (terminal as today)
    assert router.calls == 1  # no resume
    assert len(notifier.asks) == 1


def test_max_turns_gate_timeout_stops(tmp_path: Path) -> None:
    # Silence (timeout) never advances the run — fail-closed STOP.
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    router = _GateRouter([_max_turns_result()])
    notifier = FakeNotifier(AskResult(answered=False, timed_out=True, failure="timeout"))
    services = _gate_services(tmp_path, router, FakeStore(), notifier)
    runner = AgentNodeRunner(services, _inputs(tmp_path))
    result = runner.run(_gate_node(), _ctx(_gate_node()))

    assert result.outcome.kind == "done"
    assert router.calls == 1


def test_max_turns_gate_off_does_not_prompt(tmp_path: Path) -> None:
    # Gate off: error_max_turns flows on untouched — no prompt, no resume (today's behavior).
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    router = _GateRouter([_max_turns_result()])
    notifier = FakeNotifier(AskResult(answered=True, approved=True))
    services = _gate_services(tmp_path, router, FakeStore(), notifier, on=False)
    runner = AgentNodeRunner(services, _inputs(tmp_path))
    result = runner.run(_gate_node(), _ctx(_gate_node()))

    assert result.outcome.kind == "done"
    assert router.calls == 1
    assert notifier.asks == []  # gate off → never asked


def test_max_turns_gate_resumes_pending_decision_after_restart(tmp_path: Path) -> None:
    # Fail-closed across restart: a turn-gate left ``waiting`` by an interrupted run is resolved at
    # ENTRY, before the provider is touched (it reuses the persisted prompt, never re-asks). Approve
    # → one provider run (the resume), success, artifact consumed.
    from wastech_orchestrator.core.hitl import load_interaction
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    node, ctx = _gate_node(), _ctx(_gate_node())
    gate_path = _seed_waiting_turn_gate(tmp_path, node, ctx.task_id)
    router = _GateRouter([_result({"content": "done"})])
    notifier = FakeNotifier(AskResult(answered=True, approved=True))
    services = _gate_services(tmp_path, router, FakeStore(), notifier)
    runner = AgentNodeRunner(services, _inputs(tmp_path))
    result = runner.run(node, ctx)

    assert result.outcome.kind == "done"
    assert router.calls == 1  # the resume run only — the pending decision was resolved first
    assert notifier.asks == []  # resumed the persisted prompt (wait), never started a fresh one
    persisted = load_interaction(gate_path)
    assert persisted is not None and persisted["status"] == "consumed"


def test_max_turns_gate_restart_deny_goes_manual(tmp_path: Path) -> None:
    # A pending decision denied after restart fails closed to manual review — the provider is never
    # touched (no silent auto-continue).
    from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
    from wastech_orchestrator.notify import AskResult

    (tmp_path / "r.md").write_text("go", "utf-8")
    node, ctx = _gate_node(), _ctx(_gate_node())
    _seed_waiting_turn_gate(tmp_path, node, ctx.task_id)
    router = _GateRouter([_result({"content": "done"})])
    notifier = FakeNotifier(AskResult(answered=True, approved=False))
    services = _gate_services(tmp_path, router, FakeStore(), notifier)
    runner = AgentNodeRunner(services, _inputs(tmp_path))

    with pytest.raises(NodeManualRequired):
        runner.run(node, ctx)
    assert router.calls == 0  # stopped at the gate, never ran the provider
