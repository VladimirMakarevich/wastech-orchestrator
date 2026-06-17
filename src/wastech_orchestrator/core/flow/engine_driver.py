"""Engine driver — assembles the per-unit node runners and runs the engine (P1.4 step A).

This is the seam between the generic :class:`~wastech_orchestrator.core.flow.engine.FlowEngine` and
the core-owned node runners: :func:`build_node_runners` constructs the per-kind runner registry from
a unit's :class:`~wastech_orchestrator.core.flow.nodes.base.NodeServices` /
:class:`~wastech_orchestrator.core.flow.nodes.base.NodeInputs`, and :func:`drive_flow` wires that
registry into a :class:`FlowEngine` (with the persistence recorder) and runs one unit to a terminal
:class:`~wastech_orchestrator.core.flow.engine.FlowRunResult`.

The orchestrator's ``run_task``/``resume`` wrapper (P1.4 step B) builds the ``NodeServices`` /
``NodeInputs`` from its live ``_Pipeline``, resolves the flow snapshot via the ``FlowRegistry``, and
calls :func:`drive_flow` — initially behind a dormant seam dual-run against the legacy ``_drive``
by the golden harness, then as the sole driver once P1.5 removes the legacy path.

The ``hitl`` runner and the agent-node embedded-HITL / dangerous-diff guard are added with the
golden harness in step B (their durable round-trip is only meaningfully verified end-to-end).
"""

from __future__ import annotations

from wastech_orchestrator.config.schema import AgentsConfig
from wastech_orchestrator.core.flow.engine import (
    FactResolver,
    FlowEngine,
    FlowRunResult,
    NodeRunner,
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
) -> FlowRunResult:
    """Run one unit through the flow engine with the core-owned node runners."""
    engine = FlowEngine(
        snapshot,
        run_state,
        build_node_runners(services, inputs),
        recorder,
        facts=facts,
        agents=agents,
        task_id=task_id,
        subtask_order=subtask_order,
    )
    return engine.run()
