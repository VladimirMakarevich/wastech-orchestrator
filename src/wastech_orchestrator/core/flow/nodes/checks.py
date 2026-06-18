"""Checks node runner (P1.3/P1.4) — thin adapter to the CheckRunner.

Runs the resolved checks, records each ``check_runs`` row, and maps the aggregate result to the
engine outcome: ``passed`` -> ``pass``, otherwise -> ``fail``. A check *launch* failure is an
infrastructure event (a missing executable, not a quality failure): the runner re-resolves the
check command set once via ``services.check_reresolve`` (the gated ``_reresolve_on_launch_failure``
port) and retries; if it still cannot launch it raises :class:`CheckLaunchError`, which never
becomes a ``fail`` outcome. Check exit codes are authoritative (``CheckOutcome.passed``).
"""

from __future__ import annotations

from wastech_orchestrator.check_runner import CheckOutcome
from wastech_orchestrator.checks.model import ResolvedCheck
from wastech_orchestrator.core.flow.engine import NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import NodeInfraError, NodeInputs, NodeServices
from wastech_orchestrator.core.flow.schema import ChecksNode, FlowNode
from wastech_orchestrator.state_store import CheckRunRow, NodeRunRow


class CheckLaunchError(NodeInfraError):
    """A configured check could not be *launched* (infra, never a quality ``fail``)."""


class ChecksNodeRunner:
    """Run a ``checks`` node through the CheckRunner (constructed per unit)."""

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
        self._in = inputs

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, ChecksNode)
        run_id = self._s.store.record_node_run(
            NodeRunRow(
                task_id=ctx.task_id,
                node_id=node.id,
                node_kind="checks",
                subtask_order=ctx.subtask_order,
                status="running",
                started_at=self._s.clock(),
            )
        )
        outcome = self._run_checks(ctx, self._in.resolved_checks)
        if outcome.launch_failed and self._s.check_reresolve is not None:
            # Infra launch failure: re-resolve the command set once (gated) and retry the node.
            new_checks = self._s.check_reresolve()
            if new_checks is not None:
                self._in.resolved_checks = new_checks
                outcome = self._run_checks(ctx, new_checks)
        if outcome.launch_failed:
            self._s.store.complete_node_run(
                run_id, status="launch_failed", outcome=None, finished_at=self._s.clock()
            )
            raise CheckLaunchError(
                outcome.first_launch_error or "a configured check could not be launched"
            )
        result_kind = "pass" if outcome.passed else "fail"
        self._s.store.complete_node_run(
            run_id,
            status="passed" if outcome.passed else "failed",
            outcome=result_kind,
            finished_at=self._s.clock(),
        )
        return NodeResult(node_id=node.id, outcome=NodeOutcome(result_kind), node_run_id=run_id)

    def _run_checks(self, ctx: NodeContext, checks: tuple[ResolvedCheck, ...]) -> CheckOutcome:
        """Run the check profile and record one ``check_runs`` row per command."""
        outcome = self._s.check_runner.run(
            clone_dir=self._s.repo_dir,
            artifacts_root=self._s.artifacts_root,
            task_id=ctx.task_id,
            subtask=ctx.subtask_order,
            checks=checks,
        )
        for run in outcome.runs:
            self._s.store.record_check_run(
                CheckRunRow(
                    task_id=ctx.task_id,
                    subtask_order=ctx.subtask_order,
                    command=run.command,
                    exit_code=run.exit_code,
                    timed_out=run.timed_out,
                    passed=run.passed,
                    log_path=run.log_path,
                    started_at=self._s.clock(),
                    finished_at=self._s.clock(),
                )
            )
        return outcome
