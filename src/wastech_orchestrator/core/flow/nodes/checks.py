"""Checks node runner (P1.3/P1.4/P3.1) — dispatch on the node's ``checker``.

A ``checks`` node names a ``checker``; this runner dispatches on it, and every checker maps to the
same engine outcome — ``pass`` / ``fail`` — so the engine needs no per-checker special case:

* ``command_profile`` (P1.3) — runs the resolved quality-gate commands through the CheckRunner
  (exit codes authoritative). Used by the implementation flow's ``testing`` node.
* ``citation`` (P3.1) — the deterministic, no-LLM citation-manifest validator: a hallucinated
  citation fails the check, gating the synthesis loop. Used by ``deep_research``.
* ``dependency_scan`` (P3.1) — the core-owned argv advisory scanners as evidence: it always emits
  ``pass`` (the scan ran); whether findings gate is the flow's decision (its edges). Used by
  ``security_audit``.

The flow never supplies commands / scanners (security-ceiling): the command profile is resolved
by the orchestrator and the scanner set is core-owned in
:mod:`~wastech_orchestrator.core.flow.checkers`.

**Mutation guard (P2.4).** The ``command_profile`` path snapshots the working tree before and after
the checks; if a *passing* check mutated commit-candidate files (e.g. an auto-formatter rewrote
sources), it fails closed to manual review — a green-but-dirtying check must not pass silently. This
is a core-owned property of the ``checks`` node and cannot be declared away or disabled by the flow.
The ``citation`` / ``dependency_scan`` checkers are read-only, so the guard does not apply to them.
The guard is a no-op when no snapshot hook is wired (a unit harness without git).
"""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.check_runner import CheckOutcome
from wastech_orchestrator.checks.model import ResolvedCheck
from wastech_orchestrator.core.flow.checkers.citation import CitationReport, validate_citations
from wastech_orchestrator.core.flow.checkers.dependency_scan import (
    DependencyScanReport,
    run_dependency_scan,
)
from wastech_orchestrator.core.flow.engine import NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInfraError,
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.output_policy import resolve_output_policy
from wastech_orchestrator.core.flow.schema import ChecksNode, FlowNode
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.routing.snapshots import WorkingTreeSnapshot
from wastech_orchestrator.state_store import CheckRunRow, NodeRunRow


class CheckLaunchError(NodeInfraError):
    """A configured check could not be *launched* (infra, never a quality ``fail``)."""


class ChecksNodeRunner:
    """Run a ``checks`` node — ``command_profile`` / ``citation`` / ``dependency_scan``."""

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
        if node.checker == "citation":
            return self._run_citation(node, ctx, run_id)
        if node.checker == "dependency_scan":
            return self._run_dependency_scan(node, ctx, run_id)
        return self._run_command_profile(node, ctx, run_id)

    # -- command_profile (P1.3/P2.4) ------------------------------------------

    def _run_command_profile(self, node: ChecksNode, ctx: NodeContext, run_id: int) -> NodeResult:
        before = self._capture()  # working-tree state before the checks can mutate anything
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
        if outcome.passed and self._mutated_working_tree(before):
            # Green-but-dirtying guard: a passing check that rewrote commit-candidate files must not
            # pass silently. Fail closed to manual review (mirrors the launch-failure record shape).
            self._s.store.complete_node_run(
                run_id, status="dirtied_working_tree", outcome=None, finished_at=self._s.clock()
            )
            raise NodeManualRequired(
                f"checks node {node.id!r}: a check mutated the working tree "
                "(commit-candidate files changed across the check run) — a green-but-dirtying "
                "check must not pass silently"
            )
        return self._complete(run_id, node, passed=outcome.passed)

    # -- citation (P3.1) ------------------------------------------------------

    def _run_citation(self, node: ChecksNode, ctx: NodeContext, run_id: int) -> NodeResult:
        """Validate the research ``sources.json`` manifest; a hallucinated citation → ``fail``."""
        checks_dir = self._checks_dir(ctx.task_id)
        resolved = resolve_output_policy(ctx.snapshot.doc.output_policy, ctx.task_id)
        report_dir = resolved.report_dir(self._s.repo_dir)
        # A missing manifest (no report dir, or sources.json absent) → uncheckable, never a crash.
        manifest = (report_dir or checks_dir) / "sources.json"
        report = validate_citations(self._s.repo_dir, manifest)
        artifact = checks_dir / "citation.json"
        artifact.write_text(_citation_json(report), encoding="utf-8")
        self._record_check_run(
            ctx,
            command="citation",
            exit_code=0 if report.passed else 1,
            timed_out=False,
            passed=report.passed,
            log_path=str(artifact),
        )
        self._register(ctx.task_id, "citation", str(artifact))
        return self._complete(run_id, node, passed=report.passed)

    # -- dependency_scan (P3.1) ----------------------------------------------

    def _run_dependency_scan(self, node: ChecksNode, ctx: NodeContext, run_id: int) -> NodeResult:
        """Run the core-owned advisory scanners as evidence; always ``pass`` (the scan ran)."""
        checks_dir = self._checks_dir(ctx.task_id)
        report = run_dependency_scan(
            repo_dir=self._s.repo_dir,
            logs_dir=checks_dir / "dependency_scan",
            env=self._s.process_env,
            timeout_seconds=self._s.scan_timeout_s,
            run_process=self._s.run_process,
        )
        artifact = checks_dir / "dependency_scan.json"
        artifact.write_text(_dependency_scan_json(report), encoding="utf-8")
        for scan in report.runs:
            self._record_check_run(
                ctx,
                command=scan.command,
                exit_code=scan.exit_code,
                timed_out=scan.timed_out,
                # "passed" here means the scanner produced evidence (launched, did not time out);
                # the node outcome is unconditionally ``pass`` (gating is the flow's edges' call).
                passed=scan.launched and not scan.timed_out,
                log_path=scan.report_path,
            )
        self._register(ctx.task_id, "dependency_scan", str(artifact))
        return self._complete(run_id, node, passed=report.passed)

    # -- shared helpers -------------------------------------------------------

    def _complete(self, run_id: int, node: ChecksNode, *, passed: bool) -> NodeResult:
        result_kind = "pass" if passed else "fail"
        self._s.store.complete_node_run(
            run_id,
            status="passed" if passed else "failed",
            outcome=result_kind,
            finished_at=self._s.clock(),
        )
        return NodeResult(node_id=node.id, outcome=NodeOutcome(result_kind), node_run_id=run_id)

    def _checks_dir(self, task_id: str) -> Path:
        checks_dir = Path(task_artifact_dir(self._s.artifacts_root, task_id)) / "checks"
        checks_dir.mkdir(parents=True, exist_ok=True)
        return checks_dir

    def _record_check_run(
        self,
        ctx: NodeContext,
        *,
        command: str,
        exit_code: int | None,
        timed_out: bool,
        passed: bool,
        log_path: str,
    ) -> None:
        self._s.store.record_check_run(
            CheckRunRow(
                task_id=ctx.task_id,
                subtask_order=ctx.subtask_order,
                command=command,
                exit_code=exit_code,
                timed_out=timed_out,
                passed=passed,
                log_path=log_path,
                started_at=self._s.clock(),
                finished_at=self._s.clock(),
            )
        )

    def _register(self, task_id: str, kind: str, path: str) -> None:
        if self._s.register_artifact is not None:
            self._s.register_artifact(task_id, kind, path)

    def _capture(self) -> WorkingTreeSnapshot | None:
        """Snapshot the working tree before the checks run, or ``None`` when no hook is wired."""
        return self._s.snapshot.capture() if self._s.snapshot is not None else None

    def _mutated_working_tree(self, before: WorkingTreeSnapshot | None) -> bool:
        """Whether the checks changed the working tree since ``before`` (commit-candidate mutation).

        The :class:`WorkingTreeSnapshot` checksum covers ``git status`` + ``git diff HEAD``, so any
        edit a check made to tracked or untracked files trips it. A no-op (no hook / no before
        capture) reports ``False`` — the guard is inert when git is not wired.
        """
        if before is None or self._s.snapshot is None:
            return False
        return self._s.snapshot.capture().diff_checksum != before.diff_checksum

    def _run_checks(
        self, ctx: NodeContext, checks: tuple[ResolvedCheck, ...] | None
    ) -> CheckOutcome:
        """Run the check profile and record one ``check_runs`` row per command."""
        outcome = self._s.check_runner.run(
            clone_dir=self._s.repo_dir,
            artifacts_root=self._s.artifacts_root,
            task_id=ctx.task_id,
            subtask=ctx.subtask_order,
            checks=checks,
        )
        for run in outcome.runs:
            self._record_check_run(
                ctx,
                command=run.command,
                exit_code=run.exit_code,
                timed_out=run.timed_out,
                passed=run.passed,
                log_path=run.log_path,
            )
        return outcome


def _citation_json(report: CitationReport) -> str:
    return (
        json.dumps(
            {
                "manifest_status": report.manifest_status,
                "passed": report.passed,
                "entries": [
                    {"source_id": e.source_id, "status": e.status.value, "reason": e.reason}
                    for e in report.entries
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def _dependency_scan_json(report: DependencyScanReport) -> str:
    return (
        json.dumps(
            {
                "passed": report.passed,
                "scanners": [
                    {
                        "name": r.name,
                        "command": r.command,
                        "launched": r.launched,
                        "exit_code": r.exit_code,
                        "timed_out": r.timed_out,
                        "report_path": r.report_path,
                    }
                    for r in report.runs
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
