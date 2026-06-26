"""Standalone HITL gate node runner (P1.4 step B).

A ``hitl`` node is a bare human gate with no agent stage and no prompt: it pauses the flow for one
durable human round-trip via :class:`~.human_gate.HumanGate` (the same primitive behind the embedded
refinement/planning HITL and the dangerous-diff guard), keyed by the node id — not a ``Stage``.

* ``signal: approval`` branches on the decision — ``route:approve`` / ``route:deny`` (the flow
  declares those edges; the validator allows ``route:*`` on any node).
* ``signal: question`` proceeds unconditionally (``done``) once any non-empty answer arrives.

Fail-closed: a missing notifier, timeout, transport error, or invalid response raises
:class:`~.base.NodeManualRequired` (terminal ``manual_action_required``). A restarted process
resumes the persisted ``waiting`` interaction against its original deadline (like the agent HITL).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wastech_orchestrator.core.flow.engine import NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.nodes.human_gate import HumanGate
from wastech_orchestrator.core.flow.schema import FlowNode, HitlNode
from wastech_orchestrator.core.hitl import (
    HumanInputSignal,
    load_interaction,
    mark_consumed,
    node_interaction_path,
)
from wastech_orchestrator.notify import AskResult
from wastech_orchestrator.state_store import NodeRunRow


class HitlNodeRunner:
    """Run a standalone ``hitl`` gate node through the durable HumanGate (constructed per unit)."""

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
        self._in = inputs

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, HitlNode)
        run_id = self._s.store.record_node_run(
            NodeRunRow(
                task_id=ctx.task_id,
                node_id=node.id,
                node_kind="hitl",
                subtask_order=ctx.subtask_order,
                status="running",
                started_at=self._s.clock(),
            )
        )
        try:
            result = self._obtain(node, ctx)
        except NodeManualRequired:
            self._s.store.complete_node_run(
                run_id, status="failed", outcome=None, finished_at=self._s.clock()
            )
            raise
        outcome = self._outcome(node, result)
        self._s.store.complete_node_run(
            run_id, status="passed", outcome=outcome.kind, finished_at=self._s.clock()
        )
        return NodeResult(node_id=node.id, outcome=outcome, node_run_id=run_id)

    def _obtain(self, node: HitlNode, ctx: NodeContext) -> AskResult:
        """One durable round-trip (or resume a persisted one), validated fail-closed."""
        path = node_interaction_path(
            self._s.artifacts_root, ctx.task_id, node.id, subtask=ctx.subtask_order
        )
        gate = self._gate(node)
        persisted = load_interaction(path)
        if persisted is None:
            result = gate.request(
                task_id=ctx.task_id,
                node_id=node.id,
                subtask=ctx.subtask_order,
                signal=_signal(node),
                path=path,
            )
        else:
            status = str(persisted.get("status", ""))
            if status == "waiting":
                result = gate.resume(path, dict(persisted))
            elif status in ("answered", "consumed"):
                result = _result_from_persisted(persisted)
            else:
                raise NodeManualRequired(
                    f"hitl node {node.id!r}: cannot resume from status {status!r}"
                )
        self._require_answer(node, result)
        mark_consumed(path)
        return result

    def _gate(self, node: HitlNode) -> HumanGate:
        if self._s.notifier is None:
            raise NodeManualRequired(f"hitl node {node.id!r}: no notifier transport is configured")
        return HumanGate(
            self._s.notifier,
            timeout_s=node.timeout_s or self._s.ask_timeout_s,
            contacts=self._in.contacts,
            heartbeat_seconds=self._s.ask_heartbeat_seconds,
        )

    def _require_answer(self, node: HitlNode, result: AskResult) -> None:
        if result.failure is not None or not result.answered:
            raise NodeManualRequired(
                f"hitl node {node.id!r}: human input failed ({result.failure or 'no_answer'})"
            )
        if node.signal == "approval" and not isinstance(result.approved, bool):
            raise NodeManualRequired(f"hitl node {node.id!r}: approval returned no decision")
        if node.signal == "question" and not (isinstance(result.text, str) and result.text.strip()):
            raise NodeManualRequired(f"hitl node {node.id!r}: question returned no answer")

    @staticmethod
    def _outcome(node: HitlNode, result: AskResult) -> NodeOutcome:
        if node.signal == "approval":
            return NodeOutcome("route:approve" if result.approved else "route:deny")
        return NodeOutcome("done")


def _signal(node: HitlNode) -> HumanInputSignal:
    """A bare gate signal — the node carries no question text, so synthesize a generic prompt."""
    if node.signal == "approval":
        return HumanInputSignal(
            kind="approval",
            question="Approval required to continue the flow.",
            context=f"hitl gate {node.id!r}",
            risk="other",
            paths=(),
        )
    return HumanInputSignal(
        kind="question",
        question="Input required to continue the flow.",
        context=f"hitl gate {node.id!r}",
        risk="clarification",
        paths=(),
    )


def _result_from_persisted(persisted: Mapping[str, Any]) -> AskResult:
    """Rebuild the answer of an already-answered interaction (resume after a restart)."""
    failure = persisted.get("failure")
    return AskResult(
        answered=failure is None,
        text=persisted.get("answer"),
        approved=persisted.get("approved"),
        failure=failure,
    )
