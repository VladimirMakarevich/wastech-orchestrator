"""Publish node runner (P1.3/P1.4) — thin adapter to the GitManager.

Publishing is the orchestrator's sole responsibility (the hard invariant: providers and flows never
touch git). The runner maps the flow's ``PublishingPolicy`` to the idempotent git operations and
returns an unconditional ``done`` outcome.

In P1 only ``pull_request`` / ``documentation_pull_request`` are wired (the implementation parity
path): commit code, commit the audit trail, push the branch, open the PR — each idempotent via
``publish_operations``. ``none`` / ``local_artifact`` / ``private_control_workspace_report`` write
no git (the deliverable is the in-workspace artifact); their publishing specifics land in P3. Task
finalize (moving the task file before the audit commit) and auto-merge stay orchestrator-level and
are applied by the P1.4 wrapper around the engine.
"""

from __future__ import annotations

from wastech_orchestrator.core.flow.contracts import PublishingPolicy
from wastech_orchestrator.core.flow.engine import NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import NodeInputs, NodeServices
from wastech_orchestrator.core.flow.schema import FlowNode, PublishNode
from wastech_orchestrator.state_store import NodeRunRow

_PR_POLICIES = frozenset(
    {PublishingPolicy.PULL_REQUEST, PublishingPolicy.DOCUMENTATION_PULL_REQUEST}
)


class PublishConfigError(Exception):
    """The publish node needs git/branch inputs the unit did not provide."""


class PublishNodeRunner:
    """Run a ``publish`` node through the GitManager (constructed per unit)."""

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
        self._in = inputs

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, PublishNode)
        run_id = self._s.store.record_node_run(
            NodeRunRow(
                task_id=ctx.task_id,
                node_id=node.id,
                node_kind="publish",
                subtask_order=ctx.subtask_order,
                status="running",
                started_at=self._s.clock(),
            )
        )
        result_ref = self._publish(node, ctx)
        self._s.store.complete_node_run(
            run_id,
            status="published",
            outcome="done",
            finished_at=self._s.clock(),
            commit_sha_after=result_ref,
        )
        return NodeResult(node_id=node.id, outcome=NodeOutcome("done"), node_run_id=run_id)

    def _publish(self, node: PublishNode, ctx: NodeContext) -> str | None:
        if node.policy not in _PR_POLICIES:
            # none / local_artifact / private report: the deliverable is the in-workspace artifact;
            # no git publishing in P1 (P3 wires private storage / local artifact handling).
            return None
        git = self._s.git
        if git is None or self._in.branch is None:
            raise PublishConfigError(
                f"publish node {node.id!r} ({node.policy.value}) requires a GitPort + branch"
            )
        # Finalize (move the task file + write the committed summary) BEFORE the audit commit so
        # both land in it (legacy ordering); the committed summary.md is the PR body, falling back
        # to the logs/ summary slot.
        committed_summary = self._s.finalize() if self._s.finalize is not None else None
        body_path = committed_summary or self._in.summary_body_path
        if body_path is None:
            raise PublishConfigError(
                f"publish node {node.id!r} ({node.policy.value}) has no PR body: wire a finalize "
                "hook or set summary_body_path (refusing to open a PR with an empty body)"
            )
        message = self._in.commit_message or f"feat({ctx.task_id}): publish"
        git.commit_code(ctx.task_id, message)
        git.commit_audit(ctx.task_id)
        git.push(ctx.task_id, self._in.branch)
        return git.create_pr(
            ctx.task_id,
            self._in.branch,
            title=self._in.pr_title or ctx.task_id,
            body_path=body_path,
        )
