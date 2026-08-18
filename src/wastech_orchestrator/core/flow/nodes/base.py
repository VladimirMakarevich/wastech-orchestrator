"""Shared contracts for the node-kind runners.

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
from wastech_orchestrator.git_manager import (
    ChangedPath,
    GitControlDrift,
    GitControlState,
    PushOutcome,
)
from wastech_orchestrator.notify import AskHandle, AskKind, AskResult
from wastech_orchestrator.providers.base import AgentRunRequest, ErrorClass, ProviderId
from wastech_orchestrator.providers.process import ProcessResult, run_process
from wastech_orchestrator.routing.router import ResolvedRoute, StageOutcome
from wastech_orchestrator.routing.snapshots import SnapshotHook
from wastech_orchestrator.runtime_layout import ProviderWriteGuardPolicy
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

    Raised when the Router exhausted every provider for a stage (no provider could complete it, even
    after fallback) and when a provider did not honor a mandatory structured-output contract. Never
    routed to fixing — no code change can repair infrastructure.

    ``error_classes`` is every class that was *raised* across the stage's attempts — the primary,
    its same-provider transient retries, and the fallback — in attempt order. It is what the
    park/manual/terminal decision reads, because the last attempt's class alone lets a fallback
    provider that failed worse than the primary mask a park-eligible failure of the primary, making
    the task's survival depend on a provider that never ran a token of work.

    ``error_class`` is the single *representative*: the class the Router settled on, used for
    messages, logs and the per-node audit row. It is also the only source of the operator-stop
    distinction — a stop replaces the representative with ``CANCELLED`` while the killed attempt's
    own row still reads as a process crash.

    A caller that legitimately knows exactly one class (an unparseable structured output, a
    synthesized cancellation) passes only ``error_class`` and the set is derived from it.

    ``resets_at`` is the provider's own claim about when a retry could succeed (ISO-8601 UTC), when
    one was reported. Untrusted input the Core validates and clamps before scheduling on it;
    ``None`` for every error carrying no such claim.
    """

    def __init__(
        self,
        message: str,
        *,
        error_class: ErrorClass | None = None,
        error_classes: Sequence[ErrorClass] = (),
        resets_at: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.resets_at = resets_at
        # An empty set falls back to the representative on purpose: a caller that knows one class,
        # and an exhausted stage whose attempt rows were never populated, must both decide on the
        # class they do have rather than fail closed on a set they never intended to leave empty.
        self.error_classes: tuple[ErrorClass, ...] = tuple(error_classes) or (
            () if error_class is None else (error_class,)
        )


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

    ``route_grants_shell`` answers whether an attempt on that route actually gets a shell — the
    per-attempt fact the Git-control detection brackets key on. It belongs to the Router because
    only the adapters can answer it (and both ends of a route must be asked), and to this protocol
    because a runner must be able to ask before it builds a request.
    """

    def resolve_route(self, node_id: str, provider: ProviderId | None = None) -> ResolvedRoute: ...

    def route_grants_shell(
        self, route: ResolvedRoute, *, permission_profile: str | None, git_evidence: bool
    ) -> bool: ...

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
        clock: Callable[[], str] = ...,  # wall-clock for the check_runs interval
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
    """Resolve an operator ``tool`` name → its executable path.

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
    diff for the dangerous-diff guard + ``{diff_path}``, both measured from the same point in the
    task. Git is the orchestrator's sole
    responsibility — providers and flows never touch it (the hard invariant).
    """

    def commit_code(self, task_id: str, message: str) -> str | None: ...

    def commit_subtask(self, task_id: str, order: int, slug: str, message: str) -> str: ...

    def commit_audit(
        self, task_id: str, *, task_packet_digest: str | None = None
    ) -> str | None: ...

    #: Fingerprint the Git control state before a workspace-write attempt and compare it
    #: after (the agent node runner brackets ``run_stage`` with these); drift is a policy violation.
    def capture_git_control_state(self) -> GitControlState: ...

    def compare_git_control_state(self, before: GitControlState) -> GitControlDrift | None: ...

    #: The tracked files matching the given pathspecs (used by the orchestrator to resolve the root
    #: instruction closure it freezes for the per-run audit digest).
    def list_tracked_files(self, *pathspecs: str) -> tuple[str, ...]: ...

    #: The absolute Git-control + ``tasks/`` roots a workspace-write attempt must
    #: Write/Edit-deny; the agent node runner threads it onto ``AgentRunRequest.write_guard``.
    #: Repository governance/instruction files are intentionally not denied — editing them is
    #: ordinary repository work, reported to the operator rather than blocked.
    def resolve_control_paths(
        self, exchange_root: str | None = None
    ) -> ProviderWriteGuardPolicy: ...

    #: Publishes the task branch and reports what it had to do to get there — including any
    #: commits it merged in because the remote branch had moved on without us.
    def push(
        self, task_id: str, branch: str, *, mode: BranchMode = BranchMode.NEW
    ) -> PushOutcome: ...

    #: ``notice`` is prepended to the PR body — used to declare commits publishing had to adopt,
    #: without which the PR's base-measured diff would silently describe someone else's work too.
    def create_pr(
        self,
        task_id: str,
        branch: str,
        *,
        title: str,
        body_path: str,
        notice: str | None = None,
    ) -> str | None: ...

    def write_current_diff(self, task_id: str) -> str: ...

    #: The task's change — committed or not — measured from the gate's reference point, not from
    #: ``HEAD``: a commit made inside the task must not be able to empty the dangerous-diff gate.
    def changed_code_entries(self, task_id: str) -> tuple[ChangedPath, ...]: ...

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
    #: the provider-readable exchange root ``<repo>/.worc-io``. Node runners publish their
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
    #: Claude max-turns gate: when true, a node run that exhausts ``max_turns`` pauses for
    #: a durable operator continue/stop decision (via ``notifier``) instead of failing immediately.
    #: Resolved by the orchestrator from ``agents.providers.claude.max_turns_gate``; off everywhere
    #: else (unit harnesses, codex-only setups). Requires a ``notifier``, guaranteed by preflight.
    max_turns_gate: bool = False
    #: heartbeat interval (s) for the blocking HITL human-input wait — the orchestrator-wide
    #: ``--heartbeat-seconds`` value, the same one driving the provider/git/check heartbeats. ``0``
    #: disables it (the unit-test default); the wait still logs its entry/resolution either way.
    ask_heartbeat_seconds: float = 0.0
    #: Observability: whether the prompt-audit JSON is written (per-task/global gate resolved
    #: by the orchestrator), the denied-read secrets to scrub from stored prompts, and the artifact
    #: register callback. ``register_artifact=None`` disables the on-disk audit artifacts.
    prompt_audit: bool = False
    prompt_secrets: tuple[str, ...] = ()
    register_artifact: RegisterArtifact | None = None
    #: orchestrator hook the publish node calls BEFORE the audit commit: move the task file into its
    #: lifecycle folder + write the committed ``<id>.summary.md`` (so both enter the audit commit),
    #: returning that summary path (used as the PR body). ``None`` → no finalize (e.g. a unit test).
    finalize: Callable[[], str | None] | None = None
    #: The frozen task-packet sha256 the publish node passes to ``commit_audit``
    #: so it verifies the lifecycle ``<id>.md`` was not rewritten under the run. ``None`` in a
    #: flow with no frozen packet (a unit harness / the ephemeral merge flow).
    task_packet_digest: str | None = None
    #: the ``dependency_scan`` checker's process runner + its allowlisted child env + per-scanner
    #: timeout. ``process_env`` is the same allowlisted env the Check Runner uses
    #: (``build_child_env(config.security)``); empty in unit harnesses.
    run_process: RunProcess = run_process
    process_env: Mapping[str, str] = field(default_factory=dict)
    scan_timeout_s: int = 600
    #: effective approval policy for the dangerous-diff gate (``config.security.trust_level`` with
    #: any per-task override applied). ``strict`` gates any deletion/dependency diff; ``auto`` gates
    #: only a ``protected_paths`` match.
    trust_level: str = "auto"
    #: operator allowlist (repo-relative globs) of paths that ALWAYS require approval on any change,
    #: regardless of ``trust_level`` (``config.security.protected_paths``). Empty = no floor.
    protected_paths: tuple[str, ...] = ()
    #: whether the operator enabled the read-only git-evidence grant
    #: (``config.security.allow_git_evidence``). A node's own ``git_evidence: true`` is only honored
    #: when this is on, so a flow can ask for the capability but never grant it to itself. ``False``
    #: everywhere it is not wired (unit harnesses), which is also the production default.
    allow_git_evidence: bool = False
    #: Defense-in-depth: the Core-owned orchestrator security contract prepended to every
    #: provider prompt (advisory, NOT enforcement). Resolved once by the orchestrator
    #: (``build_orchestrator_security_preamble``) and set on each request's ``security_preamble``.
    #: ``None`` in a unit harness → no preamble (today's prompt byte-for-byte).
    security_preamble: str | None = None
    #: memory read path (phase 03): builds a per-node retrieval packet for any node whose role
    #: prompt references ``{memory_path}``. ``None`` when memory is disabled (the default) — then no
    #: packet is built and ``{memory_path}`` renders empty (today's behavior).
    packet_builder: PacketBuilderPort | None = None
    #: operator tool registry: resolves a ``tool`` node's name → its executable under
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
    #: per-node read-only skill reference paths (absolute POSIX), keyed by node id — the effective
    #: set the Core resolved for each node (operator pins ∪ accepted dynamic proposal). A node with
    #: no skills is simply absent from the map (``skills_for`` returns ``()``).
    skill_paths_by_node: dict[str, tuple[str, ...]] = field(default_factory=dict)
    subtask_count: int | None = None
    subtask_spec_path: str | None = None
    #: the intra-task subtask handoff brief path, set by the
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
    #: Defaults to ``new`` (an orchestrator-owned branch).
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
