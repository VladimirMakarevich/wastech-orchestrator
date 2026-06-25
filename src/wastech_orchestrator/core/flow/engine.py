"""Flow execution engine — graph driver with engine-owned transitions (P1.1).

:class:`FlowEngine` executes a validated :class:`~.snapshot.FlowSnapshot`: starting at the entry
node it runs each node through its :class:`NodeRunner`, takes the node's :class:`NodeOutcome`,
resolves the matching outgoing edge from the snapshot adjacency, and transitions. **Only the engine
moves execution** — a ``NodeRunner`` returns an outcome but never picks the next node and never
changes task status. This is the single execution model — it replaced the hardcoded
dispatch-on-``Status`` pipeline loop the orchestrator used before the flow engine.

Guarantees:

* **Outcome ⊆ declared edges.** The chosen outcome must match a declared outgoing edge; a mismatch
  is an :class:`EngineInternalError` (the fatal validator, P0.3, already rejects malformed graphs at
  load, so this is a runtime assertion against a buggy runner).
* **Bounded termination.** Every ``rework``/``fail`` edge is charged against a single global fix
  counter plus its named loop or inline budget; exhausting any limit ends the run at
  ``MANUAL_ACTION_REQUIRED`` with a failure report. Reaching a node with no outgoing edges ends the
  run at ``DONE``.

Budget bookkeeping is generic on purpose: the engine knows nothing about ``test_fix`` /
``review_fix`` / supervisor by name (the P3 abstraction test forbids domain knowledge in the
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
from dataclasses import dataclass
from typing import Literal, Protocol

from wastech_orchestrator.config.schema import AgentsConfig
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import (
    ChecksNode,
    Edge,
    EvaluatorNode,
    FlowNode,
)
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.loop_control import record_rework
from wastech_orchestrator.core.state_machine import Status

#: Resolves a ``when.fact`` (``derived.*`` / ``config.*``) to a boolean. Injected so the engine
#: carries no knowledge of where facts come from (P1.3/P1.4 wire the real resolver).
FactResolver = Callable[[str], bool]


class EngineInternalError(Exception):
    """A runtime invariant the validator should have prevented was broken (a bug, not bad YAML)."""


@dataclass(frozen=True, slots=True)
class Finding:
    """A single evaluator finding (the shared evaluator primitive, P2.1).

    ``severity`` drives blocking: a ``high`` finding blocks (an evaluator verdict of ``rework``
    requires at least one); ``medium``/``low`` findings are advisory only. ``paths`` are the
    files/locations the finding concerns. Carried on :class:`NodeOutcome` for the audit trail (the
    immutable ``evaluations`` row) — the engine never inspects it to route.
    """

    severity: Literal["low", "medium", "high"]
    reason: str
    paths: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        """A finding blocks (drives ``rework``) iff ``high``; ``medium``/``low`` are advisory."""
        return self.severity == "high"


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """What a node returned to the engine. Never names the next node directly.

    ``kind`` is the edge-selecting outcome: ``"accept"`` / ``"rework"`` (evaluator stage_output),
    ``"pass"`` / ``"fail"`` (checks), ``"done"`` (an unconditional node took its single edge), or
    an explicit ``"route:<label>"``. ``structured_output`` / ``final_message`` carry the agent
    output so the post-node hook can persist a declared ``output_artifact`` slot and read the
    decomposition contract — the engine itself never inspects them.
    """

    kind: str
    findings: tuple[Finding, ...] = ()
    structured_output: Mapping[str, object] | None = None
    final_message: str | None = None


def skip_outcome(node: FlowNode) -> NodeOutcome:
    """The pass-through outcome a skipped node yields, so the engine takes its forward edge.

    The single source of truth for the skip-outcome-per-node-kind rule, shared by the engine's
    skip path and the flow validator's per-task disabled-node routing-soundness check.
    """
    if isinstance(node, EvaluatorNode):
        return NodeOutcome("accept")
    if isinstance(node, ChecksNode):
        return NodeOutcome("pass")
    return NodeOutcome("done")


@dataclass(frozen=True, slots=True)
class NodeResult:
    """A node's execution result: its outcome plus the ``node_runs`` row id it recorded (P1.2)."""

    node_id: str
    outcome: NodeOutcome
    node_run_id: int


@dataclass(frozen=True, slots=True)
class NodeContext:
    """Read-only context handed to a :class:`NodeRunner`. P1.3 enriches with artifact paths."""

    snapshot: FlowSnapshot
    run_state: FlowRunState
    node: FlowNode
    task_id: str
    subtask_order: int | None = None


class NodeRunner(Protocol):
    """Implemented by each node kind in ``core/flow/nodes/*.py`` (P1.3). Returns an outcome; it
    never transitions the graph or the task status — that is the engine's sole responsibility."""

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult: ...


class RunRecorder(Protocol):
    """Engine-level persistence seam. P1.1 uses an in-memory fake; P1.2 backs it by the state store
    (``record_skip`` / checkpoint of ``current_node`` + ``loop_counters`` / failure report)."""

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


_REWORK_OUTCOMES: frozenset[str] = frozenset({"rework", "fail"})
_LARGE = 1 << 60  # absent flow budget => only the config cap clamps


def edge_key(edge: Edge) -> str:
    """Synthetic ``loop_counters`` key for an inline ``budget`` edge (no named loop)."""
    return f"{edge.from_node}->{edge.to}:{edge.outcome}"


#: Called after each *executed* (non-skipped) node with ``(node, outcome, node_run_id)`` so the core
#: can persist a declared ``output_artifact`` slot / read the decomposition contract and let the
#: orchestrator's supervisor layer observe the completed step (keyed by its ``node_run_id``) before
#: the next node runs. Injected by the driver (P1.4); the engine carries no post-processing
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
        subtask_order: int | None = None,
        post_node: PostNodeHook | None = None,
        region: frozenset[str] | None = None,
        disabled_nodes: frozenset[str] = frozenset(),
    ) -> None:
        self._snapshot = snapshot
        self._run_state = run_state
        self._runners = runners
        self._recorder = recorder
        self._facts = facts
        self._agents = agents
        self._task_id = task_id
        self._subtask_order = subtask_order
        self._post_node = post_node
        # Flow node ids the task disabled (``nodes.<id>.enabled: false``); each is skipped exactly
        # like a ``when``-false node — its pass-through outcome takes the forward edge. Re-derived
        # from front-matter every run/resume (not persisted). Existence + routing soundness were
        # already checked at flow resolution, so the engine can skip these unconditionally.
        self._disabled_nodes = disabled_nodes
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
            node = self._snapshot.nodes_by_id[self._run_state.current_node]
            outcome = self._execute_node(node)
            self._run_state.mark_completed(node.id)

            edges = self._snapshot.adjacency.get(node.id, ())
            if not edges:
                # No outgoing edge => terminal node => the flow is done.
                self._recorder.save_checkpoint(self._run_state)
                return FlowRunResult(status=Status.DONE, final_node=node.id)

            edge = self._select_edge(node, edges, outcome)
            if edge.outcome in _REWORK_OUTCOMES:
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
            if edge.outcome not in _REWORK_OUTCOMES:
                continue
            if edge.loop is not None:
                self._run_state.reset(edge.loop)
            elif edge.budget is not None:
                self._run_state.reset(edge_key(edge))

    def _loop_cap(self, loop: str) -> int:
        return min(self._snapshot.doc.budgets.get(loop, _LARGE), self._agents.max_fix_cycles)

    def _global_cap(self) -> int:
        flow_cap = self._snapshot.doc.budgets.get(FlowRunState.GLOBAL_FIX_KEY, _LARGE)
        return min(flow_cap, self._agents.max_total_fix_iterations)
