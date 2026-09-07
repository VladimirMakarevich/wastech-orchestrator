"""Checks node runner — dispatch on the node's ``checker``.

A ``checks`` node names a ``checker``; this runner dispatches on it, and every checker maps to the
same engine outcome — ``pass`` / ``fail`` — so the engine needs no per-checker special case:

* ``command_profile`` — runs the resolved quality-gate commands through the CheckRunner
  (exit codes authoritative). Used by the implementation flow's ``testing`` node. Its verdict
  reaches the next node as ``{checks_path}`` on **both** outcomes: the first failing log on a
  failure (what ``fixing`` acts on), a per-command summary on a pass (what an evaluator judging
  "the checks pass" needs, so it does not re-run the gate inside the agent sandbox instead).
* ``citation`` — the deterministic, no-LLM citation-manifest validator: a hallucinated
  citation fails the check, gating the synthesis loop. Used by ``deep_research``.
* ``dependency_scan`` — the core-owned argv advisory scanners as evidence: it always emits
  ``pass`` (the scan ran); whether findings gate is the flow's decision (its edges). Used by
  ``security_audit``.

The flow never supplies commands / scanners: the command profile is resolved
by the orchestrator and the scanner set is core-owned in
:mod:`~wastech_orchestrator.core.flow.checkers`.

**Mutation guard.** The ``command_profile`` path snapshots the working tree before and after
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
from wastech_orchestrator.checks.model import ResolvedCheckSet
from wastech_orchestrator.checks.selection import select_check_sets
from wastech_orchestrator.core.flow.checkers.citation import CitationReport, validate_citations
from wastech_orchestrator.core.flow.checkers.dependency_scan import (
    DependencyScanReport,
    run_dependency_scan,
)
from wastech_orchestrator.core.flow.engine import NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.nodes.exchange_publish import publish_file
from wastech_orchestrator.core.flow.output_policy import resolve_output_policy
from wastech_orchestrator.core.flow.schema import ChecksNode, FlowNode
from wastech_orchestrator.providers.artifacts import node_run_dir, task_artifact_dir
from wastech_orchestrator.routing.snapshots import WorkingTreeSnapshot
from wastech_orchestrator.state_store import CheckRunRow, NodeRunRow


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

    # -- command_profile ------------------------------------------------------

    def _run_command_profile(self, node: ChecksNode, ctx: NodeContext, run_id: int) -> NodeResult:
        before = self._capture()  # working-tree state before the checks can mutate anything
        # Diff-select which command sets to run (Р3) from the change vs base — committed (e.g. an
        # already-committed decomposed subtask) *and* uncommitted, not just the working tree, so a
        # subtask whose code is already committed is still checked. ``None`` = git not wired (unit
        # harness) → run all; ``[]`` = empty diff (nothing changed) → run nothing (vacuous pass).
        changed = self._s.git.changed_code_paths_since_base() if self._s.git is not None else None
        selected = select_check_sets(self._in.check_sets, changed)
        if not selected:
            # Empty diff (nothing changed) or no command_sets configured → nothing to check → pass.
            return self._complete(run_id, node, passed=True)
        outcome = self._run_checks(ctx, selected)
        if outcome.any_launch_failed or outcome.nothing_ran:
            # Incomplete gate: a required toolchain was absent, or every selected check was
            # skipped — changed code went unchecked. Fail closed to manual (a fix loop cannot
            # install host toolchains). This precedence wins over a co-occurring quality failure.
            self._s.store.complete_node_run(
                run_id, status="incomplete", outcome=None, finished_at=self._s.clock()
            )
            raise NodeManualRequired(
                f"checks node {node.id!r}: the quality gate could not fully run (a required "
                "toolchain is absent on the host, or every selected check was skipped) — changed "
                "code must not be handed on unchecked"
            )
        if outcome.any_quality_failed:
            self._publish_first_failure_log(ctx, outcome)
            return self._complete(run_id, node, passed=False)
        if self._mutated_working_tree(before):
            # Green-but-dirtying guard: a passing check that rewrote commit-candidate files must not
            # pass silently. Fail closed to manual review.
            self._s.store.complete_node_run(
                run_id, status="dirtied_working_tree", outcome=None, finished_at=self._s.clock()
            )
            raise NodeManualRequired(
                f"checks node {node.id!r}: a check mutated the working tree "
                "(commit-candidate files changed across the check run) — a green-but-dirtying "
                "check must not pass silently"
            )
        self._publish_command_report(ctx, node, run_id, outcome)
        return self._complete(run_id, node, passed=True)

    def _publish_command_report(
        self, ctx: NodeContext, node: ChecksNode, run_id: int, outcome: CheckOutcome
    ) -> None:
        """Publish the per-command verdicts and point ``{checks_path}`` at them, on a PASS.

        The failure path publishes the first failing log, which is the text ``fixing`` needs. A
        pass published nothing, so ``{checks_path}`` stayed ``None`` and the next node was told
        nothing about the gate at all. That is the same gap :meth:`_publish_citation_report`
        already closes for the other profile, and it costs more here: an evaluator asked to judge
        an acceptance criterion like "lint and build pass" is handed no evidence of it, and under a
        profile that gives it a shell it verifies the only way left — by running the build itself,
        inside the agent sandbox, which is not the environment the gate runs in. Observed: the
        reviewer's own attempt died on the sandbox, and it filed that as a blocking finding with no
        path, about a failure that had not happened, into a fix loop that could not act on it.

        Deliberately a summary rather than the logs: what the next node needs is which commands ran
        and how they ended, and a skipped check has to be distinguishable from a passing one — a
        skip is not evidence of a pass.
        """
        run_dir = self._run_dir(ctx.task_id, node.id, run_id)
        artifact = run_dir / "checks.json"
        artifact.write_text(_command_report_json(outcome), encoding="utf-8")
        self._register(ctx.task_id, "checks", str(artifact))
        self._in.checks_path = publish_file(
            self._s.exchange_root,
            ctx.task_id,
            f"checks/{artifact.name}",
            str(artifact),
            extra_secrets=self._s.prompt_secrets,
        )

    def _publish_first_failure_log(self, ctx: NodeContext, outcome: CheckOutcome) -> None:
        """Publish the redacted first-failure log and set ``{checks_path}`` before the fail edge.

        The authoritative full log is already written privately by the CheckRunner; this copies the
        first failing command's log (redacted) into the exchange and points ``checks_path`` at it,
        so the live ``fixing`` node receives ``{checks_path}`` with no restart. With no exchange
        wired (a unit harness) it points at the private log instead of staying unset.
        """
        log = outcome.first_failure_log
        if log is None:
            return
        self._in.checks_path = publish_file(
            self._s.exchange_root,
            ctx.task_id,
            f"checks/{Path(log).name}",
            str(log),
            extra_secrets=self._s.prompt_secrets,
        )

    # -- citation ------------------------------------------------------------

    def _run_citation(self, node: ChecksNode, ctx: NodeContext, run_id: int) -> NodeResult:
        """Validate the flow's citation manifest; a hallucinated citation → ``fail``."""
        checks_dir = self._checks_dir(ctx.task_id)
        resolved = resolve_output_policy(ctx.snapshot.doc.output_policy, ctx.task_id)
        report_dir = resolved.report_dir(self._s.repo_dir)
        # A missing manifest (no report dir, or the named file absent) → uncheckable, never a crash.
        # The filename comes from the node, so a flow whose writing node names its manifest anything
        # else is checkable — as a literal it silently produced a gate that did nothing.
        manifest = (report_dir or checks_dir) / node.manifest
        started_at = self._s.clock()  # bracket the (in-process) validation work
        report = validate_citations(self._s.repo_dir, manifest)
        finished_at = self._s.clock()
        run_dir = self._run_dir(ctx.task_id, node.id, run_id)
        artifact = run_dir / "citation.json"
        artifact.write_text(_citation_json(report, self._repo_relpath(manifest)), encoding="utf-8")
        self._record_check_run(
            ctx,
            command="citation",
            exit_code=0 if report.passed else 1,
            timed_out=False,
            passed=report.passed,
            log_path=str(artifact),
            started_at=started_at,
            finished_at=finished_at,
        )
        self._register(ctx.task_id, "citation", str(artifact))
        self._publish_citation_report(ctx, artifact)
        return self._complete(run_id, node, passed=report.passed)

    def _publish_citation_report(self, ctx: NodeContext, artifact: Path) -> None:
        """Publish the per-entry verdicts and point ``{checks_path}`` at them, on BOTH outcomes.

        Set on a pass as well as on a failure: the downstream verifier's prompt asserts a guarantee
        based on this report, so it must be able to audit that claim, and the next evaluator needs
        the entry list including the non-gating ``weak``/``uncheckable`` ones it is responsible for.
        Leaving it to the command-profile failure path alone would deliver the report to nobody
        exactly when the check passed.
        """
        self._in.checks_path = publish_file(
            self._s.exchange_root,
            ctx.task_id,
            f"checks/{artifact.name}",
            str(artifact),
            extra_secrets=self._s.prompt_secrets,
        )

    # -- dependency_scan -----------------------------------------------------

    def _run_dependency_scan(self, node: ChecksNode, ctx: NodeContext, run_id: int) -> NodeResult:
        """Run the core-owned advisory scanners as evidence; always ``pass`` (the scan ran)."""
        run_dir = self._run_dir(ctx.task_id, node.id, run_id)
        report = run_dependency_scan(
            repo_dir=self._s.repo_dir,
            logs_dir=run_dir / "dependency_scan",
            env=self._s.process_env,
            timeout_seconds=self._s.scan_timeout_s,
            run_process=self._s.run_process,
            clock=self._s.clock,
        )
        artifact = run_dir / "dependency_scan.json"
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
                started_at=scan.started_at,
                finished_at=scan.finished_at,
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

    def _repo_relpath(self, path: Path) -> str | None:
        """*path* relative to the clone (POSIX), or ``None`` when it lies outside it.

        The manifest's own location is published with the verdicts so the evaluator that consumes
        them can open the manifest for each entry's claim without being told a directory convention
        in its prompt. ``None`` for a flow with no report directory, where the looked-up manifest
        sits in the private artifact tree and no agent may read it.
        """
        try:
            return path.relative_to(self._s.repo_dir).as_posix()
        except ValueError:
            return None

    def _checks_dir(self, task_id: str) -> Path:
        checks_dir = Path(task_artifact_dir(self._s.artifacts_root, task_id)) / "checks"
        checks_dir.mkdir(parents=True, exist_ok=True)
        return checks_dir

    def _run_dir(self, task_id: str, node_id: str, run_id: int) -> Path:
        """The per-run dir for this checks node's reports (keyed by node.id + run_id), created."""
        run_dir = node_run_dir(self._s.artifacts_root, task_id, node_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _record_check_run(
        self,
        ctx: NodeContext,
        *,
        command: str,
        exit_code: int | None,
        timed_out: bool,
        passed: bool,
        log_path: str,
        started_at: str,
        finished_at: str,
        skipped: bool = False,
    ) -> None:
        # The caller supplies the check's measured interval (bracketed around the actual
        # work) rather than reading the clock twice at row-write time, so ``check_runs`` carries a
        # real duration the operator can query ("which check was slow").
        self._s.store.record_check_run(
            CheckRunRow(
                task_id=ctx.task_id,
                subtask_order=ctx.subtask_order,
                command=command,
                exit_code=exit_code,
                timed_out=timed_out,
                passed=passed,
                log_path=log_path,
                skipped=skipped,
                started_at=started_at,
                finished_at=finished_at,
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

    def _run_checks(self, ctx: NodeContext, selected: tuple[ResolvedCheckSet, ...]) -> CheckOutcome:
        """Run the selected command sets and record one ``check_runs`` row per command."""
        outcome = self._s.check_runner.run(
            clone_dir=self._s.repo_dir,
            artifacts_root=self._s.artifacts_root,
            task_id=ctx.task_id,
            subtask=ctx.subtask_order,
            selected=selected,
            clock=self._s.clock,
        )
        for run in outcome.runs:
            self._record_check_run(
                ctx,
                command=run.command,
                exit_code=run.exit_code,
                timed_out=run.timed_out,
                passed=run.passed,
                log_path=run.log_path,
                started_at=run.started_at,
                finished_at=run.finished_at,
                skipped=run.skipped,
            )
        return outcome


def _command_report_json(outcome: CheckOutcome) -> str:
    # One entry per command, with `skipped` carried explicitly: a check whose toolchain was absent
    # did not run, so a reader must not count it as a pass. `exit_code` is `None` for a command that
    # never launched or was skipped.
    return (
        json.dumps(
            {
                "passed": outcome.passed,
                "checks": [
                    {
                        "name": run.name,
                        "command": run.command,
                        "exit_code": run.exit_code,
                        "passed": run.passed,
                        "skipped": run.skipped,
                        "timed_out": run.timed_out,
                    }
                    for run in outcome.runs
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def _citation_json(report: CitationReport, manifest_path: str | None) -> str:
    # `path`/`line` are echoed per entry: this file is now handed to the downstream verifier, which
    # cannot act on an entry whose location it would have to recover from the reason prose.
    # `manifest_path` names the manifest that was validated, so that verifier can open it for each
    # entry's claim (the verdicts carry locations, never claims) without its prompt hardcoding where
    # a deliverable lives.
    return (
        json.dumps(
            {
                "manifest_path": manifest_path,
                "manifest_status": report.manifest_status,
                "passed": report.passed,
                "entries": [
                    {
                        "source_id": e.source_id,
                        "status": e.status.value,
                        "reason": e.reason,
                        "path": e.path,
                        "line": e.line,
                    }
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
