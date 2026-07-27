"""Flow execution engine — graph driver with engine-owned transitions.

:class:`FlowEngine` executes a validated :class:`~.snapshot.FlowSnapshot`: starting at the entry
node it runs each node through its :class:`NodeRunner`, takes the node's :class:`NodeOutcome`,
resolves the matching outgoing edge from the snapshot adjacency, and transitions. **Only the engine
moves execution** — a ``NodeRunner`` returns an outcome but never picks the next node and never
changes task status. This is the single execution model — it replaced the hardcoded
dispatch-on-``Status`` pipeline loop the orchestrator used before the flow engine.

Guarantees:

* **Outcome ⊆ declared edges.** The chosen outcome must match a declared outgoing edge; a mismatch
  is an :class:`EngineInternalError` (the fatal load-time validator already rejects malformed
  graphs at
  load, so this is a runtime assertion against a buggy runner).
* **Bounded termination.** Every ``rework``/``fail`` edge is charged against a single global fix
  counter plus its named loop or inline budget; exhausting any limit ends the run at
  ``MANUAL_ACTION_REQUIRED`` with a failure report. Reaching a node with no outgoing edges ends the
  run at ``DONE``.

Budget bookkeeping is generic on purpose: the engine knows nothing about ``test_fix`` /
``review_fix`` / supervisor by name (an abstraction test forbids domain knowledge in the
engine). Named loops use ``>=`` semantics — increment then compare (verified by the fix-loop
scenarios) — and inline ``budget: N`` edges use ``allow N`` semantics. Flow ``budgets`` parameterize
the limits;
``agents.max_fix_cycles`` / ``agents.max_total_fix_iterations`` clamp them as the unlosable
backstop — the effective cap is ``min(flow_budget, config_cap)``.

The node runners are the core-owned wrappers in :mod:`~wastech_orchestrator.core.flow.nodes`; the
checkpoint is persisted by the :class:`RunRecorder` (see ``core.flow.recorder``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal, Protocol

from wastech_orchestrator.config.schema import AgentsConfig
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import (
    REWORK_OUTCOMES,
    ChecksNode,
    Edge,
    EvaluatorNode,
    FlowNode,
    ToolNode,
)
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.loop_control import global_cap, loop_cap, record_rework
from wastech_orchestrator.core.state_machine import Status

#: Resolves a ``when.fact`` (``derived.*`` / ``config.*``) to a boolean. Injected so the engine
#: carries no knowledge of where facts come from (the driver wires the real resolver).
FactResolver = Callable[[str], bool]
#: Reports whether the operator requested a cooperative stop. Injected by the watch daemon; the
#: engine treats it as a generic boundary interrupt and stays ignorant of stop files and signals.
CancellationCheck = Callable[[], bool]
#: EXPERIMENTAL(no-work-infra) — the no-effective-work stall guard is a trial feature; grep the tag
#: ``no-work-infra`` to find every site (alias, constant, constructor state, ``_check_stall``, its
#: call in ``run()``, and the ``_reset_loops_at`` cleanup) and revert as one unit if we drop it.
#: Returns an opaque fingerprint of the current working tree (the ``current.diff`` text, hashed or
#: raw). The engine only compares consecutive fingerprints for equality — it never learns what a
#: diff is (same domain-free contract as :data:`FactResolver`). Injected by the driver; ``None``
#: leaves the no-effective-work stall guard inert.
DiffFingerprint = Callable[[], str]


class EngineInternalError(Exception):
    """A runtime invariant the validator should have prevented was broken (a bug, not bad YAML)."""


class FlowCancelled(Exception):
    """Cooperative stop observed before the checkpointed node started.

    ``node_id`` is the untouched resume point. The orchestrator maps this generic engine interrupt
    to its existing resumable ``ErrorClass.CANCELLED`` parking path.
    """

    def __init__(self, node_id: str) -> None:
        super().__init__(f"stop requested before flow node {node_id!r}")
        self.node_id = node_id


def _never_cancelled() -> bool:
    """Default cancellation seam for one-shot runs and isolated engine tests."""
    return False


@dataclass(frozen=True, slots=True)
class Finding:
    """A single evaluator finding (the shared evaluator primitive).

    ``severity`` is the typed audit-trail projection (``low``/``medium``/``high``). Whether a
    finding actually drives ``rework`` is decided by the evaluator runner against the node's
    configurable ``gate_severity`` (default ``high``) — NOT by this flag: ``paths`` are the
    files/locations the finding concerns. Carried on :class:`NodeOutcome` for the audit trail (the
    immutable ``evaluations`` row) — the engine never inspects it to route.
    """

    severity: Literal["low", "medium", "high"]
    reason: str
    paths: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        """Audit-only high-severity flag (``severity == "high"``); does NOT decide routing.

        The routing gate is the evaluator runner's ``gate_severity`` comparison, not this property.
        """
        return self.severity == "high"


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """What a node returned to the engine. Never names the next node directly.

    ``kind`` is the edge-selecting outcome: ``"accept"`` / ``"rework"`` (evaluator stage_output),
    ``"pass"`` / ``"fail"`` (checks), ``"done"`` (an unconditional node took its single edge), or
    an explicit ``"route:<label>"``. ``structured_output`` / ``final_message`` carry the agent
    output so the post-node hook can persist a declared ``output_artifact`` slot and read the
    decomposition contract — the engine itself never inspects them.

    ``rework_exhausted`` is set by the evaluator runner on the one ``accept`` where a
    **non-blocking** evaluator gave up: it still found a gating issue but its per-instance
    ``max_rework_per_stage`` budget was spent, so it takes ``accept`` (→ continue) with findings
    still open. The engine never inspects it (``accept`` is ``accept`` for routing); the
    orchestrator's post-node hook surfaces it as an operator warning + Telegram trace so a human
    knows the stage moved on and may need follow-up.

    ``read_only_write`` is the same shape of signal for a different event: a read-only node holding
    the git-evidence grant changed the working tree, which the provider's sandbox is supposed to
    make impossible. The outcome stays ``done`` and the run continues — the grant buys an audit node
    real history, and parking a task over a stray file would trade that for a hypothetical — but the
    post-node hook warns the operator through the same console + ⚠️ trace surface.

    ``read_only_git_drift`` is the second, sharper event on that same never-park path (operator
    decision 2, 2026-07-26): the same node class changed **Git control state** — a hook,
    ``.git/config``, the index. It carries the redacted drift summary rather than a bool precisely
    because the warning *is* the mitigation here — an operator told only "something changed" would
    inspect the working tree, while the aspect that matters ("hooks: hook 'post-commit' added") is
    the one that makes the next orchestrator git command execute provider-supplied code. The same
    node on a ``workspace-write`` profile still parks the task; see the agent runner.
    """

    kind: str
    findings: tuple[Finding, ...] = ()
    structured_output: Mapping[str, object] | None = None
    final_message: str | None = None
    rework_exhausted: bool = False
    read_only_write: bool = False
    read_only_git_drift: str | None = None


def skip_outcome(node: FlowNode) -> NodeOutcome:
    """The pass-through outcome a skipped node yields, so the engine takes its forward edge.

    The single source of truth for the skip-outcome-per-node-kind rule, shared by the engine's
    skip path and the flow validator's per-task disabled-node routing-soundness check.
    """
    if isinstance(node, EvaluatorNode):
        return NodeOutcome("accept")
    if isinstance(node, (ChecksNode, ToolNode)):
        return NodeOutcome("pass")
    return NodeOutcome("done")


@dataclass(frozen=True, slots=True)
class NodeResult:
    """A node's execution result: its outcome plus the ``node_runs`` row id it recorded."""

    node_id: str
    outcome: NodeOutcome
    node_run_id: int


@dataclass(frozen=True, slots=True)
class NodeContext:
    """Read-only context handed to a :class:`NodeRunner` (the driver adds the artifact paths)."""

    snapshot: FlowSnapshot
    run_state: FlowRunState
    node: FlowNode
    task_id: str
    subtask_order: int | None = None


class NodeRunner(Protocol):
    """Implemented by each node kind in ``core/flow/nodes/*.py``. Returns an outcome; it
    never transitions the graph or the task status — that is the engine's sole responsibility."""

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult: ...


class RunRecorder(Protocol):
    """Engine-level persistence seam (``record_skip`` / checkpoint of ``current_node`` +
    ``loop_counters`` / failure report). Backed by the state store; tests use an in-memory fake."""

    def record_skip(self, node: FlowNode, *, reason: str, subtask_order: int | None) -> None: ...

    def save_checkpoint(self, run_state: FlowRunState) -> None: ...

    def write_failure_report(
        self,
        *,
        node_id: str,
        loop: str | None,
        limit_name: str,
        run_state: FlowRunState,
        subtask_order: int | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class FlowRunResult:
    """Terminal outcome of one unit's graph traversal."""

    status: Status  # DONE | FAILED | MANUAL_ACTION_REQUIRED
    final_node: str
    stuck_loop: str | None = None
    limit_name: str | None = None
    failure_report_path: str | None = None


@dataclass(frozen=True, slots=True)
class _Stuck:
    """A rework/fail edge could not be taken because a budget is exhausted."""

    loop: str | None
    limit_name: str


#: EXPERIMENTAL(no-work-infra). Consecutive fixing cycles with an unchanged working tree that abort
#: a fix loop as a no-effective-work stall (the "agent emits tokens but never edits" case the
#: provider boundary cannot observe).
_STALL_NO_CHANGE_LIMIT = 2
_NO_OVERRIDES: Mapping[str, Mapping[str, object]] = MappingProxyType({})


def edge_key(edge: Edge) -> str:
    """Synthetic ``loop_counters`` key for an inline ``budget`` edge (no named loop)."""
    return f"{edge.from_node}->{edge.to}:{edge.outcome}"


#: Called after each *executed* (non-skipped) node with ``(node, outcome, node_run_id)`` so the core
#: can persist a declared ``output_artifact`` slot / read the decomposition contract and let the
#: orchestrator's supervisor layer observe the completed step (keyed by its ``node_run_id``) before
#: the next node runs. Injected by the driver; the engine carries no post-processing
#: knowledge itself — passing the run id it already holds is generic, not domain knowledge.
PostNodeHook = Callable[[FlowNode, NodeOutcome, int], None]


def entry_node_id(snapshot: FlowSnapshot) -> str:
    """The single entry node (zero incoming edges). The validator guarantees exactly one.

    Exposed so the driver can seed the first checkpoint (fingerprint + entry) *before* the entry
    node runs — otherwise a crash during the entry node leaves no checkpoint and resume restarts.
    """
    incoming = dict.fromkeys(snapshot.nodes_by_id, 0)
    for edges in snapshot.adjacency.values():
        for edge in edges:
            if edge.to in incoming:
                incoming[edge.to] += 1
    entries = [nid for nid, n in incoming.items() if n == 0]
    if len(entries) != 1:  # the validator guarantees exactly one entry
        raise EngineInternalError(f"expected exactly one entry node, got {sorted(entries)}")
    return entries[0]


class FlowEngine:
    """Drives one unit through a validated flow graph; the engine owns every transition."""

    def __init__(
        self,
        snapshot: FlowSnapshot,
        run_state: FlowRunState,
        runners: Mapping[str, NodeRunner],
        recorder: RunRecorder,
        *,
        facts: FactResolver,
        agents: AgentsConfig,
        task_id: str,
        is_cancelled: CancellationCheck = _never_cancelled,
        subtask_order: int | None = None,
        post_node: PostNodeHook | None = None,
        diff_fingerprint: DiffFingerprint | None = None,
        region: frozenset[str] | None = None,
        disabled_nodes: frozenset[str] = frozenset(),
        node_overrides: Mapping[str, Mapping[str, object]] = _NO_OVERRIDES,
    ) -> None:
        self._snapshot = snapshot
        self._run_state = run_state
        self._runners = runners
        self._recorder = recorder
        self._facts = facts
        self._agents = agents
        self._task_id = task_id
        self._is_cancelled = is_cancelled
        self._subtask_order = subtask_order
        self._post_node = post_node
        # EXPERIMENTAL(no-work-infra). No-effective-work stall guard (transient, never persisted — a
        # reset on resume is desired). ``_stall_fp`` holds the last working-tree fingerprint seen
        # when charging a rework for each loop; ``_stall_streak`` counts consecutive unchanged
        # cycles. ``None`` callable => inert.
        self._diff_fingerprint = diff_fingerprint
        self._stall_fp: dict[str, str] = {}
        self._stall_streak: dict[str, int] = {}
        # Flow node ids the task disabled (``nodes.<id>.enabled: false``); each is skipped exactly
        # like a ``when``-false node — its pass-through outcome takes the forward edge. Re-derived
        # from front-matter every run/resume (not persisted). Existence + routing soundness were
        # already checked at flow resolution, so the engine can skip these unconditionally.
        self._disabled_nodes = disabled_nodes
        # Per-node field overlay (``node_id -> {field: value}``) the task requested via
        # ``nodes.<id>.{model,reasoning,provider}``, already validated by the resolver
        # (``core.node_overrides``). The engine applies it mechanically to the fetched node before
        # the runner sees it — it never learns what a field means (same separation as
        # ``disabled_nodes``). Re-derived from front-matter every run/resume (not persisted).
        self._node_overrides = node_overrides
        # When set, the run is confined to this node-id set (a decomposition sub_flow region): it
        # ends when a *forward* edge leaves the region (the driver then runs the next subtask or the
        # post-region phase). Rework/fail edges always point back into the region, so they never
        # trigger the exit. ``None`` => run the whole graph to a terminal node.
        self._region = region

    @property
    def run_state(self) -> FlowRunState:
        return self._run_state

    def run(self) -> FlowRunResult:
        """Execute the graph until a terminal node or an exhausted budget.

        A fresh run starts at the entry node; a resumed run (a hydrated ``run_state`` whose
        ``current_node`` is already set) continues from that node. Already-completed side effects
        are deduplicated by ``publish_operations``, so a resumed run never repeats a commit/push/PR.
        """
        if self._run_state.current_node is None:
            self._run_state.current_node = self._entry_node_id()
        while True:
            assert self._run_state.current_node is not None
            if self._is_cancelled():
                # Persist even on a fresh direct engine run. After a completed node the transition
                # already saved this same next-node checkpoint, so this remains idempotent.
                self._recorder.save_checkpoint(self._run_state)
                raise FlowCancelled(self._run_state.current_node)
            node = self._apply_overrides(self._snapshot.nodes_by_id[self._run_state.current_node])
            outcome = self._execute_node(node)
            self._run_state.mark_completed(node.id)

            edges = self._snapshot.adjacency.get(node.id, ())
            if not edges:
                # No outgoing edge => terminal node => the flow is done.
                self._recorder.save_checkpoint(self._run_state)
                return FlowRunResult(status=Status.DONE, final_node=node.id)

            edge = self._select_edge(node, edges, outcome)
            if edge.outcome in REWORK_OUTCOMES:
                # EXPERIMENTAL(no-work-infra): abort a no-effective-work stall BEFORE charging
                # rework, so a frozen fix loop is not counted toward its fix budget (it is not real
                # work); else the budget cap. Drop the `_check_stall` line to disable the guard.
                stuck = self._check_stall(edge.loop) if edge.loop is not None else None
                if stuck is None:
                    stuck = self._charge_rework(edge)
                if stuck is not None:
                    report = self._recorder.write_failure_report(
                        node_id=node.id,
                        loop=stuck.loop,
                        limit_name=stuck.limit_name,
                        run_state=self._run_state,
                        subtask_order=self._subtask_order,
                    )
                    self._recorder.save_checkpoint(self._run_state)
                    return FlowRunResult(
                        status=Status.MANUAL_ACTION_REQUIRED,
                        final_node=node.id,
                        stuck_loop=stuck.loop,
                        limit_name=stuck.limit_name,
                        failure_report_path=report,
                    )
            else:
                self._reset_loops_at(node.id)

            self._run_state.current_node = edge.to
            self._recorder.save_checkpoint(self._run_state)
            if self._region is not None and edge.to not in self._region:
                # A forward edge left the region => this region run is complete. ``current_node`` is
                # the post-region node, so resume continues there; the driver runs the next phase.
                return FlowRunResult(status=Status.DONE, final_node=node.id)

    # -- node execution --------------------------------------------------------

    def _apply_overrides(self, node: FlowNode) -> FlowNode:
        """Overlay the task's per-node field overrides onto the fetched node, if any.

        Mechanical: the resolver guarantees every overlay key is a valid field of this node kind, so
        the engine just ``replace``s the fields (``provider``/``model``/``reasoning``) before the
        runner resolves the route and builds the request. ``id`` is never overridden, so skip /
        edge-selection / completion bookkeeping (all id-keyed) are unaffected."""
        fields = self._node_overrides.get(node.id)
        # The resolver guarantees every key is a real field of this node kind with a type-correct
        # value, but mypy's dataclass plugin can't verify keys unpacked from a generic mapping.
        return replace(node, **fields) if fields else node  # type: ignore[arg-type]

    def _execute_node(self, node: FlowNode) -> NodeOutcome:
        if self._should_skip(node):
            reason = self._skip_reason(node)
            self._recorder.record_skip(node, reason=reason, subtask_order=self._subtask_order)
            return self._skip_outcome(node)
        runner = self._runners.get(node.kind)
        if runner is None:
            raise EngineInternalError(f"no runner registered for node kind {node.kind!r}")
        ctx = NodeContext(
            snapshot=self._snapshot,
            run_state=self._run_state,
            node=node,
            task_id=self._task_id,
            subtask_order=self._subtask_order,
        )
        result = runner.run(node, ctx)
        if self._post_node is not None:
            # Core post-processing (output_artifact slot, decomposition contract) + the supervisor
            # layer's per-step observation run between the node completing and the next node — only
            # for executed nodes, never on a skip. The node_run_id keys the observation.
            self._post_node(node, result.outcome, result.node_run_id)
        return result.outcome

    def _should_skip(self, node: FlowNode) -> bool:
        if node.id in self._disabled_nodes:
            return True
        when = node.when
        return when is not None and self._facts(when.fact) != when.equals

    def _skip_reason(self, node: FlowNode) -> str:
        if node.id in self._disabled_nodes:
            return f"disabled by task: nodes.{node.id}.enabled=false"
        when = node.when
        fact = when.fact if when is not None else "?"
        return f"deterministic skip: when {fact} != {when.equals if when else True}"

    @staticmethod
    def _skip_outcome(node: FlowNode) -> NodeOutcome:
        """A skipped node yields its pass-through outcome so the engine takes the forward edge."""
        return skip_outcome(node)

    # -- edge resolution -------------------------------------------------------

    def _entry_node_id(self) -> str:
        return entry_node_id(self._snapshot)

    def _select_edge(self, node: FlowNode, edges: tuple[Edge, ...], outcome: NodeOutcome) -> Edge:
        kind = outcome.kind
        if kind.startswith("route:"):
            for edge in edges:
                if edge.outcome == kind:
                    return edge
            raise EngineInternalError(
                f"node {node.id!r}: route outcome {kind!r} matches no declared edge"
            )
        for edge in edges:
            if edge.outcome == kind:
                return edge
        if kind == "done":
            unconditional = [edge for edge in edges if edge.outcome is None]
            if len(unconditional) == 1:
                return unconditional[0]
        raise EngineInternalError(
            f"node {node.id!r}: outcome {kind!r} matches no declared edge "
            f"(declared: {sorted(repr(e.outcome) for e in edges)})"
        )

    # -- budget bookkeeping ----------------------------------------------------

    def _check_stall(self, loop: str) -> _Stuck | None:
        """EXPERIMENTAL(no-work-infra). No-effective-work guard: abort ``loop`` when unchanged
        across :data:`_STALL_NO_CHANGE_LIMIT` consecutive rework charges — the "agent emits tokens
        but never edits" stall the provider boundary cannot see (it produced output, just no edit).

        Reads an opaque fingerprint via the injected callable and only compares it for equality, so
        the engine stays domain-free. A cycle that changes the tree resets the streak. No callable
        (e.g. the merge flow / a test that omits it) leaves the guard inert.
        """
        if self._diff_fingerprint is None:
            return None
        fingerprint = self._diff_fingerprint()
        if self._stall_fp.get(loop) == fingerprint:
            self._stall_streak[loop] = self._stall_streak.get(loop, 0) + 1
        else:
            self._stall_streak[loop] = 0
            self._stall_fp[loop] = fingerprint
        if self._stall_streak[loop] >= _STALL_NO_CHANGE_LIMIT:
            return _Stuck(loop=loop, limit_name="no_file_change")
        return None

    def _charge_rework(self, edge: Edge) -> _Stuck | None:
        """Charge a rework/fail edge against the global counter plus its loop/inline budget.

        Returns a :class:`_Stuck` when a limit is reached (the edge must not be taken), else
        ``None``. The per-loop / inline limit is reported before the global cap when both trip on
        the same entry.

        The single global ``fix_iterations`` increment goes through
        :func:`~wastech_orchestrator.core.loop_control.record_rework` — the one accounting path
        shared by every in-flow rework/fail edge (``test_fix`` and ``review_fix`` alike), so a
        rework is never double-counted (anchored by ``test_record_rework_single_increment``).
        """
        glob = record_rework(self._run_state)
        if edge.loop is not None:
            cycles = self._run_state.bump(edge.loop)
            # Cumulative per-loop total for the audit trail: bumped on every rework of this
            # loop, never reset on a forward edge, so a converged loop is not attributed 0.
            self._run_state.bump(FlowRunState.total_key(edge.loop))
            if cycles >= self._loop_cap(edge.loop):
                return _Stuck(loop=edge.loop, limit_name="max_fix_cycles")
        elif edge.budget is not None:
            key = edge_key(edge)
            if self._run_state.counter(key) >= edge.budget:
                return _Stuck(loop=None, limit_name=f"budget:{edge.from_node}->{edge.to}")
            self._run_state.bump(key)
        if glob >= self._global_cap():
            return _Stuck(loop=edge.loop, limit_name="max_total_fix_iterations")
        return None

    def _reset_loops_at(self, node_id: str) -> None:
        """Leaving a node via a forward edge resolves any loop/inline budget anchored at it.

        A node whose rework/fail back-edge is satisfied resets that counter (the consecutive-cycle
        count starts over).
        """
        for edge in self._snapshot.adjacency.get(node_id, ()):
            if edge.outcome not in REWORK_OUTCOMES:
                continue
            if edge.loop is not None:
                self._run_state.reset(edge.loop)
                # EXPERIMENTAL(no-work-infra): the no-change stall streak/fingerprint are per-loop
                # and transient — clear them too so a re-entered loop starts its consecutive-cycle
                # count fresh.
                self._stall_streak.pop(edge.loop, None)
                self._stall_fp.pop(edge.loop, None)
            elif edge.budget is not None:
                self._run_state.reset(edge_key(edge))

    def _loop_cap(self, loop: str) -> int:
        return loop_cap(self._snapshot.doc.budgets, self._agents.max_fix_cycles, loop)

    def _global_cap(self) -> int:
        return global_cap(self._snapshot.doc.budgets, self._agents.max_total_fix_iterations)
