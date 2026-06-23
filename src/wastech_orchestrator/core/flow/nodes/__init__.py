"""Core-owned node-kind runners for the flow engine (P1.3).

Each node kind is a thin adapter from a :class:`~wastech_orchestrator.core.flow.engine.NodeRunner`
to an existing core capability, so executing a node yields the same result as the direct call:

* :class:`~.agent.AgentNodeRunner` -> :class:`~wastech_orchestrator.routing.router.AgentRouter`
* :class:`~.evaluator.EvaluatorNodeRunner` -> the router + structured-verdict parsing (the minimal
  ``role=review`` evaluator; supervisor/critic/verifier are P2)
* :class:`~.checks.ChecksNodeRunner` -> :class:`~wastech_orchestrator.check_runner.CheckRunner`

The ``publish`` runner (git publishing), the agent-node embedded HITL round-trip + dangerous-diff
guard, and the standalone :class:`~.hitl.HitlNodeRunner` gate are implemented here too.

A runner is constructed per execution unit with its collaborators (:class:`~.base.NodeServices`)
and the unit's inputs (:class:`~.base.NodeInputs`) baked in, so the generic engine stays free of any
per-run context.
"""

from wastech_orchestrator.core.flow.nodes.agent import AgentNodeRunner
from wastech_orchestrator.core.flow.nodes.base import NodeInputs, NodeServices
from wastech_orchestrator.core.flow.nodes.checks import ChecksNodeRunner
from wastech_orchestrator.core.flow.nodes.evaluator import EvaluatorNodeRunner
from wastech_orchestrator.core.flow.nodes.hitl import HitlNodeRunner
from wastech_orchestrator.core.flow.nodes.publish import PublishConfigError, PublishNodeRunner

__all__ = [
    "AgentNodeRunner",
    "ChecksNodeRunner",
    "EvaluatorNodeRunner",
    "HitlNodeRunner",
    "NodeInputs",
    "NodeServices",
    "PublishConfigError",
    "PublishNodeRunner",
]
