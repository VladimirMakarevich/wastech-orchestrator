"""Shared contracts for the node-kind runners (P1.3).

:class:`NodeServices` — the collaborators a runner needs, constructed once per orchestrator and
shared across units. :class:`NodeInputs` — the per-execution-unit data bundle (artifact paths,
resolved checks), constructed per unit. Both are injected into a runner
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
from wastech_orchestrator.checks.model import ResolvedCheckSet
from wastech_orchestrator.config.schema import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    BranchMode,
    PublishScope,
)
from wastech_orchestrator.git_manager import ChangedPath
from wastech_orchestrator.notify import AskHandle, AskKind, AskResult
from wastech_orchestrator.providers.base import AgentRunRequest, ErrorClass, ProviderId
from wastech_orchestrator.providers.process import ProcessResult, run_process
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.routing.snapshots import SnapshotHook
from wastech_orchestrator.state_store import (
    CheckRunRow,
    EditingLineageRow,
    EvaluationRow,
    NodeLineageRow,
    NodeRunRow,
    ProviderAttemptRow,
)

#: Register a written artifact in the audit trail: ``(task_id, kind, path)``. The orchestrator's
#: ``_register_artifact`` (sha256 + upsert, skips a missing file) satisfies it.
RegisterArtifact = Callable[[str, str, str], None]

#: The safe argv process runner (``providers.process.run_process``) the ``dependency_scan`` checker
#: launches its scanners through (argv-without-shell, mandatory timeout, allowlisted env).
RunProcess = Callable[..., ProcessResult]


class NodeInfraError(Exception):
    """A node could not run because of an infrastructure failure (not a quality result).

    Agent infra-exhaustion (no provider could complete the stage even after fallback) and a check
    *launch* failure both raise this; the orchestrator maps it to terminal ``failed`` — it never
    routes to fixing (no code change can fix infrastructure).

    ``error_class`` carries the normalized terminal error class (``None`` when unknown — e.g. a
    check launch failure) so the orchestrator can tell a *transient* exhaustion
    (PROVIDER_UNAVAILABLE / NETWORK_UNAVAILABLE — both providers down) apart from a hard infra
    failure: the former parks the task as resumable (B-lite), the latter goes terminal.
    """

    def __init__(self, message: str, *, error_class: ErrorClass | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class


class EvaluatorInfraError(NodeInfraError):
    """An *evaluator* node could not run (no provider could complete it).

    A subclass of :class:`NodeInfraError` (so generic infra handling still catches it), raised only
    by the evaluator runner. The orchestrator distinguishes it: an evaluator that could not *run*
    (infra/misconfig) must not discard an already-green diff. It degrades to
    ``manual_action_required`` — the branch is preserved and the operator reviews/publishes — unlike
    an agent node whose infra exhaustion leaves no usable result to ship (terminal ``failed``).
    """


class NodeManualRequired(Exception):
    """A node needs human action and cannot proceed automatically (terminal manual_action_required).

    Raised by the dangerous-diff guard when a workspace-write edit produced a deletion/dependency
    change. The orchestrator maps it to terminal
    ``manual_action_required``. (The full durable approval round-trip — prompt, persist, resume,
    reconsider-on-denial — is the next Step-B piece; until then a dangerous diff fails closed.)
    """


class RouterPort(Protocol):
    """The slice of :class:`~wastech_orchestrator.routing.router.AgentRouter` runners use.

    Runners pass the flow node's declared ``provider`` to ``resolve_route``; ``None`` defaults to
    the config's global primary (PRE.1). ``node_id`` is carried for audit/logging only — it no
    longer selects the provider.
    """

    def resolve_route(self, node_id: str, provider: ProviderId | None = None) -> ResolvedRoute: ...

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
        selected: Sequence[ResolvedCheckSet] | None = None,
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

    def record_evaluation(self, row: EvaluationRow) -> int: ...

    def count_rework_verdicts(
        self, task_id: str, *, node_id: str | None = ..., subtask_order: int | None = ...
    ) -> int: ...

    def get_editing_lineage(
        self, task_id: str, lineage_key: str, subtask_order: int | None = ...
    ) -> EditingLineageRow | None: ...

    def upsert_editing_lineage(self, row: EditingLineageRow) -> None: ...

    def get_node_lineage(
        self, task_id: str, node_id: str, subtask_order: int | None = ...
    ) -> NodeLineageRow | None: ...

    def upsert_node_lineage(self, row: NodeLineageRow) -> None: ...


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


class PacketBuilderPort(Protocol):
    """The slice of the memory :class:`~wastech_orchestrator.memory.packet.PacketBuilder` the node
    runners use: build a per-node retrieval packet and write it to ``dest``.

    Returns the packet path when one was written, or ``None`` when there is no relevant memory (no
    file is created — so ``{memory_path}`` renders empty). Set only when memory is enabled; ``None``
    on :class:`NodeServices` is the disabled state (a no-op, today's behavior).
    """

    def write_packet(
        self,
        *,
        node_id: str,
        task_type: str | None,
        touched_paths: Sequence[str],
        dest: Path,
    ) -> Path | None: ...


class ToolResolverPort(Protocol):
    """Resolve an operator ``tool`` name → its executable path (P5).

    The concrete :class:`~wastech_orchestrator.core.flow.tools_registry.ToolRegistry` satisfies it
    structurally; a test passes a fake. ``None`` on :class:`NodeServices` means no operator tool
    layer is wired — a flow with a ``tool`` node then fails closed at run time (the fatal
    install/preflight gate already rejects an unregistered tool, so this is defense in depth).
    ``resolve`` raises :class:`~wastech_orchestrator.core.flow.tools_registry.ToolResolutionError`
    when the name is unknown / uncontained / not executable.
    """

    def resolve(self, name: str) -> Path: ...


class GitPort(Protocol):
    """The slice of :class:`~wastech_orchestrator.git_manager.GitManager` the node runners use.

    The publish operations are idempotent (keyed by ``publish_operations``), so a resumed run never
    repeats a commit/push/PR; ``write_current_diff``/``changed_code_entries`` capture the post-edit
    diff for the dangerous-diff guard + ``{diff_path}``. Git is the orchestrator's sole
    responsibility — providers and flows never touch it (the hard invariant).
    """

    def commit_code(self, task_id: str, message: str) -> str | None: ...

    def commit_subtask(self, task_id: str, order: int, slug: str, message: str) -> str: ...

    def commit_audit(self, task_id: str) -> str | None: ...

    def push(self, task_id: str, branch: str, *, mode: BranchMode = BranchMode.NEW) -> bool: ...

    def create_pr(self, task_id: str, branch: str, *, title: str, body_path: str) -> str | None: ...

    def write_current_diff(self, task_id: str) -> str: ...

    def changed_code_entries(self) -> tuple[ChangedPath, ...]: ...

    def changed_code_paths(self) -> list[str]: ...

    def changed_code_paths_since_base(self) -> list[str]: ...

    def changed_code_paths_since_task_base(self) -> list[str]: ...


@dataclass(frozen=True)
class NodeServices:
    """Collaborators shared across a unit's node runners."""

    router: RouterPort
    check_runner: CheckRunnerPort
    store: NodeRunStorePort
    repo_dir: str
    artifacts_root: str
    clock: Callable[[], str]
    #: the provider-readable exchange root ``<repo>/.worc-io`` (WRI-001). Node runners publish their
    #: agent-facing artifacts here through :mod:`~wastech_orchestrator.providers.exchange` and
    #: resolve the exchange fan-in from it. Empty in a unit harness that does no publication.
    exchange_root: str = ""
    default_timeout_seconds: int = 7200
    snapshot: SnapshotHook | None = None  # git snapshot hook for provider observability
    #: the orchestrator's git manager (it owns git; providers/flows never touch it). Set for any
    #: flow with a publish node or a workspace-write agent node (the dangerous-diff guard).
    git: GitPort | None = None
    #: durable HITL transport (refinement/planning embedded HITL + dangerous-diff approval).
    notifier: NotifierPort | None = None
    ask_timeout_s: int = 0
    #: Claude max-turns gate (idea 29): when true, a node run that exhausts ``max_turns`` pauses for
    #: a durable operator continue/stop decision (via ``notifier``) instead of failing immediately.
    #: Resolved by the orchestrator from ``agents.providers.claude.max_turns_gate``; off everywhere
    #: else (unit harnesses, codex-only setups). Requires a ``notifier``, guaranteed by preflight.
    max_turns_gate: bool = False
    #: heartbeat interval (s) for the blocking HITL human-input wait — the orchestrator-wide
    #: ``--heartbeat-seconds`` value, the same one driving the provider/git/check heartbeats. ``0``
    #: disables it (the unit-test default); the wait still logs its entry/resolution either way.
    ask_heartbeat_seconds: float = 0.0
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
    #: the ``dependency_scan`` checker's process runner + its allowlisted child env + per-scanner
    #: timeout. ``process_env`` is the same allowlisted env the Check Runner uses
    #: (``build_child_env(config.security.allowed_environment)``); empty in unit harnesses.
    run_process: RunProcess = run_process
    process_env: Mapping[str, str] = field(default_factory=dict)
    scan_timeout_s: int = 600
    #: effective approval policy for the dangerous-diff gate (``config.security.trust_level`` with
    #: any per-task override applied). ``strict`` gates any deletion/dependency diff; ``auto`` gates
    #: only a ``protected_paths`` match.
    trust_level: str = "strict"
    #: operator allowlist (repo-relative globs) of paths that ALWAYS require approval on any change,
    #: regardless of ``trust_level`` (``config.security.protected_paths``). Empty = no floor.
    protected_paths: tuple[str, ...] = ()
    #: memory read path (phase 03): builds a per-node retrieval packet for any node whose role
    #: prompt references ``{memory_path}``. ``None`` when memory is disabled (the default) — then no
    #: packet is built and ``{memory_path}`` renders empty (today's behavior).
    packet_builder: PacketBuilderPort | None = None
    #: operator tool registry (P5): resolves a ``tool`` node's name → its executable under
    #: ``<repo>/.worc/tools/``. ``None`` = no operator tool layer wired (a unit harness or a
    #: tool-less setup); a ``tool`` node then fails closed to manual at run time.
    tool_registry: ToolResolverPort | None = None
    #: flow-wide default wall-clock timeout (s) for a ``tool`` node whose own ``timeout_seconds`` is
    #: unset (``config.tools.default_timeout_seconds``; 3600 = 1h by default).
    tools_default_timeout_seconds: int = DEFAULT_TOOL_TIMEOUT_SECONDS


@dataclass
class NodeInputs:
    """Per-execution-unit data the runners read (artifact paths + resolved checks + session map)."""

    flow_dir: Path
    #: the task's ``task_type`` (flow dispatch key) — a packet-retrieval signal (memory phase 03);
    #: ``None`` for the default implementation flow.
    task_type: str | None = None
    task_path: str | None = None
    plan_path: str | None = None
    diff_path: str | None = None
    checks_path: str | None = None
    review_path: str | None = None
    #: WRI-011 frozen repository-instruction injection file (redacted exchange copy of the root
    #: AGENTS.md/CLAUDE.md/AGENTS.override.md concatenation). ``None`` when the repo defines no
    #: tracked root instruction files. The adapters inject it through their instruction layer and
    #: disable provider-native live project-instruction discovery; it is never re-read live.
    repository_instructions_path: str | None = None
    #: per-node read-only skill reference paths (absolute POSIX), keyed by node id — the effective
    #: set the Core resolved for each node (operator pins ∪ accepted dynamic proposal). A node with
    #: no skills is simply absent from the map (``skills_for`` returns ``()``).
    skill_paths_by_node: dict[str, tuple[str, ...]] = field(default_factory=dict)
    subtask_count: int | None = None
    subtask_spec_path: str | None = None
    #: the intra-task subtask handoff brief path (subtask-context-handoff ADR), set by the
    #: orchestrator per subtask that has ``depends_on`` predecessors; ``None`` outside a decompose
    #: region or for a subtask with no predecessors. Injected as ``{predecessor_context}`` into the
    #: region's ``implementation`` node only when its template references it (node-driven opt-in).
    predecessor_context_path: str | None = None
    #: the normalized ``checks.command_sets`` (diff-selected at run time by the checks node). An
    #: empty tuple means *no gate* — the checks node passes vacuously.
    check_sets: tuple[ResolvedCheckSet, ...] = ()
    #: publish inputs (set for the unit that reaches a publish node).
    branch: str | None = None
    #: the task's effective branch mode — governs whether ``push`` may target the base branch
    #: (branch-mode ADR). Defaults to ``new`` (an orchestrator-owned branch).
    branch_mode: BranchMode = BranchMode.NEW
    #: the per-task downgrade-only publish cap (``commit``/``push``/``pull_request``), or ``None``
    #: to defer to the flow's ``PublishingPolicy``. A downgrade cap (:class:`PublishScope`).
    publish_scope: PublishScope | None = None
    pull_request_title: str | None = None
    summary_body_path: str | None = None
    commit_message: str | None = None
    #: notification recipients for HITL prompts (the task's contacts).
    contacts: tuple[str, ...] = ()

    def skills_for(self, node_id: str) -> tuple[str, ...]:
        """The read-only skill reference paths resolved for *node_id* (``()`` when it has none)."""
        return self.skill_paths_by_node.get(node_id, ())
