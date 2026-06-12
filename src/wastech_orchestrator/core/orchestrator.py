"""The deterministic Orchestrator Core pipeline (spec §5, §6, §8).

Drives one task end to end: validation gate → slot → branch → refinement (deterministic skip) →
planning (+ decomposition) → per-unit [implementation → testing → review → fixing] → summary →
publishing → terminal cleanup → ledger. The Core **never** builds a CLI command — it calls only the
Agent Router for agent stages, the Check Runner for ``testing``, and the Git Manager for everything
that touches git. Context is handed to agents **only as artifact file paths** on the request (§6).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wastech_orchestrator.check_runner import CheckOutcome, CheckRunner
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.decomposition import (
    DecompositionDecision,
    SubtaskSpec,
    decide_decomposition,
    update_subtask_index,
    write_subtask_artifacts,
)
from wastech_orchestrator.core.loop_control import FixLoop, LoopController, LoopCounters
from wastech_orchestrator.core.recovery import (
    RecoveryAction,
    RecoveryPlan,
    RecoveryReconciler,
)
from wastech_orchestrator.core.state_machine import Status, assert_transition
from wastech_orchestrator.git_manager import (
    GitCommandError,
    GitManager,
    ManualActionRequired,
)
from wastech_orchestrator.ledger import (
    DecomposedFailureInfo,
    Ledger,
    LedgerRecord,
    write_failure_report,
    write_minimal_summary,
)
from wastech_orchestrator.notify import Notifier, NullNotifier, build_notifier
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.providers.artifacts import sha256_file, task_artifact_dir
from wastech_orchestrator.providers.base import (
    AgentProvider,
    AgentRunRequest,
    ProviderId,
    RunStatus,
    Stage,
)
from wastech_orchestrator.routing.router import AgentRouter, StageOutcome
from wastech_orchestrator.security.isolation import check_isolation
from wastech_orchestrator.state_store import (
    ArtifactRow,
    CheckRunRow,
    ProviderAttemptRow,
    StageRunRow,
    StateStore,
    SubtaskRow,
    TaskRow,
)
from wastech_orchestrator.task.model import NormalizedTask
from wastech_orchestrator.task.parser import (
    load_normalized,
    read_task_source,
    slugify,
    write_normalized,
)
from wastech_orchestrator.task.validation_gate import (
    Completeness,
    ValidationGate,
    ValidationResult,
    write_validation_report,
)

_LOG = logging.getLogger(__name__)

# Severities that make a review finding "blocking" → the review-driven fix loop (§5, §8.1).
_BLOCKING_SEVERITIES = frozenset({"blocking", "critical", "high"})

# Concise per-stage instruction prompts. Task content is NOT embedded — the provider appends the
# context **file paths** set on the request (§6); these strings only state the stage's intent.
_STAGE_PROMPTS: dict[Stage, str] = {
    Stage.REFINEMENT: (
        "Enrich the task into a complete, unambiguous specification. Document any assumptions. "
        "Do not edit code. Read the context files listed below."
    ),
    Stage.PLANNING: (
        "Produce a brief implementation plan from the task and enriched spec. Do not edit code. "
        "If decomposition is enabled and the task is large, instead return an ordered subtask list."
    ),
    Stage.IMPLEMENTATION: (
        "Implement the task in the working tree, following the plan. Make a minimal focused change."
    ),
    Stage.REVIEW: (
        "Review the current diff against the task and plan. Report findings with a severity each; "
        "mark anything that must change before merge as blocking."
    ),
    Stage.FIXING: (
        "Address the failing checks and/or the blocking review findings in the context files. "
        "Make the minimal change needed to resolve them."
    ),
    Stage.SUMMARY: (
        "Write a plain-language summary of the change: what was done, how it works, how it "
        "integrates, and why. Do not edit code."
    ),
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Map a task-level artifact filename to its registry ``kind`` (§10). Unknown names fall back to the
# filename so registration is always meaningful even if a new artifact is added.
_ARTIFACT_KINDS: dict[str, str] = {
    "task.enriched.md": "enriched",
    "plan.md": "plan",
    "fixing-context.json": "fixing_context",
}

_UNIT_STATUSES = frozenset(
    {Status.IMPLEMENTING, Status.TESTING, Status.REVIEWING, Status.FIXING}
)


def _artifact_kind(name: str) -> str:
    return _ARTIFACT_KINDS.get(name, name)


@dataclass(frozen=True)
class PipelineResult:
    """The terminal outcome of running one task."""

    task_id: str
    final_status: Status
    pr_url: str | None = None
    validation_reason: str | None = None


class SlotBusyError(Exception):
    """Raised when the single processing slot is already held by another active task (§8.2)."""


class PipelineFailed(Exception):
    """An unrecoverable error → terminal ``failed`` (e.g. no provider could complete a stage)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Pipeline:
    """Mutable per-run context threaded through the stage drivers."""

    task: NormalizedTask
    task_file: str
    status: Status
    counters: LoopCounters
    decomposition: DecompositionDecision
    plan_path: str | None = None
    enriched_path: str | None = None
    diff_path: str | None = None
    check_log: str | None = None
    review_findings_path: str | None = None
    last_review_findings: list[dict[str, Any]] = field(default_factory=list)
    branch: str = ""
    slug: str = ""


class Orchestrator:
    """The single-slot deterministic Core (§8.2). One instance drives one task at a time."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        router: AgentRouter,
        git: GitManager,
        checks: CheckRunner,
        store: StateStore,
        ledger: Ledger,
        loops: LoopController,
        gate: ValidationGate,
        artifacts_root: str | Path,
        clock: Callable[[], str] = _utc_now_iso,
        monotonic: Callable[[], float] = time.monotonic,
        notifier: Notifier | None = None,
    ) -> None:
        self._config = config
        self._router = router
        self._git = git
        self._checks = checks
        self._store = store
        self._ledger = ledger
        self._loops = loops
        self._gate = gate
        self._artifacts_root = artifacts_root
        self._clock = clock
        self._monotonic = monotonic
        self._notifier: Notifier = notifier if notifier is not None else NullNotifier()

    # --- entry point ----------------------------------------------------------------------

    def run_task(self, task_file: str) -> PipelineResult:
        """Process exactly one task file through the pipeline, then perform terminal cleanup."""
        source = read_task_source(task_file)
        result = self._gate.validate(source)
        if not result.passed:
            return self._reject(task_file, result)

        assert result.normalized is not None
        task = result.normalized
        completeness = result.completeness or Completeness.NEEDS_ENRICHMENT

        if not self.acquire_slot(task.id):
            raise SlotBusyError(f"another task is active; {task.id} must wait (§8.2)")

        self._register_task(task, task_file, result)
        pipeline = _Pipeline(
            task=task,
            task_file=task_file,
            status=Status.VALIDATED,
            counters=LoopCounters(),
            decomposition=DecompositionDecision(accepted=False, reason="pending", n=1),
        )
        try:
            return self._drive(pipeline, completeness)
        except ManualActionRequired as exc:
            return self._go_terminal(
                pipeline, Status.MANUAL_ACTION_REQUIRED, manual_reason=exc.reason
            )
        except (PipelineFailed, GitCommandError) as exc:
            return self._fail(pipeline, str(exc))

    def acquire_slot(self, task_id: str) -> bool:
        """True iff no *other* task currently owns the processing slot (§8.2)."""
        return not any(t.task_id != task_id for t in self._store.find_active_tasks())

    def refresh_repo(self) -> None:
        """Best-effort fetch/pull of ``base_branch`` so git-pushed tasks become visible (§8.3).

        Called by the ``watch`` loop between ticks. Delegates to the Git Manager, which no-ops
        unless the working copy is on ``base_branch`` (the slot is free after terminal cleanup), so
        it never disturbs an active or interrupted task branch.
        """
        self._git.refresh_base()

    def resume(self) -> PipelineResult | None:
        """Reconcile persisted state on startup and resume the single unfinished task (§13).

        Returns the terminal result of the resumed task, or ``None`` when the slot is free (no
        active task and no interrupted cleanup) so a caller may pick a pending task.
        """
        plan = RecoveryReconciler(self._config, self._store, self._git).reconcile()
        if plan.action is RecoveryAction.NONE:
            return None
        if plan.action is RecoveryAction.MANUAL:
            return self._resume_manual(plan)
        if plan.action is RecoveryAction.CLEANUP:
            return self._resume_cleanup(plan.task_id)
        assert plan.task_id is not None
        return self._resume_task(plan)

    def _resume_manual(self, plan: RecoveryPlan) -> PipelineResult:
        """Mark every ambiguously-active task ``manual_action_required`` and record it (§13)."""
        for task_id in plan.manual_task_ids:
            self._store.set_status(task_id, Status.MANUAL_ACTION_REQUIRED)
            self._store.update_task(
                task_id, finished_at=self._clock(), cleanup_last_error=plan.manual_reason
            )
            row = self._store.get_task(task_id)
            if row is not None and not self._ledger.has_task_id(task_id):
                self._ledger.append(
                    LedgerRecord(
                        id=task_id,
                        title=row.title,
                        final_status=Status.MANUAL_ACTION_REQUIRED.value,
                        finished_at=self._clock(),
                        branch=row.branch,
                        fix_iterations=row.fix_iterations,
                    )
                )
                self._notify_terminal(
                    task_id=task_id,
                    final_status=Status.MANUAL_ACTION_REQUIRED,
                    pr_url=None,
                    reason=plan.manual_reason,
                )
        first = plan.manual_task_ids[0] if plan.manual_task_ids else (plan.task_id or "")
        return PipelineResult(task_id=first, final_status=Status.MANUAL_ACTION_REQUIRED)

    def _resume_cleanup(self, task_id: str | None) -> PipelineResult | None:
        """Finish an interrupted terminal cleanup: checkout base once, then ledger (§8.3)."""
        if task_id is None:
            return None
        row = self._store.get_task(task_id)
        if row is None:
            return None
        cleanup = self._git.terminal_cleanup(task_id)
        self._store.update_task(
            task_id,
            cleanup_target_branch=cleanup.target_branch,
            cleanup_completed=cleanup.safe,
            cleanup_completed_at=self._clock() if cleanup.safe else None,
            cleanup_last_error=cleanup.error,
        )
        if not self._ledger.has_task_id(task_id):
            self._ledger.append(
                LedgerRecord(
                    id=task_id,
                    title=row.title,
                    branch=row.branch,
                    final_status=row.status.value,
                    fix_iterations=row.fix_iterations,
                    terminal_cleanup="completed" if cleanup.safe else "blocked",
                    finished_at=self._clock(),
                )
            )
            self._notify_terminal(
                task_id=task_id,
                final_status=row.status,
                pr_url=None,
                reason=cleanup.error,
            )
        return PipelineResult(task_id=task_id, final_status=row.status)

    def _resume_task(self, plan: RecoveryPlan) -> PipelineResult:
        """Rebuild the context for the one active task and continue it idempotently (§13)."""
        assert plan.task_id is not None
        row = self._store.get_task(plan.task_id)
        assert row is not None
        task = load_normalized(self._artifacts_root, plan.task_id)
        decomposition = self._rebuild_decomposition(plan.task_id, row)
        p = _Pipeline(
            task=task,
            task_file=row.source_path or "",
            status=row.status,
            counters=self._store.get_counters(plan.task_id),
            decomposition=decomposition,
            branch=row.branch or "",
            slug=row.slug or slugify(task.title),
            plan_path=str(task_artifact_dir(self._artifacts_root, plan.task_id) / "plan.md"),
        )

        publish_phase = {
            Status.SUMMARIZING,
            Status.READY_TO_PUBLISH,
            Status.COMMITTING,
            Status.PUSHING,
            Status.CREATING_PR,
        }
        try:
            if row.status is Status.VALIDATED:
                return self._drive(p, self._gate.phase_b(task))
            if row.status is Status.PREPARING:
                self._prepare_branch(p)
                self._refinement(p, self._gate.phase_b(task))
                self._planning(p)
                return self._run_units_and_finish(p)

            # Re-attach to the existing branch (reused, never recreated) so git ops target it.
            self._git.prepare_branch(plan.task_id, p.slug)

            if row.status is Status.REFINING:
                self._run_refinement(p)
                self._planning(p)
                return self._run_units_and_finish(p)
            if row.status is Status.PLANNING:
                self._planning(p)
                return self._run_units_and_finish(p)
            if row.status in _UNIT_STATUSES:
                self._restore_recovery_context(p, row)
                return self._run_units_and_finish(p)
            if row.status in publish_phase:
                if row.status is Status.SUMMARIZING:
                    self._summary(p)
                else:
                    self._store.set_status(plan.task_id, Status.READY_TO_PUBLISH)
                    p.status = Status.READY_TO_PUBLISH
                return self._publish(p)
            raise PipelineFailed(f"cannot recover task from status {row.status.value}")
        except ManualActionRequired as exc:
            return self._go_terminal(p, Status.MANUAL_ACTION_REQUIRED, manual_reason=exc.reason)
        except (PipelineFailed, GitCommandError) as exc:
            return self._go_terminal(p, Status.FAILED, manual_reason=str(exc))

    def _rebuild_decomposition(self, task_id: str, row: TaskRow) -> DecompositionDecision:
        if not row.decomposition_accepted:
            return DecompositionDecision(
                accepted=False, reason=row.decomposition_reason or "single", n=1
            )
        specs = tuple(
            SubtaskSpec(
                order=s.order,
                title=s.title,
                slug=s.slug,
                acceptance_criteria=(),
                depends_on=s.depends_on,
            )
            for s in self._store.get_subtasks(task_id)
        )
        return DecompositionDecision(
            accepted=True,
            reason=row.decomposition_reason or "accepted",
            n=row.subtask_count or len(specs),
            subtasks=specs,
        )

    # --- pipeline -------------------------------------------------------------------------

    def _drive(self, p: _Pipeline, completeness: Completeness) -> PipelineResult:
        # strict_isolation preflight (§12.8): if a provider that may run cannot have its required
        # isolation enabled, fail here — before any branch — rather than silently downgrading.
        if self._config.security.strict_isolation:
            reasons = check_isolation(self._config)
            if reasons:
                joined = "; ".join(reasons)
                self._log(p.task.id).warning(
                    "isolation preflight failed", extra={"reasons": joined}
                )
                raise PipelineFailed(f"strict_isolation: {joined}")

        self._transition(p, Status.PREPARING)
        self._prepare_branch(p)
        self._refinement(p, completeness)
        self._planning(p)
        return self._run_units_and_finish(p)

    def _prepare_branch(self, p: _Pipeline) -> None:
        """Complete the persisted ``preparing`` checkpoint and attach the task branch."""
        # Branch + footprint preflight (no branch is ever created before this point).
        self._git.preflight_footprint()
        self._git.ensure_exclude_local()
        p.slug = slugify(p.task.title)
        p.branch = self._observe(
            p,
            "branch preparation",
            lambda: self._git.prepare_branch(p.task.id, p.slug),
        )
        self._store.update_task(p.task.id, branch=p.branch, slug=p.slug)

    def _run_units_and_finish(self, p: _Pipeline) -> PipelineResult:
        """Run the per-unit loop (skipping already-committed subtasks) then summary + publish."""
        units: list[SubtaskSpec | None] = (
            list(p.decomposition.subtasks) if p.decomposition.accepted else [None]
        )
        completed = {s.order for s in self._store.get_subtasks(p.task.id) if s.commit_sha}
        for index, unit in enumerate(units):
            if unit is not None and unit.order in completed:
                continue  # recovery: a subtask with a recorded commit is never re-run (§13)
            terminal = self._run_unit(p, unit, is_last=index == len(units) - 1)
            if terminal is not None:
                return terminal

        self._summary(p)
        return self._publish(p)

    def _refinement(self, p: _Pipeline, completeness: Completeness) -> None:
        skip = p.task.refined or completeness is Completeness.COMPLETE
        if skip:
            reason = "task flagged refined" if p.task.refined else "task already complete"
            self._store.update_task(p.task.id, refinement_ran=False, refinement_skip_reason=reason)
            self._log(p.task.id).info("refinement skipped", extra={"reason": reason})
            self._transition(p, Status.PLANNING)
            return
        self._log(p.task.id).info("refinement running")
        self._transition(p, Status.REFINING)
        self._run_refinement(p)

    def _run_refinement(self, p: _Pipeline) -> None:
        """Run or re-run the persisted ``refining`` checkpoint."""
        outcome = self._run_stage(p, Stage.REFINEMENT)
        message = self._require_result(p, outcome, Stage.REFINEMENT)
        p.enriched_path = self._write_artifact(p, "task.enriched.md", message)
        self._store.update_task(p.task.id, refinement_ran=True)
        self._transition(p, Status.PLANNING)

    def _planning(self, p: _Pipeline) -> None:
        outcome = self._run_stage(p, Stage.PLANNING)
        result = self._require_result_outcome(p, outcome, Stage.PLANNING)
        p.plan_path = self._write_artifact(p, "plan.md", result.final_message or "")
        gate_on = self._decomposition_gate_on(p.task)
        decision = decide_decomposition(
            result.structured_output,
            gate_on=gate_on,
            max_subtasks=self._config.agents.decomposition.max_subtasks,
        )
        p.decomposition = decision
        self._log(p.task.id).info(
            "decomposition decided",
            extra={
                "gate_on": gate_on,
                "accepted": decision.accepted,
                "n": decision.n,
                "reason": decision.reason,
            },
        )
        self._store.update_task(
            p.task.id,
            decomposition_enabled=gate_on,
            decomposition_accepted=decision.accepted,
            decomposition_reason=decision.reason,
            subtask_count=decision.n if decision.accepted else None,
            active_subtask=1 if decision.accepted else None,
        )
        if decision.accepted:
            write_subtask_artifacts(decision, self._artifacts_root, p.task.id)
            subtasks_dir = task_artifact_dir(self._artifacts_root, p.task.id) / "subtasks"
            self._register_artifact(p.task.id, "subtasks_index", str(subtasks_dir / "index.json"))
            for s in decision.subtasks:
                self._register_artifact(
                    p.task.id, "subtask_spec", str(subtasks_dir / f"{s.order:02d}-{s.slug}.md")
                )
            self._store.insert_subtasks(
                [
                    SubtaskRow(
                        task_id=p.task.id,
                        order=s.order,
                        slug=s.slug,
                        title=s.title,
                        status="pending",
                        depends_on=s.depends_on,
                    )
                    for s in decision.subtasks
                ]
            )
        self._transition(p, Status.IMPLEMENTING)

    def _run_unit(
        self, p: _Pipeline, unit: SubtaskSpec | None, *, is_last: bool
    ) -> PipelineResult | None:
        """Run one unit's implement→test→review→fix loop; return a terminal result if stuck."""
        subtask = unit.order if unit is not None else None
        if p.status not in _UNIT_STATUSES:
            raise PipelineFailed(f"cannot run unit from status {p.status.value}")

        while True:
            if p.status is Status.IMPLEMENTING:
                outcome = self._run_stage(p, Stage.IMPLEMENTATION, subtask=subtask, unit=unit)
                self._require_result(p, outcome, Stage.IMPLEMENTATION)
                p.diff_path = self._git.write_current_diff(p.task.id)
                self._register_artifact(p.task.id, "diff", p.diff_path)
                self._transition(p, Status.TESTING)

            if p.status is Status.TESTING:
                check = self._run_checks(p, subtask)
                if check.passed:
                    self._loops.on_check_pass(p.counters)
                    self._transition(p, Status.REVIEWING)
                else:
                    p.check_log = check.first_failure_log
                    stuck = self._enter_fixing(p, FixLoop.TEST)
                    if stuck is not None:
                        return stuck
                    continue

            if p.status is Status.REVIEWING:
                outcome = self._run_stage(p, Stage.REVIEW, subtask=subtask, unit=unit)
                result = self._require_result_outcome(p, outcome, Stage.REVIEW)
                blocking = self._write_review(p, result.structured_output, result.final_message)
                if not blocking:
                    self._loops.on_review_pass(p.counters)
                    self._save_counters(p)
                    return self._on_review_passed(p, unit, is_last=is_last)
                stuck = self._enter_fixing(p, FixLoop.REVIEW)
                if stuck is not None:
                    return stuck

            if p.status is Status.FIXING:
                outcome = self._run_stage(p, Stage.FIXING, subtask=subtask, unit=unit)
                self._require_result(p, outcome, Stage.FIXING)
                p.diff_path = self._git.write_current_diff(p.task.id)
                self._register_artifact(p.task.id, "diff", p.diff_path)
                self._transition(p, Status.TESTING)

    def _on_review_passed(
        self, p: _Pipeline, unit: SubtaskSpec | None, *, is_last: bool
    ) -> PipelineResult | None:
        if unit is not None:
            # Every subtask — the last one included — gets its own local commit on the single
            # branch (§5.1, ``commit_per_subtask``); publishing then only pushes + opens the PR.
            message = f"feat({p.task.id}): subtask {unit.order:02d} {unit.title}"
            sha = self._git.commit_subtask(p.task.id, unit.order, unit.slug, message)
            update_subtask_index(
                self._artifacts_root, p.task.id, unit.order, status="committed", commit_sha=sha
            )
            self._store.set_subtask_commit(p.task.id, unit.order, sha, "committed")
            self._store.update_task(p.task.id, subtasks_completed=unit.order)
            if not is_last:
                self._loops.reset_for_next_subtask(p.counters)
                self._store.update_task(p.task.id, active_subtask=unit.order + 1)
                self._save_counters(p)
                self._transition(p, Status.IMPLEMENTING)
                return None
        self._transition(p, Status.SUMMARIZING)
        return None

    def _summary(self, p: _Pipeline) -> None:
        outcome = self._run_stage(p, Stage.SUMMARY)
        if outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED:
            self._write_summary_from_agent(
                p, outcome.result.final_message, outcome.result.structured_output
            )
        else:
            # Best-effort stage: no provider could produce it → deterministic minimal summary.
            diff = self._git.cumulative_committed_diff() or (
                Path(p.diff_path).read_text(encoding="utf-8") if p.diff_path else ""
            )
            write_minimal_summary(
                self._artifacts_root,
                p.task.id,
                title=p.task.title,
                description=p.task.description,
                diff=diff,
            )
        # summary.json stays under logs/ (a working artifact, never committed); the human-readable
        # summary.md is placed next to the task and committed during finalize (§6, §21.3).
        task_dir = task_artifact_dir(self._artifacts_root, p.task.id)
        self._register_artifact(p.task.id, "summary_json", str(task_dir / "summary.json"))
        self._transition(p, Status.READY_TO_PUBLISH)

    def _publish(self, p: _Pipeline) -> PipelineResult:
        # Finalize BEFORE the commit so the task move + summary.md enter the task commit (§6, §21):
        # the scoped code commit carries the code, the audit commit carries `tasks/` (the moved task
        # file + `<id>.summary.md`); `logs/` is never committed.
        summary_md = self._finalize_task_artifacts(p, Status.DONE)
        self._transition(p, Status.COMMITTING)
        message = f"feat({p.task.id}): {p.task.title}"
        self._observe(
            p,
            "commit",
            lambda: (self._git.commit_code(p.task.id, message), self._git.commit_audit(p.task.id)),
        )
        self._transition(p, Status.PUSHING)
        self._observe(p, "push", lambda: self._git.push(p.task.id, p.branch))
        self._transition(p, Status.CREATING_PR)
        body_path = str(summary_md) if summary_md else self._fallback_summary_path(p)
        pr_url = self._observe(
            p,
            "pull request",
            lambda: self._git.create_pr(
                p.task.id, p.branch, title=f"{p.task.title}", body_path=body_path
            ),
        )
        return self._go_terminal(p, Status.DONE, pr_url=pr_url, already_moved=True)

    def _fallback_summary_path(self, p: _Pipeline) -> str:
        """The logs/ working copy of summary.md — PR body fallback when no task file is on disk."""
        return str(task_artifact_dir(self._artifacts_root, p.task.id) / "summary.md")

    def _summary_md_body(self, p: _Pipeline) -> str:
        """The human-readable summary text; falls back to a deterministic minimal summary (§5.2)."""
        md_path = task_artifact_dir(self._artifacts_root, p.task.id) / "summary.md"
        if not md_path.exists():
            diff = self._git.cumulative_committed_diff() or (
                Path(p.diff_path).read_text(encoding="utf-8")
                if p.diff_path and Path(p.diff_path).exists()
                else ""
            )
            write_minimal_summary(
                self._artifacts_root,
                p.task.id,
                title=p.task.title,
                description=p.task.description,
                diff=diff,
            )
        return md_path.read_text(encoding="utf-8") if md_path.exists() else (p.task.title + "\n")

    def _finalize_task_artifacts(self, p: _Pipeline, final: Status) -> Path | None:
        """Move the task into its lifecycle folder; write the committed `<id>.summary.md` alongside.

        Runs **before** the commit so both land in the task (audit) commit (§6, §21.3). Returns the
        path to the committed `summary.md`, or ``None`` when there is no on-disk task file (e.g. a
        synthetic ``run`` path). ``summary.json`` and the rest of ``logs/`` are never committed.
        """
        dest = self._move_task_file(p, final)
        body = self._summary_md_body(p)
        if dest is None:
            return None
        summary_path = dest.with_name(f"{p.task.id}.summary.md")
        try:
            summary_path.write_text(body, encoding="utf-8")
        except OSError:
            return None
        self._register_artifact(p.task.id, "summary_md", str(summary_path))
        return summary_path

    # --- fix-loop control -----------------------------------------------------------------

    def _enter_fixing(self, p: _Pipeline, loop: FixLoop) -> PipelineResult | None:
        decision = self._loops.enter_fixing(p.counters, loop)
        self._save_counters(p)
        counters = {
            "loop": loop.value,
            "fix_iterations": p.counters.fix_iterations,
            "test_fix_cycles": p.counters.test_fix_cycles,
            "review_fix_cycles": p.counters.review_fix_cycles,
            "stage_attempts": p.counters.stage_attempts,
        }
        if decision.stuck:
            self._log(p.task.id).warning(
                "task stuck", extra={**counters, "limit": decision.limit_name}
            )
            report_path = self._write_failure_report(p, decision.loop, decision.limit_name)
            self._store.update_task(p.task.id, failure_report_path=report_path)
            return self._go_terminal(
                p, Status.MANUAL_ACTION_REQUIRED, manual_reason=f"stuck: {decision.limit_name}"
            )
        self._log(p.task.id).info("entering fixing", extra=counters)
        self._write_fixing_context(p, loop)
        self._transition(p, Status.FIXING)
        return None

    def _write_fixing_context(self, p: _Pipeline, loop: FixLoop) -> str:
        """Persist the current fixing trigger so restart can rebuild the provider request."""
        payload = {
            "loop": loop.value,
            "check_artifacts_path": p.check_log if loop is FixLoop.TEST else None,
            "review_artifacts_path": (
                p.review_findings_path if loop is FixLoop.REVIEW else None
            ),
        }
        return self._write_artifact(
            p,
            "fixing-context.json",
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )

    def _restore_recovery_context(self, p: _Pipeline, row: TaskRow) -> None:
        """Restore task-level paths needed by testing/review/fixing after restart."""
        task_dir = task_artifact_dir(self._artifacts_root, p.task.id)
        diff_path = task_dir / "current.diff"
        if diff_path.exists():
            p.diff_path = str(diff_path)

        if p.status is not Status.FIXING:
            return

        context_path = task_dir / "fixing-context.json"
        context: Mapping[str, Any] = {}
        if context_path.exists():
            try:
                loaded = json.loads(context_path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    context = loaded
            except (OSError, json.JSONDecodeError):
                context = {}

        check_path = context.get("check_artifacts_path")
        if isinstance(check_path, str) and Path(check_path).exists():
            p.check_log = check_path
        review_path = context.get("review_artifacts_path")
        if isinstance(review_path, str) and Path(review_path).exists():
            p.review_findings_path = review_path

        # Backward compatibility for tasks that entered fixing before the checkpoint existed.
        subtask = row.active_subtask if row.decomposition_accepted else None
        if p.check_log is None:
            latest_check = self._store.latest_failed_check_log(p.task.id, subtask)
            if latest_check and Path(latest_check).exists():
                p.check_log = latest_check
        if p.review_findings_path is None:
            fallback_review = task_dir / "review" / "findings.json"
            if fallback_review.exists():
                p.review_findings_path = str(fallback_review)

        if p.review_findings_path:
            try:
                loaded = json.loads(
                    Path(p.review_findings_path).read_text(encoding="utf-8")
                )
                if isinstance(loaded, Mapping):
                    p.last_review_findings = self._extract_findings(loaded)
            except (OSError, json.JSONDecodeError):
                p.last_review_findings = []

    # --- terminal handling ----------------------------------------------------------------

    def _fail(self, p: _Pipeline, error: str) -> PipelineResult:
        """Terminal ``failed``. When a task branch exists, finalize like a success — move the task
        to ``tasks/failed/``, write its ``summary.md``, commit (code + task) and push — so the
        failed attempt and its summary are stored in git (§6). No PR is opened for a failure. When
        no branch was created yet (e.g. an isolation-preflight failure), nothing is published.

        The git operations are best-effort: a failed task must still reach a terminal state even if
        git is unhappy, so a publish error here is logged, not raised.
        """
        if not p.branch:
            return self._go_terminal(p, Status.FAILED, manual_reason=error)
        moved = False
        try:
            moved = self._finalize_task_artifacts(p, Status.FAILED) is not None
            message = f"chore({p.task.id}): failed attempt — {p.task.title}"
            self._git.commit_code(p.task.id, message)
            self._git.commit_audit(p.task.id)
            self._git.push(p.task.id, p.branch)
        except (GitCommandError, OSError) as exc:
            self._log(p.task.id).warning(
                "failed-task publish incomplete", extra={"error": str(exc)}
            )
        return self._go_terminal(p, Status.FAILED, manual_reason=error, already_moved=moved)

    def _go_terminal(
        self,
        p: _Pipeline,
        status: Status,
        *,
        pr_url: str | None = None,
        manual_reason: str | None = None,
        already_moved: bool = False,
    ) -> PipelineResult:
        """Run terminal cleanup, set the final status, append exactly one ledger record (§8.3).

        ``already_moved`` is set when the task file was moved + committed during finalize (§6); the
        move is then complete on the task branch, so this must not re-move it on ``base_branch``
        after the cleanup checkout.
        """
        final = status
        cleanup = self._observe(
            p, "terminal cleanup", lambda: self._git.terminal_cleanup(p.task.id)
        )
        if not cleanup.safe and status is Status.DONE:
            # Publishing finished but the working copy could not be safely restored → manual (§8.3).
            final = Status.MANUAL_ACTION_REQUIRED
        # Record the terminal-cleanup outcome and the reason this task stopped (when applicable).
        last_error = cleanup.error or manual_reason
        self._store.update_task(
            p.task.id,
            cleanup_target_branch=cleanup.target_branch,
            cleanup_completed=cleanup.safe,
            cleanup_completed_at=self._clock() if cleanup.safe else None,
            cleanup_last_error=last_error,
        )
        self._transition(p, final, finished_at=self._clock())
        if not already_moved:
            self._move_task_file(p, final)
        self._append_ledger(p, final, pr_url=pr_url, cleanup_safe=cleanup.safe)
        self._notify_terminal(
            task_id=p.task.id, final_status=final, pr_url=pr_url, reason=manual_reason
        )
        self._log(p.task.id).info(
            "terminal",
            extra={"final_status": final.value, "pr_url": pr_url, "cleanup_safe": cleanup.safe},
        )
        return PipelineResult(task_id=p.task.id, final_status=final, pr_url=pr_url)

    def _move_task_file(self, p: _Pipeline, final: Status) -> Path | None:
        """Move the task file into its lifecycle folder (``tasks/done`` / ``tasks/failed``, §20.2).

        ``done`` and ``failed`` (died during processing) move; ``manual_action_required`` stays put
        for the operator to resolve (§8.3). A §19 gate reject is quarantined separately (§19.4).
        Idempotent: returns the destination path whether it moved now or was already in place (a
        restart, or a finalize that already moved it); returns ``None`` when there is nothing to do.
        """
        folder_name = {Status.DONE: "done", Status.FAILED: "failed"}.get(final)
        if folder_name is None or not p.task_file:
            return None
        src = Path(p.task_file)
        parent = src.parent
        if parent.name in ("pending", "processing", "done", "failed"):
            tasks_root = parent.parent
        else:
            tasks_root = parent
        dest = tasks_root / folder_name / src.name
        if src.resolve() == dest.resolve():
            return dest  # already in its lifecycle folder (idempotent restart)
        if not src.exists():
            return dest if dest.exists() else None  # already moved
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dest)
            self._store.update_task(p.task.id, source_path=str(dest))
        except OSError:
            # Never let a file-move failure mask the terminal outcome; the ledger still records it.
            return None
        return dest

    def _reject(self, task_file: str, result: ValidationResult) -> PipelineResult:
        """Handle a §19 Phase-A reject: failed, quarantine, report, ledger — no branch (§19.4)."""
        task_id = Path(task_file).stem
        reason = result.reason.value if result.reason else "unknown"
        self._log(task_id).info("validation rejected", extra={"reason": reason})
        write_validation_report(result, task_id, self._artifacts_root)
        self._quarantine(task_file)
        self._ledger.append(
            LedgerRecord(
                id=task_id,
                title=task_id,
                final_status=Status.FAILED.value,
                finished_at=self._clock(),
                validation_reason=reason,
                branch=None,
            )
        )
        self._notify_terminal(
            task_id=task_id, final_status=Status.FAILED, pr_url=None, reason=reason
        )
        return PipelineResult(task_id=task_id, final_status=Status.FAILED, validation_reason=reason)

    def _quarantine(self, task_file: str) -> str | None:
        """Move the task file ``processing/ -> tasks/rejected/`` when a tasks/ layout is present."""
        src = Path(task_file)
        if not src.exists():
            return None
        quarantine_dir = Path(self._config.validation.quarantine_folder)
        try:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            dest = quarantine_dir / src.name
            src.replace(dest)
            return str(dest)
        except OSError:
            return None

    # --- stage execution + artifacts ------------------------------------------------------

    def _run_stage(
        self,
        p: _Pipeline,
        stage: Stage,
        *,
        subtask: int | None = None,
        unit: SubtaskSpec | None = None,
    ) -> StageOutcome:
        route = self._router.resolve_route(stage, p.task.agents)
        provider_cfg = self._config.agents.providers[route.primary]
        started_at = self._clock()
        run_id = self._store.record_stage_run(
            StageRunRow(
                task_id=p.task.id,
                stage=stage.value,
                subtask_order=subtask,
                status="running",
                route_primary=route.primary.value,
                route_fallback=route.fallback.value if route.fallback else None,
                route_source=route.source.value,
                stage_attempts=0,
                started_at=started_at,
            )
        )
        request = AgentRunRequest(
            task_id=p.task.id,
            stage=stage,
            working_directory=self._config.repo.local_path,
            prompt=self._build_prompt(p, stage, unit),
            permission_profile=provider_cfg.permission_profile,
            timeout_seconds=provider_cfg.timeout_seconds,
            attempt=1,
            stage_run_id=run_id,
            task_path=p.task_file,
            plan_path=p.plan_path,
            diff_path=p.diff_path,
            check_artifacts_path=p.check_log,
            review_artifacts_path=p.review_findings_path,
        )
        fields: dict[str, object] = {
            "stage": stage.value,
            "primary": route.primary.value,
            "stage_run_id": run_id,
        }
        if subtask is not None:
            fields["subtask"] = subtask
        outcome = self._observe(
            p,
            "stage",
            lambda: self._router.run_stage(request, route, snapshot=self._git),
            fields=fields,
        )
        self._record_stage(run_id, outcome)
        p.counters.stage_attempts = outcome.stage_attempts
        return outcome

    def _build_prompt(self, p: _Pipeline, stage: Stage, unit: SubtaskSpec | None) -> str:
        prompt = _STAGE_PROMPTS[stage]
        if unit is not None:
            spec_path = (
                task_artifact_dir(self._artifacts_root, p.task.id)
                / "subtasks"
                / f"{unit.order:02d}-{unit.slug}.md"
            )
            prompt += f"\n\nActive subtask {unit.order} of {p.decomposition.n}; spec: {spec_path}"
        return prompt

    def _record_stage(self, run_id: int, outcome: StageOutcome) -> None:
        status = outcome.result.status.value if outcome.result is not None else "infra_exhausted"
        self._store.complete_stage_run(
            run_id,
            status=status,
            provider_used=outcome.provider_used.value if outcome.provider_used else None,
            error_class=outcome.terminal_error.error_class.value
            if outcome.terminal_error
            else None,
            stage_attempts=outcome.stage_attempts,
            finished_at=self._clock(),
        )
        for attempt in outcome.attempts:
            attempt_dir = (
                str(Path(attempt.result.stdout_path).parent)
                if attempt.result and attempt.result.stdout_path
                else None
            )
            self._store.record_provider_attempt(
                ProviderAttemptRow(
                    stage_run_id=run_id,
                    provider=attempt.provider.value,
                    attempt=attempt.attempt,
                    status=attempt.status.value if attempt.status else None,
                    error_class=attempt.error_class.value if attempt.error_class else None,
                    exit_code=attempt.result.exit_code if attempt.result else None,
                    attempt_dir=attempt_dir,
                    started_at=self._clock(),
                    finished_at=self._clock(),
                )
            )

    def _run_checks(self, p: _Pipeline, subtask: int | None) -> CheckOutcome:
        outcome = self._checks.run(
            clone_dir=self._config.repo.local_path,
            artifacts_root=self._artifacts_root,
            task_id=p.task.id,
            subtask=subtask,
        )
        for run in outcome.runs:
            self._store.record_check_run(
                CheckRunRow(
                    task_id=p.task.id,
                    subtask_order=subtask,
                    command=run.command,
                    exit_code=run.exit_code,
                    timed_out=run.timed_out,
                    passed=run.passed,
                    log_path=run.log_path,
                    started_at=self._clock(),
                    finished_at=self._clock(),
                )
            )
        return outcome

    # --- artifact helpers -----------------------------------------------------------------

    def _write_artifact(self, p: _Pipeline, name: str, content: str) -> str:
        task_dir = task_artifact_dir(self._artifacts_root, p.task.id)
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / name
        path.write_text(content, encoding="utf-8")
        self._register_artifact(p.task.id, _artifact_kind(name), str(path))
        return str(path)

    def _log(self, task_id: str) -> logging.LoggerAdapter[logging.Logger]:
        """A task-scoped structured logger (§6.6): every record carries ``task_id``."""
        return bind(_LOG, task_id=task_id)

    def _register_artifact(self, task_id: str, kind: str, path: str | None) -> None:
        """Register a durable §10 artifact in SQLite with a sha256 checksum (best-effort, §10).

        Idempotent (the store upserts on ``(task_id, kind, path)``); a missing file is skipped and
        registration never raises into the terminal path. Requires the ``tasks`` row to exist (FK),
        so a §19-rejected task — which has no row — is not registered here.
        """
        if not path or not Path(path).exists():
            return
        self._store.register_artifact(
            ArtifactRow(task_id=task_id, kind=kind, path=path, checksum=sha256_file(path))
        )

    def _write_review(
        self, p: _Pipeline, structured: Mapping[str, Any] | None, message: str | None
    ) -> bool:
        review_dir = task_artifact_dir(self._artifacts_root, p.task.id) / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        findings = self._extract_findings(structured)
        (review_dir / "findings.json").write_text(
            json.dumps({"findings": findings}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (review_dir / "summary.md").write_text(message or "(no review summary)\n", encoding="utf-8")
        p.review_findings_path = str(review_dir / "findings.json")
        p.last_review_findings = findings
        self._register_artifact(p.task.id, "review_findings", p.review_findings_path)
        self._register_artifact(p.task.id, "review_summary", str(review_dir / "summary.md"))
        return any(self._is_blocking(f) for f in findings)

    def _extract_findings(self, structured: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(structured, Mapping):
            return []
        raw = structured.get("findings")
        if not isinstance(raw, list):
            return []
        return [dict(f) for f in raw if isinstance(f, Mapping)]

    def _is_blocking(self, finding: Mapping[str, Any]) -> bool:
        if finding.get("blocking") is True:
            return True
        severity = str(finding.get("severity", "")).lower()
        return severity in _BLOCKING_SEVERITIES

    def _write_summary_from_agent(
        self, p: _Pipeline, message: str | None, structured: Mapping[str, Any] | None
    ) -> None:
        task_dir = task_artifact_dir(self._artifacts_root, p.task.id)
        task_dir.mkdir(parents=True, exist_ok=True)
        body = message or p.task.title
        (task_dir / "summary.md").write_text(body + "\n", encoding="utf-8")
        payload = dict(structured) if isinstance(structured, Mapping) else {"what": p.task.title}
        (task_dir / "summary.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def _write_failure_report(
        self, p: _Pipeline, loop: FixLoop | None, limit_name: str | None
    ) -> str:
        diff = (
            Path(p.diff_path).read_text(encoding="utf-8")
            if p.diff_path and Path(p.diff_path).exists()
            else ""
        )
        check_log = (
            Path(p.check_log).read_text(encoding="utf-8")
            if p.check_log and Path(p.check_log).exists()
            else None
        )
        report_path, stuck_path = write_failure_report(
            self._artifacts_root,
            p.task.id,
            loop=loop.value if loop else "unknown",
            limit_name=limit_name or "unknown",
            counters={
                "stage_attempts": p.counters.stage_attempts,
                "test_fix_cycles": p.counters.test_fix_cycles,
                "review_fix_cycles": p.counters.review_fix_cycles,
                "fix_iterations": p.counters.fix_iterations,
            },
            last_check_log=check_log,
            last_review_findings=p.last_review_findings,
            final_diff=diff,
            decomposed=self._decomposed_failure_info(p),
        )
        self._register_artifact(p.task.id, "failure_report", report_path)
        self._register_artifact(p.task.id, "stuck", stuck_path)
        return report_path

    def _decomposed_failure_info(self, p: _Pipeline) -> DecomposedFailureInfo | None:
        """Build the decomposed failure-report block (§10): failing subtask + committed SHAs."""
        if not p.decomposition.accepted:
            return None
        row = self._store.get_task(p.task.id)
        subtasks = sorted(self._store.get_subtasks(p.task.id), key=lambda s: s.order)
        committed = tuple(sha for s in subtasks if (sha := s.commit_sha) is not None)
        completed = row.subtasks_completed if row else len(committed)
        failing = row.active_subtask if row and row.active_subtask else len(committed) + 1
        return DecomposedFailureInfo(
            subtask_count=p.decomposition.n,
            subtasks_completed=completed,
            failing_subtask=failing,
            committed_shas=committed,
        )

    # --- store helpers --------------------------------------------------------------------

    def _register_task(self, task: NormalizedTask, task_file: str, result: Any) -> None:
        self._store.insert_task(
            TaskRow(
                task_id=task.id,
                title=task.title,
                status=Status.NEW,
                source_path=task_file,
                validation_passed=True,
            )
        )
        normalized_path = write_normalized(task, self._artifacts_root)
        report_path = write_validation_report(result, task.id, self._artifacts_root)
        self._register_artifact(task.id, "normalized", normalized_path)
        self._register_artifact(task.id, "validation_report", report_path)
        self._transition_status(task.id, Status.NEW, Status.VALIDATED)

    def _decomposition_gate_on(self, task: NormalizedTask) -> bool:
        if task.decompose is True:
            return True
        if task.decompose is False:
            return False
        return self._config.agents.decomposition.enabled

    def _transition(self, p: _Pipeline, dst: Status, **fields: object) -> None:
        src = p.status
        with self._store.transaction() as conn:
            assert_transition(src, dst)
            self._store.set_status(p.task.id, dst, conn)
            self._store.save_counters(p.task.id, p.counters, conn)
            if fields:
                self._store.update_task(p.task.id, conn, **fields)
        p.status = dst
        self._log(p.task.id).info(
            "status changed", extra={"from_status": src.value, "to_status": dst.value}
        )

    def _transition_status(self, task_id: str, src: Status, dst: Status) -> None:
        with self._store.transaction() as conn:
            assert_transition(src, dst)
            self._store.set_status(task_id, dst, conn)

    def _save_counters(self, p: _Pipeline) -> None:
        self._store.save_counters(p.task.id, p.counters)

    def _observe[T](
        self,
        p: _Pipeline,
        operation_name: str,
        operation: Callable[[], T],
        *,
        fields: Mapping[str, object] | None = None,
    ) -> T:
        """Log a safe start/end/failure envelope around a pipeline operation."""
        log = self._log(p.task.id)
        safe_fields = dict(fields or {})
        started = self._monotonic()
        log.info(f"{operation_name} started", extra=safe_fields)
        try:
            result = operation()
        except Exception as exc:
            log.error(
                f"{operation_name} failed",
                extra={
                    **safe_fields,
                    "duration_seconds": round(self._monotonic() - started, 3),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        log.info(
            f"{operation_name} completed",
            extra={
                **safe_fields,
                "duration_seconds": round(self._monotonic() - started, 3),
            },
        )
        return result

    def _notify_terminal(
        self,
        *,
        task_id: str,
        final_status: Status,
        pr_url: str | None,
        reason: str | None,
    ) -> None:
        """Best-effort terminal notification (§4.7). Never raises and never alters the outcome."""
        try:
            self._notifier.send_notification(
                task_id=task_id,
                final_status=final_status.value,
                pr_url=pr_url,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — notifier is best-effort by contract
            self._log(task_id).warning(
                "terminal notification failed", extra={"error_type": type(exc).__name__}
            )

    def _append_ledger(
        self, p: _Pipeline, final: Status, *, pr_url: str | None, cleanup_safe: bool
    ) -> None:
        task_row = self._store.get_task(p.task.id)
        self._ledger.append(
            LedgerRecord(
                id=p.task.id,
                title=p.task.title,
                branch=p.branch or None,
                pr_url=pr_url,
                final_status=final.value,
                fix_iterations=p.counters.fix_iterations,
                terminal_cleanup="completed" if cleanup_safe else "blocked",
                finished_at=self._clock(),
                failure_report=task_row.failure_report_path if task_row else None,
                decomposed=p.decomposition.accepted,
                subtask_count=p.decomposition.n if p.decomposition.accepted else None,
                subtasks_completed=task_row.subtasks_completed if task_row else None,
            )
        )

    # --- stage-result guards --------------------------------------------------------------

    def _require_result_outcome(self, p: _Pipeline, outcome: StageOutcome, stage: Stage) -> Any:
        # Infra-exhausted (no provider could run the stage even after fallback) is unrecoverable →
        # terminal ``failed`` (§8.1 "no provider available"). A quality FAILED result is NOT fatal:
        # it flows on through the normal pipeline (testing/review/fixing are the quality gates).
        if outcome.result is None:
            error = (
                outcome.terminal_error.error_class.value
                if outcome.terminal_error
                else "no_provider_available"
            )
            raise PipelineFailed(f"{stage.value}: no provider could complete it ({error})")
        return outcome.result

    def _require_result(self, p: _Pipeline, outcome: StageOutcome, stage: Stage) -> str:
        result = self._require_result_outcome(p, outcome, stage)
        return result.final_message or ""


def build_providers(
    config: OrchestratorConfig,
    *,
    artifacts_root: str | Path,
    heartbeat_seconds: float = 30.0,
) -> dict[ProviderId, AgentProvider]:
    """Construct the real provider adapters for the configured providers (Core + CLI use this)."""
    from wastech_orchestrator.providers.claude import ClaudeCodeProvider
    from wastech_orchestrator.providers.codex import CodexProvider

    root = str(Path(artifacts_root))
    providers: dict[ProviderId, AgentProvider] = {}
    for pid, provider_cfg in config.agents.providers.items():
        if pid is ProviderId.CLAUDE:
            providers[pid] = ClaudeCodeProvider(
                provider_cfg,
                security=config.security,
                artifacts_root=root,
                heartbeat_seconds=heartbeat_seconds,
            )
        elif pid is ProviderId.CODEX:
            providers[pid] = CodexProvider(
                provider_cfg,
                security=config.security,
                artifacts_root=root,
                heartbeat_seconds=heartbeat_seconds,
            )
    return providers


def build_orchestrator(
    config: OrchestratorConfig,
    *,
    artifacts_root: str | Path,
    gh_runner: Callable[..., Any] | None = None,
    heartbeat_seconds: float = 30.0,
) -> Orchestrator:
    """Wire the full dependency graph from a validated config (used by the CLI and e2e tests).

    Constructs the real provider adapters, Router, State Store (``<artifacts_root>/state.db``),
    ledger (``<artifacts_root>/logs/completed.jsonl``), Git Manager, Check Runner, loop controller,
    and validation gate. The Core depends only on these interfaces — never on a provider directly.
    """
    root = Path(artifacts_root)
    providers = build_providers(config, artifacts_root=root, heartbeat_seconds=heartbeat_seconds)

    store = StateStore.open(root / "state.db")
    ledger = Ledger(root / "logs")
    router = AgentRouter(config, providers)
    git = GitManager(
        config,
        store=store,
        artifacts_root=str(root),
        gh_runner=gh_runner,
        heartbeat_seconds=heartbeat_seconds,
    )
    checks = CheckRunner(config, heartbeat_seconds=heartbeat_seconds)
    gate = ValidationGate(
        config,
        store_has_task_id=store.task_id_exists,
        ledger_has_task_id=ledger.has_task_id,
    )
    loops = LoopController(config.agents)
    notifier = build_notifier(config.telegram)
    return Orchestrator(
        config,
        router=router,
        git=git,
        checks=checks,
        store=store,
        ledger=ledger,
        loops=loops,
        gate=gate,
        artifacts_root=str(root),
        notifier=notifier,
    )
