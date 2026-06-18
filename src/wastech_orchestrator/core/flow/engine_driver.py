"""Engine driver — assembles the per-unit node runners and runs the engine (P1.4 step A).

This is the seam between the generic :class:`~wastech_orchestrator.core.flow.engine.FlowEngine` and
the core-owned node runners: :func:`build_node_runners` constructs the per-kind runner registry from
a unit's :class:`~wastech_orchestrator.core.flow.nodes.base.NodeServices` /
:class:`~wastech_orchestrator.core.flow.nodes.base.NodeInputs`, and :func:`drive_flow` wires that
registry into a :class:`FlowEngine` (with the persistence recorder) and runs one unit to a terminal
:class:`~wastech_orchestrator.core.flow.engine.FlowRunResult`.

The orchestrator's ``run_task``/``resume`` wrapper (the cutover step) builds the ``NodeServices`` /
``NodeInputs`` from its live ``_Pipeline``, resolves the flow snapshot via the ``FlowRegistry``, and
calls :func:`drive_flow` as **the** driver — replacing the legacy ``_drive``, which is deleted in
the same cutover step (no dual-run: greenfield, the goal is to preserve every capability, not to
match the old driver byte-for-byte — see ``docs/backlog/flows/p1-step-b-wiring-draft.md``).
"""

from __future__ import annotations

from dataclasses import dataclass

from wastech_orchestrator.config.schema import AgentsConfig
from wastech_orchestrator.core.flow.engine import (
    _REWORK_OUTCOMES,
    FactResolver,
    FlowEngine,
    FlowRunResult,
    NodeRunner,
    PostNodeHook,
    RunRecorder,
)
from wastech_orchestrator.core.flow.nodes import (
    AgentNodeRunner,
    ChecksNodeRunner,
    EvaluatorNodeRunner,
    NodeInputs,
    NodeServices,
    PublishNodeRunner,
)
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot


@dataclass(frozen=True)
class DecompositionRegions:
    """The three phases a decomposed flow is driven in (P1.4 slice 5).

    The graph is one connected flow; decomposition carves it into a ``pre`` prefix that runs once
    (entry…proposed_by), a ``region`` (the ``sub_flow``) that runs once per subtask, and a ``post``
    suffix that runs once after all subtasks. ``region_entry`` is the node ``pre`` transitions into;
    ``post_entry`` is the node the region's forward exit edge points to.
    """

    pre: frozenset[str]
    region: frozenset[str]
    region_entry: str
    post_entry: str


def partition_decomposition(snapshot: FlowSnapshot) -> DecompositionRegions:
    """Partition a flow with a ``decomposition`` block into its pre / region / post phases."""
    decomp = snapshot.doc.decomposition
    if decomp is None:
        raise ValueError("flow has no decomposition block to partition")
    region = frozenset(decomp.sub_flow)
    region_entry = next(
        e.to for e in snapshot.adjacency.get(decomp.proposed_by, ()) if e.to in region
    )
    # The region exit is a forward (non-rework) edge from a region node to a non-region node;
    # rework/fail edges always point back into the region, so they are never the exit.
    post_entry = next(
        e.to
        for node_id in region
        for e in snapshot.adjacency.get(node_id, ())
        if e.to not in region and e.outcome not in _REWORK_OUTCOMES
    )
    post = _reachable(snapshot, post_entry)
    pre = frozenset(snapshot.nodes_by_id) - region - post
    return DecompositionRegions(
        pre=pre, region=region, region_entry=region_entry, post_entry=post_entry
    )


def _reachable(snapshot: FlowSnapshot, start: str) -> frozenset[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        stack.extend(e.to for e in snapshot.adjacency.get(node_id, ()))
    return frozenset(seen)


def build_node_runners(services: NodeServices, inputs: NodeInputs) -> dict[str, NodeRunner]:
    """Construct the per-kind node-runner registry for one execution unit."""
    return {
        "agent": AgentNodeRunner(services, inputs),
        "evaluator": EvaluatorNodeRunner(services, inputs),
        "checks": ChecksNodeRunner(services, inputs),
        "publish": PublishNodeRunner(services, inputs),
    }


def drive_flow(
    *,
    snapshot: FlowSnapshot,
    run_state: FlowRunState,
    recorder: RunRecorder,
    services: NodeServices,
    inputs: NodeInputs,
    facts: FactResolver,
    agents: AgentsConfig,
    task_id: str,
    subtask_order: int | None = None,
    post_node: PostNodeHook | None = None,
    region: frozenset[str] | None = None,
) -> FlowRunResult:
    """Run one unit through the flow engine with the core-owned node runners.

    ``region`` confines the run to a decomposition sub_flow (it ends at the forward edge leaving the
    region); ``subtask_order`` scopes the node_runs to that subtask.
    """
    engine = FlowEngine(
        snapshot,
        run_state,
        build_node_runners(services, inputs),
        recorder,
        facts=facts,
        agents=agents,
        task_id=task_id,
        subtask_order=subtask_order,
        post_node=post_node,
        region=region,
    )
    return engine.run()
