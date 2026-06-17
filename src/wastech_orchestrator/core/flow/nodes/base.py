"""Shared contracts for the node-kind runners (P1.3).

:class:`NodeServices` — the collaborators a runner needs, constructed once per orchestrator and
shared across units. :class:`NodeInputs` — the per-execution-unit data bundle (artifact paths,
resolved checks, the in-memory session map), constructed per unit. Both are injected into a runner
at construction so the engine and :class:`~wastech_orchestrator.core.flow.engine.NodeContext` stay
free of per-run context.

The collaborator fields are typed as narrow :class:`Protocol`\\ s so the real
``AgentRouter``/``CheckRunner``/``StateStore`` satisfy them structurally and tests can pass fakes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from wastech_orchestrator.check_runner import CheckOutcome
from wastech_orchestrator.checks.model import ResolvedCheck
from wastech_orchestrator.providers.base import AgentRunRequest, Stage
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.routing.snapshots import SnapshotHook
from wastech_orchestrator.state_store import CheckRunRow, NodeRunRow


class NodeInfraError(Exception):
    """A node could not run because of an infrastructure failure (not a quality result).

    Agent infra-exhaustion (no provider could complete the stage even after fallback) and a check
    *launch* failure both raise this; the orchestrator maps it to terminal ``failed`` — it never
    routes to fixing (no code change can fix infrastructure). Mirrors the legacy ``PipelineFailed``
    + ``CheckOutcome.launch_failed`` handling.
    """


class RouterPort(Protocol):
    """The slice of :class:`~wastech_orchestrator.routing.router.AgentRouter` runners use."""

    def resolve_route(
        self, stage: Stage, override: Mapping[Stage, object] | None = None
    ) -> ResolvedRoute: ...

    def run_stage(
        self,
        request: AgentRunRequest,
        route: ResolvedRoute,
        *,
        snapshot: SnapshotHook | None = None,
    ) -> StageOutcome: ...


class CheckRunnerPort(Protocol):
    """The slice of :class:`~wastech_orchestrator.check_runner.CheckRunner` runners use."""

    def run(
        self,
        *,
        clone_dir: str | Path,
        artifacts_root: str | Path,
        task_id: str,
        subtask: int | None = None,
        checks: Sequence[ResolvedCheck] | None = None,
    ) -> CheckOutcome: ...


class NodeRunStorePort(Protocol):
    """The slice of :class:`~wastech_orchestrator.state_store.StateStore` runners use."""

    def record_node_run(self, run: NodeRunRow, conn: object | None = None) -> int: ...

    def complete_node_run(
        self,
        run_id: int,
        *,
        status: str,
        outcome: str | None,
        provider_used: str | None = ...,
        error_class: str | None = ...,
        stage_attempts: int = ...,
        finished_at: str,
        commit_sha_after: str | None = ...,
        conn: object | None = None,
    ) -> None: ...

    def record_check_run(self, run: CheckRunRow, conn: object | None = None) -> None: ...


class GitPublishPort(Protocol):
    """The slice of :class:`~wastech_orchestrator.git_manager.GitManager` the publish runner uses.

    Every method is idempotent (keyed by ``publish_operations``), so a resumed run never repeats a
    commit/push/PR. Git is the orchestrator's sole responsibility — providers and flows never touch
    it (the hard invariant).
    """

    def commit_code(self, task_id: str, message: str) -> str | None: ...

    def commit_audit(self, task_id: str) -> str | None: ...

    def push(self, task_id: str, branch: str) -> bool: ...

    def create_pr(
        self, task_id: str, branch: str, *, title: str, body_path: str
    ) -> str | None: ...


@dataclass(frozen=True)
class NodeServices:
    """Collaborators shared across a unit's node runners."""

    router: RouterPort
    check_runner: CheckRunnerPort
    store: NodeRunStorePort
    repo_dir: str
    artifacts_root: str
    #: node id -> legacy routing ``Stage`` (parity routing map; routing is by ``Stage`` until P1.5).
    stage_for_node: Mapping[str, Stage]
    clock: Callable[[], str]
    default_timeout_seconds: int = 7200
    snapshot: SnapshotHook | None = None  # git snapshot hook for provider observability
    #: set only for flows with a publish node (the orchestrator owns git; providers/flows do not).
    git: GitPublishPort | None = None


@dataclass
class NodeInputs:
    """Per-execution-unit data the runners read (artifact paths + resolved checks + session map)."""

    flow_dir: Path
    task_path: str | None = None
    plan_path: str | None = None
    diff_path: str | None = None
    checks_path: str | None = None
    review_path: str | None = None
    skill_paths: tuple[str, ...] = ()
    subtask_count: int | None = None
    subtask_spec_path: str | None = None
    resolved_checks: tuple[ResolvedCheck, ...] = ()
    #: publish inputs (set for the unit that reaches a publish node).
    branch: str | None = None
    pr_title: str | None = None
    summary_body_path: str | None = None
    commit_message: str | None = None
    #: in-memory provider -> session id map (legacy parity; durable lineage is P2.2).
    session_ids: dict[str, str] = field(default_factory=dict)
