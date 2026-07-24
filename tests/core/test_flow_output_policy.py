"""P3.2 — output / network policies: write containment, private-report publish, network grant.

The after-stage write guard confines a flow's writing nodes to its resolved report directory, the
``private_control_workspace_report`` publish stores the report without touching git (fail-closed if
it would be git-trackable), and the flow's ``network_policy`` toggles the agent request's network
grant (absent = no network).
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from wastech_orchestrator.core.flow.contracts import (
    NetworkPolicy,
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
)
from wastech_orchestrator.core.flow.engine import NodeContext
from wastech_orchestrator.core.flow.nodes import NodeInputs, NodeServices
from wastech_orchestrator.core.flow.nodes.agent import AgentNodeRunner
from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
from wastech_orchestrator.core.flow.nodes.publish import PublishNodeRunner
from wastech_orchestrator.core.flow.output_policy import (
    is_within,
    resolve_output_policy,
    within_subdir,
)
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import AgentNode, FlowDoc, PublishNode
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.git_manager import ChangedPath
from wastech_orchestrator.providers.base import AgentRunResult, ProviderId, RunStatus
from wastech_orchestrator.routing.router import ResolvedRoute, RouteSource, StageOutcome
from wastech_orchestrator.runtime_layout import ProviderWriteGuardPolicy

# -- fakes / helpers ----------------------------------------------------------


def _result() -> AgentRunResult:
    return AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="codex",
        node_id="synthesis",
        attempt=1,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        structured_output={"summary": "done"},
    )


class _Router:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def resolve_route(self, node_id: str, override: Any = None) -> ResolvedRoute:
        return ResolvedRoute(
            node_id=node_id, primary=ProviderId.CODEX, fallback=None, source=RouteSource.CONFIG
        )

    def run_stage(
        self, request: Any, route: ResolvedRoute, *, snapshot: Any = None
    ) -> StageOutcome:
        self.requests.append(request)
        return StageOutcome(
            route=route,
            result=_result(),
            provider_used=ProviderId.CODEX,
            stage_attempts=1,
            terminal_error=None,
            attempts=(),
        )


class _Store:
    def __init__(self) -> None:
        self._next = 1

    def record_node_run(self, run: Any, conn: Any = None) -> int:
        rid = self._next
        self._next += 1
        return rid

    def complete_node_run(self, run_id: int, **kwargs: Any) -> None:
        pass

    def get_editing_lineage(
        self, task_id: str, lineage_key: str, subtask_order: int | None = None
    ) -> None:
        return None


class _Git:
    """A minimal GitPort: ``changed_code_entries`` is scripted; commit/push/PR record calls."""

    def __init__(self, changed: tuple[ChangedPath, ...] = ()) -> None:
        self.calls: list[str] = []
        self._changed = changed

    def write_current_diff(self, task_id: str) -> str:
        return "/art/current.diff"

    def changed_code_entries(self) -> tuple[ChangedPath, ...]:
        return self._changed

    def commit_code(self, task_id: str, message: str) -> str | None:
        self.calls.append("commit_code")
        return "sha"

    def commit_audit(self, task_id: str, *, task_packet_digest: str | None = None) -> str | None:
        self.calls.append("commit_audit")
        return "sha"

    def capture_git_control_state(self) -> object:
        return object()

    def compare_git_control_state(self, before: object) -> None:
        return None

    def list_tracked_files(self, *pathspecs: str) -> tuple[str, ...]:
        return ()

    def resolve_control_paths(
        self, exchange_root: str | None = None, *, instruction_files: tuple[Path, ...] = ()
    ) -> ProviderWriteGuardPolicy:
        return ProviderWriteGuardPolicy(
            exchange_root=None,
            git_dir=Path("/x/.git"),
            git_common_dir=Path("/x/.git"),
            hooks_dir=Path("/x/.git/hooks"),
            tasks_dir=Path("/x/tasks"),
        )

    def push(self, task_id: str, branch: str, **_: object) -> bool:
        self.calls.append("push")
        return True

    def create_pr(self, task_id: str, branch: str, *, title: str, body_path: str) -> str | None:
        self.calls.append("create_pr")
        return "url"


def _snapshot(
    node: AgentNode | PublishNode,
    *,
    output_policy: OutputPolicy,
    network_policy: NetworkPolicy | None = None,
) -> FlowSnapshot:
    doc = FlowDoc(
        name="t",
        task_type="t",
        permission_ceiling=PermissionProfile.WORKSPACE_WRITE,
        output_policy=output_policy,
        publishing=PublishingPolicy.NONE,
        nodes=(node,),
        edges=(),
        budgets=MappingProxyType({}),
        network_policy=network_policy,
    )
    return FlowSnapshot(
        doc=doc,
        nodes_by_id=MappingProxyType({node.id: node}),
        adjacency=MappingProxyType({}),
        flow_fingerprint="fp",
    )


def _services(tmp_path: Path, git: _Git, router: _Router | None = None) -> NodeServices:
    from wastech_orchestrator.check_runner import CheckOutcome

    class _Checks:
        def run(self, **kwargs: Any) -> CheckOutcome:
            return CheckOutcome(passed=True, runs=())

    return NodeServices(
        router=router or _Router(),  # type: ignore[arg-type]
        check_runner=_Checks(),  # type: ignore[arg-type]
        store=_Store(),  # type: ignore[arg-type]
        repo_dir=str(tmp_path),
        artifacts_root=str(tmp_path / "art"),
        clock=lambda: "ts",
        git=git,  # type: ignore[arg-type]
    )


def _ctx(snapshot: FlowSnapshot, node: Any) -> NodeContext:
    return NodeContext(
        snapshot=snapshot, run_state=FlowRunState(flow_fingerprint="fp"), node=node, task_id="t"
    )


def _ws_node() -> AgentNode:
    return AgentNode(
        id="synthesis",
        kind="agent",
        role_file="r.md",
        permission_profile=PermissionProfile.WORKSPACE_WRITE,
    )


def _run_agent(tmp_path: Path, snapshot: FlowSnapshot, router: _Router, git: _Git) -> Any:
    (tmp_path / "r.md").write_text("write the report", encoding="utf-8")
    node = snapshot.doc.nodes[0]
    return AgentNodeRunner(_services(tmp_path, git, router), NodeInputs(flow_dir=tmp_path)).run(
        node, _ctx(snapshot, node)
    )


# -- pure resolution ----------------------------------------------------------


def test_resolve_output_policy_shapes() -> None:
    research = resolve_output_policy(OutputPolicy.REPOSITORY_DOCUMENT, "t1")
    assert research.report_subdir == "docs/research/t1"
    assert research.required_files == ("report.md", "sources.json")
    assert research.private is False

    audit = resolve_output_policy(OutputPolicy.PRIVATE_CONTROL_WORKSPACE_REPORT, "t1")
    assert audit.report_subdir == ".worc/security-reports/t1"
    assert audit.private is True

    code = resolve_output_policy(OutputPolicy.CODE_CHANGE, "t1")
    assert code.report_subdir is None and code.required_files == ()


def test_within_subdir_and_is_within(tmp_path: Path) -> None:
    assert within_subdir("docs/research/t1/report.md", "docs/research/t1")
    assert not within_subdir("src/app.py", "docs/research/t1")
    assert not within_subdir("docs/research/t1evil/x", "docs/research/t1")
    assert is_within(tmp_path, tmp_path / "a" / "b")
    assert not is_within(tmp_path / "repo", tmp_path / "outside")


# -- after-stage write containment guard --------------------------------------


def test_research_writes_only_research_dir(tmp_path: Path) -> None:
    snap = _snapshot(_ws_node(), output_policy=OutputPolicy.REPOSITORY_DOCUMENT)
    # A write confined to the report directory is accepted...
    inside = _Git(changed=(ChangedPath(status="??", path="docs/research/t/report.md"),))
    assert _run_agent(tmp_path, snap, _Router(), inside).outcome.kind == "done"
    # ...a write outside it (touching source) fails closed to manual review.
    outside = _Git(changed=(ChangedPath(status="M", path="src/app.py"),))
    with pytest.raises(NodeManualRequired):
        _run_agent(tmp_path, snap, _Router(), outside)


def test_audit_leaves_repo_byte_for_byte(tmp_path: Path) -> None:
    # The private report lives under the gitignored .worc/, so it never appears in the tracked
    # tree: an audit writing node leaves no git-visible change. Any tracked change → refusal.
    snap = _snapshot(_ws_node(), output_policy=OutputPolicy.PRIVATE_CONTROL_WORKSPACE_REPORT)
    assert _run_agent(tmp_path, snap, _Router(), _Git(changed=())).outcome.kind == "done"
    leaked = _Git(changed=(ChangedPath(status="M", path="src/app.py"),))
    with pytest.raises(NodeManualRequired):
        _run_agent(tmp_path, snap, _Router(), leaked)


# -- private-report publish ---------------------------------------------------


def _private_publish(tmp_path: Path, git: _Git) -> Any:
    node = PublishNode(
        id="private_storage",
        kind="publish",
        policy=PublishingPolicy.PRIVATE_CONTROL_WORKSPACE_REPORT,
    )
    snap = _snapshot(node, output_policy=OutputPolicy.PRIVATE_CONTROL_WORKSPACE_REPORT)
    return PublishNodeRunner(_services(tmp_path, git), NodeInputs(flow_dir=tmp_path)).run(
        node, _ctx(snap, node)
    )


def test_private_report_not_in_staging_commit_pr(tmp_path: Path) -> None:
    # The report is gitignored (no tracked change), so the publish touches git not at all.
    git = _Git(changed=())
    result = _private_publish(tmp_path, git)
    assert result.outcome.kind == "done"
    assert git.calls == []  # no commit_code / commit_audit / push / create_pr


def test_private_report_fail_closed_if_config_in_repo(tmp_path: Path) -> None:
    # The report directory is git-trackable (e.g. .worc/ not ignored) — it could enter staging/a
    # commit/a PR. The publish refuses rather than risk leaking the private report.
    git = _Git(changed=(ChangedPath(status="??", path=".worc/security-reports/t/report.md"),))
    with pytest.raises(NodeManualRequired):
        _private_publish(tmp_path, git)


# -- network grant ------------------------------------------------------------


def test_network_policy_off_by_default(tmp_path: Path) -> None:
    # A flow without network_policy grants no network: the request carries network_access=False.
    snap = _snapshot(_ws_node(), output_policy=OutputPolicy.CODE_CHANGE, network_policy=None)
    router = _Router()
    _run_agent(tmp_path, snap, router, _Git(changed=()))
    assert router.requests[0].network_access is False


def test_network_policy_granted_sets_request_flag(tmp_path: Path) -> None:
    snap = _snapshot(
        _ws_node(), output_policy=OutputPolicy.CODE_CHANGE, network_policy=NetworkPolicy.RESEARCH
    )
    router = _Router()
    _run_agent(tmp_path, snap, router, _Git(changed=()))
    assert router.requests[0].network_access is True
