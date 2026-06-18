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
from wastech_orchestrator.git_manager import ChangedPath
from wastech_orchestrator.notify import AskHandle, AskKind, AskResult
from wastech_orchestrator.providers.base import AgentRunRequest, Stage
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.routing.snapshots import SnapshotHook
from wastech_orchestrator.state_store import CheckRunRow, NodeRunRow, ProviderAttemptRow

#: Register a written artifact in the audit trail: ``(task_id, kind, path)``. The orchestrator's
#: ``_register_artifact`` (sha256 + upsert, skips a missing file) satisfies it.
RegisterArtifact = Callable[[str, str, str], None]


class NodeInfraError(Exception):
    """A node could not run because of an infrastructure failure (not a quality result).

    Agent infra-exhaustion (no provider could complete the stage even after fallback) and a check
    *launch* failure both raise this; the orchestrator maps it to terminal ``failed`` — it never
    routes to fixing (no code change can fix infrastructure). Mirrors the legacy ``PipelineFailed``
    + ``CheckOutcome.launch_failed`` handling.
    """


class NodeManualRequired(Exception):
    """A node needs human action and cannot proceed automatically (terminal manual_action_required).

    Raised by the dangerous-diff guard when a workspace-write edit produced a deletion/dependency
    change. Mirrors the legacy ``ManualActionRequired``. The orchestrator maps it to terminal
    ``manual_action_required``. (The full durable approval round-trip — prompt, persist, resume,
    reconsider-on-denial — is the next Step-B piece; until then a dangerous diff fails closed.)
    """


class RouterPort(Protocol):
    """The slice of :class:`~wastech_orchestrator.routing.router.AgentRouter` runners use.

    The runners call ``resolve_route(stage)`` without a per-task override (P1 routes by stage from
    config); the real router's extra optional ``override`` param still satisfies this protocol.
    """

    def resolve_route(self, stage: Stage) -> ResolvedRoute: ...

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
    """The slice of :class:`~wastech_orchestrator.state_store.StateStore` runners use.

    The runners never pass the optional ``conn`` (single-connection store); omitting it here keeps
    the protocol free of the concrete ``sqlite3.Connection`` type, and the store's extra optional
    ``conn`` param still satisfies these signatures structurally.
    """

    def record_node_run(self, run: NodeRunRow) -> int: ...

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
    ) -> None: ...

    def record_check_run(self, run: CheckRunRow) -> None: ...

    def record_provider_attempt(self, attempt: ProviderAttemptRow) -> None: ...


class NotifierPort(Protocol):
    """The slice of :class:`~wastech_orchestrator.notify.interface.Notifier` the durable HITL gate
    uses: start one correlated prompt, then wait for the answer against the persisted deadline."""

    def start_ask(
        self,
        *,
        question: str,
        context: str,
        task_id: str,
        kind: AskKind,
        timeout_s: int,
        interaction_id: str,
        contacts: tuple[str, ...] = (),
    ) -> AskHandle: ...

    def wait_for_answer(self, handle: AskHandle) -> AskResult: ...


class GitPort(Protocol):
    """The slice of :class:`~wastech_orchestrator.git_manager.GitManager` the node runners use.

    The publish operations are idempotent (keyed by ``publish_operations``), so a resumed run never
    repeats a commit/push/PR; ``write_current_diff``/``changed_code_entries`` capture the post-edit
    diff for the dangerous-diff guard + ``{diff_path}``. Git is the orchestrator's sole
    responsibility — providers and flows never touch it (the hard invariant).
    """

    def commit_code(self, task_id: str, message: str) -> str | None: ...

    def commit_audit(self, task_id: str) -> str | None: ...

    def push(self, task_id: str, branch: str) -> bool: ...

    def create_pr(
        self, task_id: str, branch: str, *, title: str, body_path: str
    ) -> str | None: ...

    def write_current_diff(self, task_id: str) -> str: ...

    def changed_code_entries(self) -> tuple[ChangedPath, ...]: ...


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
    #: the orchestrator's git manager (it owns git; providers/flows never touch it). Set for any
    #: flow with a publish node or a workspace-write agent node (the dangerous-diff guard).
    git: GitPort | None = None
    #: durable HITL transport (refinement/planning embedded HITL + dangerous-diff approval).
    notifier: NotifierPort | None = None
    ask_timeout_s: int = 0
    #: observability (P1.4): whether the prompt-audit JSON is written (per-task/global gate resolved
    #: by the orchestrator), the denied-read secrets to scrub from stored prompts, and the artifact
    #: register callback. ``register_artifact=None`` disables the on-disk audit artifacts.
    prompt_audit: bool = False
    prompt_secrets: tuple[str, ...] = ()
    register_artifact: RegisterArtifact | None = None
    #: orchestrator hook the publish node calls BEFORE the audit commit: move the task file into its
    #: lifecycle folder + write the committed ``<id>.summary.md`` (so both enter the audit commit),
    #: returning that summary path (used as the PR body). ``None`` → no finalize (e.g. a unit test).
    finalize: Callable[[], str | None] | None = None
    #: orchestrator hook the checks node calls on a check *launch* failure: re-resolve the check
    #: command set once (gated by the change-approval), returning the new checks to retry, or
    #: ``None`` when no different ready profile exists. Bounded to once per task by the hook itself
    #: (it returns ``None`` after the first re-resolve). Ports the legacy re-resolve-on-launch-fail.
    check_reresolve: Callable[[], tuple[ResolvedCheck, ...] | None] | None = None


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
    #: notification recipients for HITL prompts (the task's contacts).
    contacts: tuple[str, ...] = ()
    #: in-memory provider -> session id map (legacy parity; durable lineage is P2.2).
    session_ids: dict[str, str] = field(default_factory=dict)
