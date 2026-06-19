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
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wastech_orchestrator.check_runner import CheckRunner
from wastech_orchestrator.checks.discovery_factory import build_discovery
from wastech_orchestrator.checks.model import ResolvedCheck
from wastech_orchestrator.checks.profile import ResolvedCheckProfile
from wastech_orchestrator.checks.resolver import CheckResolver, ReResolveReason
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.decomposition import (
    DecompositionDecision,
    SubtaskSpec,
    subtask_spec_path,
    update_subtask_index,
    write_subtask_artifacts,
)
from wastech_orchestrator.core.flow.engine import FlowRunResult, NodeOutcome, entry_node_id
from wastech_orchestrator.core.flow.engine_driver import (
    DecompositionRegions,
    drive_flow,
    partition_decomposition,
)
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInfraError,
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.postprocess import apply_output_artifact, read_decomposition
from wastech_orchestrator.core.flow.recorder import StateStoreRunRecorder, hydrate_run_state
from wastech_orchestrator.core.flow.registry import FlowRegistry
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import AgentNode, EvaluatorNode, FlowNode
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.flow.wiring import build_node_inputs, build_node_services
from wastech_orchestrator.core.hitl import (
    HumanInputSignal,
    consume_pending_interactions,
    discovery_interaction_id,
    discovery_interaction_path,
    handle_from_artifact,
    load_interaction,
    mark_consumed,
    reset_pending_interactions,
    write_answer,
    write_waiting_interaction,
)
from wastech_orchestrator.core.loop_control import LoopCounters
from wastech_orchestrator.core.recovery import (
    RecoveryAction,
    RecoveryPlan,
    RecoveryReconciler,
)
from wastech_orchestrator.core.skills import (
    SkillDedupEntry,
    SkillInventory,
    SkillInventoryScanner,
    SkillRef,
    SkillSelection,
    resolve_planning_skills,
)
from wastech_orchestrator.core.state_machine import Status, assert_transition
from wastech_orchestrator.core.supervisor import Supervisor
from wastech_orchestrator.git_manager import (
    GitCommandError,
    GitManager,
    ManualActionRequired,
)
from wastech_orchestrator.ledger import (
    Ledger,
    LedgerRecord,
    write_minimal_summary,
)
from wastech_orchestrator.notify import (
    AskKind,
    AskResult,
    Notifier,
    NullNotifier,
    build_notifier,
)
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.providers.artifacts import (
    archive_task_artifacts,
    sha256_file,
    task_artifact_dir,
)
from wastech_orchestrator.providers.base import (
    AgentProvider,
    ProviderId,
    Stage,
)
from wastech_orchestrator.providers.redaction import read_denied_secrets, redact_text
from wastech_orchestrator.routing.router import AgentRouter
from wastech_orchestrator.security.isolation import check_isolation
from wastech_orchestrator.state_store import (
    ArtifactRow,
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


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Map a task-level artifact filename to its registry ``kind`` (§10). Unknown names fall back to the
# filename so registration is always meaningful even if a new artifact is added.
_ARTIFACT_KINDS: dict[str, str] = {
    "task.enriched.md": "enriched",
    "plan.md": "plan",
    "fixing-context.json": "fixing_context",
    "rendered-prompt.md": "rendered_prompt",
}

@dataclass(frozen=True)
class RerunPlan:
    """The reconciled facts + refusals for a ``rerun``/``rerun --continue`` (read-only; §rerun)."""

    task_id: str
    continue_mode: bool
    found: bool = False
    current_status: Status | None = None
    source_path: str | None = None
    branch: str | None = None
    base_branch: str = ""
    attempt: int = 1
    interrupted_node: str | None = None  # the flow checkpoint's current_node, for the dry-run view
    dirty_paths: tuple[str, ...] = ()
    has_remote_branch: bool = False
    pr_url: str | None = None
    refusals: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalizePlan:
    """The reconciled facts + warnings/refusals for a ``finalize`` (read-only; §finalize)."""

    task_id: str
    declared: Status
    found: bool = False
    current_status: Status | None = None
    source_path: str | None = None
    branch: str | None = None
    base_branch: str = ""
    pr_url: str | None = None
    pr_url_source: str = "none"  # explicit | recorded | none
    verify_state: str | None = None  # gh PR state when checked (MERGED/OPEN/CLOSED)
    dirty_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()  # non-fatal; require confirmation (no URL / not merged)
    refusals: tuple[str, ...] = ()  # fatal; abort with exit 1


def _ledger_has_manual(ledger: Ledger, task_id: str) -> bool:
    """True iff the ledger already holds an operator-finalized (``manual``) record for the id."""
    return any(r.get("id") == task_id and r.get("manual") for r in ledger.records())


def _ledger_attempt_count(ledger: Ledger, task_id: str) -> int:
    """How many terminal records the ledger already holds for ``task_id`` (prior attempts)."""
    return sum(1 for rec in ledger.records() if rec.get("id") == task_id)


_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")


def _validate_session_id(raw: str) -> str | None:
    return raw if _SESSION_ID_RE.fullmatch(raw) else None


def _artifact_kind(name: str) -> str:
    return _ARTIFACT_KINDS.get(name, name)


def effective_skip(task: NormalizedTask) -> frozenset[Stage]:
    """The stages skipped for ``task`` — its own ``stages.<stage>.enabled: false`` overrides
    (per-task stage-skip control; flow-contract §10 bounded exception).

    Validation has already guaranteed every member is in ``SKIPPABLE_STAGES`` and that any
    ``review`` skip is permitted (``agents.allow_review_skip``), so the orchestrator can trust this
    set unconditionally.
    """
    return task.disabled_stages()


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
    check_profile: ResolvedCheckProfile | None = None  # resolved at preflight (before any branch)
    reresolved_once: bool = False  # mid-task check re-resolve is bounded to once per task (§1.2)
    # Repo skill inventory scanned at task start; planning's chosen subset is surfaced to downstream
    # stages as read-only reference paths (§2.1). Re-derived per run; `selected_skills` is set when
    # the planning agent runs this process and is persisted to `selected_skills.json`, so a resume
    # past planning restores it (see `_persist_selected_skills` / `_restore_engine_inputs`).
    skill_inventory: SkillInventory = field(default_factory=SkillInventory)
    selected_skills: tuple[SkillRef, ...] = ()
    # Effective stage-skip set (global config ∪ per-task). Re-derived on every run/resume from
    # config + frontmatter, so a restart recovers it without persistence (stage-skip control).
    skip: frozenset[Stage] = frozenset()


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
        gate: ValidationGate,
        artifacts_root: str | Path,
        clock: Callable[[], str] = _utc_now_iso,
        monotonic: Callable[[], float] = time.monotonic,
        notifier: Notifier | None = None,
        resolver: CheckResolver | None = None,
        skill_scanner: SkillInventoryScanner | None = None,
    ) -> None:
        self._config = config
        self._router = router
        self._git = git
        self._checks = checks
        self._store = store
        self._ledger = ledger
        self._gate = gate
        self._artifacts_root = artifacts_root
        self._clock = clock
        self._monotonic = monotonic
        self._notifier: Notifier = notifier if notifier is not None else NullNotifier()
        # The check resolver runs a deterministic preflight before any branch (automatic check
        # discovery §11). ``None`` skips it — the Check Runner then uses ``checks.commands``.
        self._resolver = resolver
        # Repo skill inventory scanner (§2.1). Defaults to the target repo clone's `.claude/skills`.
        self._skill_scanner = skill_scanner or self._default_skill_scanner()
        # Per-id attempt number stamped onto the next ledger record, set by ``rerun``/``continue``.
        self._rerun_attempt: dict[str, int] = {}
        # Flow registry (P1.4 cutover): resolves a task's flow snapshot. Packaged-only in P1;
        # operator flows (``.worc/flows/``) are wired on the live path in P4.
        self._flow_registry = FlowRegistry()
        # The constant supervisor layer (P2.1) — rebuilt per task in ``_engine_run`` (it carries the
        # task's own resume_own_lineage session). Single-slot, so one live instance at a time.
        self._supervisor: Supervisor | None = None

    def _default_skill_scanner(self) -> SkillInventoryScanner:
        root = self._config.skills.scan_root or str(
            Path(self._config.repo.local_path) / ".claude" / "skills"
        )
        return SkillInventoryScanner(
            root,
            denied_read_paths=self._config.security.denied_read_paths,
            excluded_names=self._config.skills.exclude,
        )

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
            skip=effective_skip(task),
            skill_inventory=self._skill_scanner.collect(),
        )
        try:
            # The FlowEngine is the sole driver: the refinement→…→publish pipeline is the validated
            # flow graph, executed by the engine (both fresh and on resume).
            return self._drive_via_engine(pipeline, completeness)
        except ManualActionRequired as exc:
            return self._go_terminal(
                pipeline, Status.MANUAL_ACTION_REQUIRED, manual_reason=exc.reason
            )
        except (PipelineFailed, GitCommandError) as exc:
            return self._fail(pipeline, str(exc))

    def acquire_slot(self, task_id: str) -> bool:
        """True iff no *other* task currently owns the processing slot (§8.2)."""
        return not any(t.task_id != task_id for t in self._store.find_active_tasks())

    # --- rerun (operator-driven re-attempt of a terminal task) ----------------------------

    def plan_rerun(
        self,
        task_id: str,
        *,
        continue_mode: bool = False,
        force_reset_remote: bool = False,
    ) -> RerunPlan:
        """Gather the facts + refusal reasons for a ``rerun`` (read-only; mutates nothing)."""
        row = self._store.get_task(task_id)
        if row is None:
            return RerunPlan(
                task_id=task_id,
                continue_mode=continue_mode,
                found=False,
                refusals=(f"unknown task id '{task_id}'",),
            )
        refusals: list[str] = []
        if row.status not in (Status.FAILED, Status.MANUAL_ACTION_REQUIRED):
            refusals.append(
                f"task '{task_id}' is {row.status.value}; rerun is for failed / "
                "manual_action_required tasks (use `run` for a new task)"
            )
        others = sorted(t.task_id for t in self._store.find_active_tasks() if t.task_id != task_id)
        if others:
            refusals.append(
                f"another task is active ({', '.join(others)}); rerun needs an idle slot"
            )
        source_path = row.source_path
        if not source_path or not Path(source_path).is_file():
            refusals.append(f"task source file is missing ({source_path or 'unset'}); cannot rerun")
        dirty = self._git.unaccounted_dirty_paths()
        if dirty:
            refusals.append(
                f"the working tree has unaccounted changes ({', '.join(sorted(dirty))}); "
                "resolve them before rerun"
            )
        interrupted_node: str | None = None
        has_remote = False
        pr_url: str | None = None
        if continue_mode:
            # --continue re-enters at the flow checkpoint (current_node); the node id is surfaced
            # in the dry-run view.
            current_node, _counters, _fingerprint = self._store.get_flow_checkpoint(task_id)
            interrupted_node = current_node
            if not current_node:
                refusals.append(
                    "no recoverable node was recorded for this task; use a fresh `rerun` "
                    "(without --continue)"
                )
        else:
            pr_url = self._git.recorded_pr_url(task_id)
            has_remote = bool(row.branch) and self._git.remote_branch_exists(row.branch or "")
            if (has_remote or pr_url) and not force_reset_remote:
                refusals.append(
                    f"a prior attempt left a remote branch / open PR ({pr_url or row.branch}); "
                    "resolve it with `finalize` first, or pass --force-reset-remote to delete the "
                    "remote branch (this closes the PR)"
                )
        return RerunPlan(
            task_id=task_id,
            continue_mode=continue_mode,
            found=True,
            current_status=row.status,
            source_path=source_path,
            branch=row.branch,
            base_branch=self._config.repo.base_branch,
            attempt=_ledger_attempt_count(self._ledger, task_id) + 1,
            interrupted_node=interrupted_node,
            dirty_paths=tuple(sorted(dirty)),
            has_remote_branch=has_remote,
            pr_url=pr_url,
            refusals=tuple(refusals),
        )

    def rerun_task(
        self, task_id: str, *, source_path: str, force_reset_remote: bool = False
    ) -> PipelineResult:
        """Fresh attempt of a terminal task from the *current* ``base_branch`` (§rerun).

        Archives the prior attempt's artifacts, resets the branch to base, clears the per-attempt
        state, then drives the full pipeline via ``run_task`` (the gate admits the id once). The
        git/fs steps are idempotent and the state reset leaves the status terminal, so an
        interrupted rerun stays re-runnable.
        """
        row = self._store.get_task(task_id)
        if row is None:
            raise PipelineFailed(f"unknown task id '{task_id}'")
        slug = row.slug or slugify(row.title)
        prior = _ledger_attempt_count(self._ledger, task_id)
        archive_task_artifacts(self._artifacts_root, task_id, prior)
        self._git.reset_branch_to_base(task_id, slug, force_reset_remote=force_reset_remote)
        self._store.reset_task_for_rerun(task_id)
        self._rerun_attempt[task_id] = prior + 1
        self._log(task_id).info("rerun: fresh attempt", extra={"attempt": prior + 1})
        return self.run_task(source_path)

    def continue_task(self, task_id: str) -> PipelineResult:
        """Fix-and-continue: revive a terminal task at the stage it failed and resume it (§rerun).

        Reuses the existing branch and all prior work; only the terminal markers are cleared and
        any un-answered HITL prompt is reset so the re-entered stage asks fresh. The whole pipeline
        re-run is delegated to the resume engine (``resume`` → ``_resume_task``), which
        idempotently re-enters at the recovered stage.
        """
        row = self._store.get_task(task_id)
        if row is None:
            raise PipelineFailed(f"unknown task id '{task_id}'")
        current_node, _counters, _fingerprint = self._store.get_flow_checkpoint(task_id)
        if not current_node:
            raise PipelineFailed(
                f"cannot continue '{task_id}': no recoverable stage recorded; use a fresh rerun"
            )
        self._rerun_attempt[task_id] = _ledger_attempt_count(self._ledger, task_id) + 1
        reset = reset_pending_interactions(self._artifacts_root, task_id)
        if reset:
            self._log(task_id).info(
                "rerun --continue: reset pending HITL", extra={"reset": len(reset)}
            )
        # Revive the terminal task as active; the resume engine re-enters at the persisted
        # ``current_node`` (the flow checkpoint), reusing the branch + prior work.
        self._store.revive_task_for_continue(task_id, Status.RUNNING)
        self._log(task_id).info("rerun --continue: revived", extra={"node": current_node})
        result = self.resume()
        if result is None:
            raise PipelineFailed(f"continue '{task_id}' did not resume (no active task found)")
        return result

    # --- finalize (operator records + tidies a task they handled out-of-band) -------------

    def plan_finalize(
        self,
        task_id: str,
        *,
        declared: Status,
        pr_url: str | None = None,
        verify: bool = True,
    ) -> FinalizePlan:
        """Gather the facts + warnings/refusals for a ``finalize`` (read-only; mutates nothing)."""
        row = self._store.get_task(task_id)
        if row is None:
            return FinalizePlan(
                task_id=task_id,
                declared=declared,
                found=False,
                refusals=(f"unknown task id '{task_id}'",),
            )
        refusals: list[str] = []
        warnings: list[str] = []
        if _ledger_has_manual(self._ledger, task_id):
            refusals.append(
                f"task '{task_id}' was already finalized (a manual ledger record exists); refusing"
            )
        dirty = self._git.unaccounted_dirty_paths()
        if dirty:
            refusals.append(
                f"the working tree has unaccounted changes ({', '.join(sorted(dirty))}); "
                "resolve them before finalize (it will not discard your work)"
            )
        resolved_url: str | None = None
        source = "none"
        verify_state: str | None = None
        if declared is Status.DONE:
            if pr_url:
                resolved_url, source = pr_url, "explicit"
            elif (recorded := self._git.recorded_pr_url(task_id)) is not None:
                resolved_url, source = recorded, "recorded"
            else:
                warnings.append("no PR URL found for this task; recording done without merge proof")
            if resolved_url is not None and verify:
                verify_state = self._git.verify_pr_state(resolved_url)
                if verify_state is not None and verify_state != "MERGED":
                    warnings.append(f"the PR is {verify_state}, not merged; recording done anyway")
        return FinalizePlan(
            task_id=task_id,
            declared=declared,
            found=True,
            current_status=row.status,
            source_path=row.source_path,
            branch=row.branch,
            base_branch=self._config.repo.base_branch,
            pr_url=resolved_url,
            pr_url_source=source,
            verify_state=verify_state,
            dirty_paths=tuple(sorted(dirty)),
            warnings=tuple(warnings),
            refusals=tuple(refusals),
        )

    def finalize_task(
        self,
        task_id: str,
        *,
        declared: Status,
        pr_url: str | None = None,
        note: str | None = None,
        delete_branch: bool = False,
    ) -> PipelineResult:
        """Reconcile a task the operator handled by hand: set the declared terminal status, tidy the
        working tree/branch/file/HITL, and append a ``manual`` ledger record. Runs no pipeline and
        never commits/pushes/PRs. ``pr_url`` is the already-resolved URL (see ``plan_finalize``)."""
        row = self._store.get_task(task_id)
        if row is None:
            raise PipelineFailed(f"unknown task id '{task_id}'")
        cleanup = self._git.terminal_cleanup(task_id)
        if not cleanup.safe:
            # Fail-closed: do not touch status/file/ledger when the tree can't be safely restored.
            self._store.update_task(
                task_id,
                cleanup_target_branch=cleanup.target_branch,
                cleanup_completed=False,
                cleanup_last_error=cleanup.error,
            )
            raise PipelineFailed(f"finalize blocked: {cleanup.error}")
        self._store.update_task(
            task_id,
            cleanup_target_branch=cleanup.target_branch,
            cleanup_completed=True,
            cleanup_completed_at=self._clock(),
            cleanup_last_error=note,
            finished_at=self._clock(),
        )
        self._store.set_status(task_id, declared)  # out-of-band operator override (no assert)
        self._relocate_task_file(row.source_path, task_id, declared)
        consume_pending_interactions(self._artifacts_root, task_id)
        if delete_branch and row.branch:
            self._git.delete_branch(row.branch)
        outcome = "abandoned" if declared is Status.MANUAL_ACTION_REQUIRED else None
        self._ledger.append(
            LedgerRecord(
                id=task_id,
                title=row.title,
                branch=row.branch,
                pr_url=pr_url,
                final_status=declared.value,
                fix_iterations=row.fix_iterations,
                terminal_cleanup="completed",
                finished_at=self._clock(),
                failure_report=row.failure_report_path,
                decomposed=bool(row.decomposition_accepted),
                subtask_count=row.subtask_count,
                subtasks_completed=row.subtasks_completed,
                manual=True,
                note=note,
                outcome=outcome,
            )
        )
        self._log(task_id).info(
            "finalized", extra={"final_status": declared.value, "pr_url": pr_url, "manual": True}
        )
        return PipelineResult(task_id=task_id, final_status=declared, pr_url=pr_url)

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
            skip=effective_skip(task),
            skill_inventory=self._skill_scanner.collect(),
        )

        try:
            return self._resume_via_engine(p)
        except ManualActionRequired as exc:
            return self._go_terminal(p, Status.MANUAL_ACTION_REQUIRED, manual_reason=exc.reason)
        except (PipelineFailed, GitCommandError) as exc:
            return self._go_terminal(p, Status.FAILED, manual_reason=str(exc))

    def _resume_via_engine(self, p: _Pipeline) -> PipelineResult:
        """Resume the engine from the persisted checkpoint (node-based recovery, §13).

        Hydrates the :class:`FlowRunState` from ``node_runs`` + the ``tasks`` checkpoint and
        continues from ``current_node`` (the decomposed re-entry is handled in :meth:`_run_phases`).
        A task with no flow checkpoint (interrupted before the engine started, or a flow whose
        fingerprint no longer matches) restarts from the top. Side-effect idempotency (commit/push/
        PR) lives in ``publish_operations``, so a resumed run never duplicates them."""
        snapshot = self._flow_registry.resolve(None)
        run_state = hydrate_run_state(self._store, p.task.id)
        if run_state is None or run_state.flow_fingerprint != snapshot.flow_fingerprint:
            # No usable checkpoint (interrupted before the engine wrote one, or the flow changed) →
            # restart from the top via the full driver (re-does preflight + branch prep + engine).
            if p.status not in (Status.NEW, Status.PENDING, Status.VALIDATED):
                self._store.set_status(p.task.id, Status.VALIDATED)  # reset to re-enter the driver
                p.status = Status.VALIDATED
            return self._drive_via_engine(p, self._gate.phase_b(p.task))
        self._check_preflight(p)  # re-resolve the launchable check profile (idempotent)
        self._git.prepare_branch(p.task.id, p.slug)  # re-attach the existing branch (reused)
        return self._engine_run(p, self._gate.phase_b(p.task), resume=True, run_state=run_state)

    def _restore_engine_inputs(self, p: _Pipeline, inputs: NodeInputs) -> None:
        """Repopulate the artifact paths a resumed fixing/review node reads (port of the legacy
        ``_restore_recovery_context``): the diff, the latest failed check log, the review findings,
        and the plan — from disk + the store, scoped to the active subtask when decomposed."""
        task_dir = task_artifact_dir(self._artifacts_root, p.task.id)
        diff = task_dir / "current.diff"
        if diff.exists():
            inputs.diff_path = str(diff)
        plan = task_dir / "plan.md"
        if plan.exists():
            inputs.plan_path = str(plan)
        review = task_dir / "review" / "findings.json"
        if review.exists():
            inputs.review_path = str(review)
        # Restore the planning-selected skills (in-memory only otherwise; lost on a resume past
        # planning) so resumed edit nodes keep {skills_path} / skill_reference_paths.
        skills_file = task_dir / "selected_skills.json"
        if skills_file.exists():
            refs = tuple(
                SkillRef(name=d["name"], description=d["description"], path=d["path"])
                for d in json.loads(skills_file.read_text(encoding="utf-8"))
            )
            p.selected_skills = refs
            inputs.skill_paths = tuple(r.path for r in refs)
        # P1: the fixing-resume check log is task-scoped — a decomposed subtask re-runs its region
        # from the top (region entry), regenerating its own check log.
        latest_check = self._store.latest_failed_check_log(p.task.id, None)
        if latest_check and Path(latest_check).exists():
            inputs.checks_path = latest_check

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

    # --- pipeline (the FlowEngine is the driver) ------------------------------------------

    def _check_flow_providers(self) -> None:
        """Fatal preflight (PRE.1a): every flow node's declared ``provider`` must be in
        ``agents.allowed``.

        Runs before any branch/provider launch so a node pinned to a disallowed provider fails loud
        early, not mid-run. This is the config-aware half of flow validation that the load-time
        validator cannot do (it has no config); the full move into the validator is P4.2.
        """
        snapshot = self._flow_registry.resolve(None)
        allowed = frozenset(self._config.agents.allowed)
        bad: list[str] = []
        for node in snapshot.doc.nodes:
            if (
                isinstance(node, AgentNode | EvaluatorNode)
                and node.provider is not None
                and node.provider not in allowed
            ):
                bad.append(
                    f"node {node.id!r}: provider {node.provider.value!r} not in agents.allowed"
                )
        if bad:
            raise PipelineFailed("flow provider not allowed: " + "; ".join(bad))

    def _drive_via_engine(self, p: _Pipeline, completeness: Completeness) -> PipelineResult:
        """Drive the task through the :class:`FlowEngine`.

        Keeps the orchestrator-owned preamble (isolation + check preflight, branch prep) and the
        terminal handling (auto-merge + cleanup); the refinement→…→publish body is expressed as the
        validated flow graph and executed by the engine. Per-node post-processing (artifact slots,
        skills) runs in the post-node hook; the publish node finalizes the task file + opens the PR.
        Infra failure → ``failed``; a node needing human action → ``manual_action_required``.
        """
        self._check_flow_providers()
        if self._config.security.strict_isolation:
            reasons = check_isolation(self._config)
            if reasons:
                joined = "; ".join(reasons)
                self._log(p.task.id).warning(
                    "isolation preflight failed", extra={"reasons": joined}
                )
                raise PipelineFailed(f"strict_isolation: {joined}")
        self._check_preflight(p)
        self._transition(p, Status.PREPARING)
        self._prepare_branch(p)
        self._transition(p, Status.RUNNING)
        return self._engine_run(p, completeness, resume=False)

    def _engine_run(
        self,
        p: _Pipeline,
        completeness: Completeness,
        *,
        resume: bool,
        run_state: FlowRunState | None = None,
    ) -> PipelineResult:
        """Build the node services/inputs and drive the flow (fresh or resumed). The preamble
        (preflight, branch) + terminal handling live in the callers; this is the engine core."""
        snapshot = self._flow_registry.resolve(None)  # P1: every task → the implementation flow
        assert snapshot.source_path is not None
        if run_state is None:
            run_state = FlowRunState(flow_fingerprint=snapshot.flow_fingerprint)
        # The constant supervisor layer starts at task start and lives the whole cycle (P2.1); it
        # carries this task's own resume_own_lineage session.
        self._supervisor = self._build_supervisor(snapshot)
        inputs = build_node_inputs(
            p,
            flow_dir=snapshot.source_path.parent,
            resolved_checks=self._resolved_checks(p),  # None → CheckRunner uses checks.commands
            pr_title=p.task.pr_title or p.task.title,
            commit_message=f"feat({p.task.id}): {p.task.title}",
            summary_body_path=self._fallback_summary_path(p),
        )
        if resume:
            self._restore_engine_inputs(p, inputs)  # diff/checks/review/plan paths from disk
        services = build_node_services(
            router=self._router,
            check_runner=self._checks,
            store=self._store,
            repo_dir=self._config.repo.local_path,
            artifacts_root=str(self._artifacts_root),
            snapshot=snapshot,
            clock=self._clock,
            git=self._git,
            notifier=self._notifier,
            snapshot_hook=self._git,
            ask_timeout_s=self._config.telegram.ask_timeout_s,
            prompt_audit=self._prompt_audit_on(p.task),
            prompt_secrets=self._prompt_secrets(),
            register_artifact=self._register_artifact,
            finalize=lambda: self._engine_finalize(p),
            check_reresolve=lambda: self._engine_check_reresolve(p),
        )
        recorder = StateStoreRunRecorder(
            self._store, p.task.id, artifacts_root=self._artifacts_root
        )
        try:
            result = self._run_phases(
                p, snapshot, run_state, recorder, services, inputs, completeness, resume=resume
            )
        except NodeManualRequired as exc:
            self._sync_counters_from_run_state(p, run_state)
            return self._go_terminal(p, Status.MANUAL_ACTION_REQUIRED, manual_reason=str(exc))
        except NodeInfraError as exc:
            self._sync_counters_from_run_state(p, run_state)
            return self._fail(p, str(exc))
        self._sync_counters_from_run_state(p, run_state)
        return self._finish_engine_run(p, result)

    def _sync_counters_from_run_state(self, p: _Pipeline, run_state: FlowRunState) -> None:
        """Mirror the engine's authoritative loop counters into the operator-facing LoopCounters.

        The engine owns counting in ``FlowRunState.loop_counters``; the legacy ``tasks`` counter
        columns back the operator surfaces (the ledger ``_append_ledger``, CLI ``status`` via
        ``get_counters``, ``finalize``). Syncing here before the terminal transition keeps them from
        reading 0 after the engine ran fix loops. The global ``fix_iterations`` is generic; the
        named loop mirrors apply to the implementation flow's ``test_fix`` / ``review_fix`` loops.
        """
        p.counters = replace(
            p.counters,
            fix_iterations=run_state.fix_iterations,
            test_fix_cycles=run_state.counter("test_fix"),
            review_fix_cycles=run_state.counter("review_fix"),
        )

    def _run_phases(
        self,
        p: _Pipeline,
        snapshot: FlowSnapshot,
        run_state: FlowRunState,
        recorder: StateStoreRunRecorder,
        services: NodeServices,
        inputs: NodeInputs,
        completeness: Completeness,
        *,
        resume: bool = False,
    ) -> FlowRunResult:
        """Drive the flow in phases. Fresh: a flow with no decomposition runs in one pass; a
        decomposed one runs pre (entry…proposed_by) once, the sub_flow region once per subtask
        (commit between), then post once. Resume: continue from the hydrated ``current_node`` — a
        run still in ``pre`` re-runs pre (planning re-decides), a single-unit run continues from
        ``current_node``, and a decomposed run re-enters the active uncommitted subtask at the
        region entry (committed subtasks are skipped, never re-committed)."""
        post_node = self._engine_post_node(p, inputs, snapshot)
        facts = self._engine_facts(p, completeness)
        if resume and run_state.current_node is None:
            resume = False  # no checkpoint position to resume from → start fresh

        def phase(
            entry: str, region: frozenset[str] | None, subtask: int | None = None
        ) -> FlowRunResult:
            run_state.current_node = entry  # seed BEFORE the phase entry node runs (resume-safe)
            recorder.save_checkpoint(run_state)
            return drive_flow(
                snapshot=snapshot,
                run_state=run_state,
                recorder=recorder,
                services=services,
                inputs=inputs,
                facts=facts,
                agents=self._config.agents,
                task_id=p.task.id,
                post_node=post_node,
                subtask_order=subtask,
                region=region,
            )

        if snapshot.doc.decomposition is None:
            entry = run_state.current_node if resume else entry_node_id(snapshot)
            assert entry is not None
            return phase(entry, None)  # whole graph in one pass (or continue from current_node)

        regions = partition_decomposition(snapshot)
        current = run_state.current_node
        in_pre = resume and current is not None and current in regions.pre
        if not resume or in_pre:
            # Run pre (fresh from entry) or re-run it (resume still in pre, planning unfinished);
            # planning's post_node sets p.decomposition.
            pre = phase(current if in_pre else entry_node_id(snapshot), regions.pre)  # type: ignore[arg-type]
            if pre.status is not Status.DONE:
                return pre
        if not p.decomposition.accepted:
            # Single unit: fresh / resumed-from-pre start at the region entry; a resume already past
            # pre continues from current_node.
            if resume and not in_pre and current is not None:
                return phase(current, None)
            return phase(regions.region_entry, None)
        return self._fan_out_subtasks(p, run_state, regions, phase, inputs)

    def _fan_out_subtasks(
        self,
        p: _Pipeline,
        run_state: FlowRunState,
        regions: DecompositionRegions,
        phase: Callable[..., FlowRunResult],
        inputs: NodeInputs,
    ) -> FlowRunResult:
        """Run the sub_flow region once per subtask (commit each, reset per-subtask counters), then
        the post-region phase. A subtask with a verified commit is never re-run (recovery, §13).

        Before each subtask's region runs, the active immutable spec is injected as
        ``inputs.subtask_spec_path`` so the edit nodes' ``{subtask_spec_path}`` (plus
        ``{subtask_order}`` / ``{subtask_count}``) scopes them to that one subtask. The post-region
        phase is whole-task again, so the spec path is cleared first."""
        units = list(p.decomposition.subtasks)
        # Decomposition is decided during planning (after `inputs` was built), so the count was None
        # at build time; surface it now for the edit nodes' "subtask N of M" context.
        inputs.subtask_count = p.decomposition.n
        committed = {s.order for s in self._store.get_subtasks(p.task.id) if s.commit_sha}
        for index, unit in enumerate(units):
            if unit.order in committed:
                continue
            inputs.subtask_spec_path = str(
                subtask_spec_path(self._artifacts_root, p.task.id, unit.order, unit.slug)
            )
            sub = phase(regions.region_entry, regions.region, subtask=unit.order)
            if sub.status is not Status.DONE:
                return sub
            self._commit_subtask(p, unit)
            if index != len(units) - 1:
                run_state.reset_for_next_subtask()  # fresh per-loop budgets; global accumulates
                self._store.update_task(p.task.id, active_subtask=unit.order + 1)
        inputs.subtask_spec_path = None  # post-region phase is whole-task, not subtask-scoped
        return phase(regions.post_entry, None)

    def _commit_subtask(self, p: _Pipeline, unit: SubtaskSpec) -> None:
        """Commit one completed subtask + persist its SHA (legacy ``_on_review_passed`` parity)."""
        message = f"feat({p.task.id}): subtask {unit.order:02d} {unit.title}"
        sha = self._git.commit_subtask(p.task.id, unit.order, unit.slug, message)
        update_subtask_index(self._artifacts_root, p.task.id, unit.order, status="committed",
                             commit_sha=sha)
        self._store.set_subtask_commit(p.task.id, unit.order, sha, "committed")
        self._store.update_task(p.task.id, subtasks_completed=unit.order)

    def _build_supervisor(self, snapshot: FlowSnapshot) -> Supervisor:
        """Construct the per-task supervisor layer from ``config.yaml: supervisor`` (P2.1).

        It runs read-only on the global primary; ``role_file`` is resolved inside the packaged flow
        dir (same containment as a node ``role_file``). Built fresh per task so its own session does
        not leak across tasks.
        """
        assert snapshot.source_path is not None
        return Supervisor(
            settings=self._config.supervisor,
            router=self._router,
            store=self._store,
            repo_dir=self._config.repo.local_path,
            artifacts_root=str(self._artifacts_root),
            flow_dir=snapshot.source_path.parent,
            register_artifact=self._register_artifact,
        )

    def _engine_finalize(self, p: _Pipeline) -> str | None:
        """The publish node's finalize hook: write the supervisor summary, move the task file, and
        write the committed summary (P2.1).

        The constant supervisor layer synthesizes ``summary.{md,json}`` at whole-task close (before
        publish, so the ``summary.md`` is the PR body); ``_finalize_task_artifacts`` then falls back
        to the deterministic minimal summary when the advisory synthesis could not run — so a
        summary is *always* written, one way or the other (``config.summary_enabled`` is gone)."""
        if self._supervisor is not None:
            self._supervisor.finalize(task_id=p.task.id, task_title=p.task.title)
        self._append_skip_section(p)  # note skipped stages on the supervisor summary (idempotent)
        summary_md = self._finalize_task_artifacts(p, Status.DONE)
        return str(summary_md) if summary_md is not None else None

    def _engine_check_reresolve(self, p: _Pipeline) -> tuple[ResolvedCheck, ...] | None:
        """The checks node's re-resolve hook: re-resolve once on a launch failure (gated)."""
        if self._reresolve_on_launch_failure(p) and p.check_profile is not None:
            return p.check_profile.checks
        return None

    def _engine_facts(
        self, p: _Pipeline, completeness: Completeness
    ) -> Callable[[str], bool]:
        """Resolve a flow ``when`` fact (``derived.*`` / ``config.*_enabled``) to a boolean."""
        # Refinement-skip is deterministic — driven purely by completeness classification, never a
        # task flag (PRE.3): a ``complete`` task skips refinement, anything else runs it (§5).
        needs_refinement = completeness is not Completeness.COMPLETE

        def facts(fact: str) -> bool:
            if fact == "derived.needs_refinement":
                return needs_refinement
            if fact.startswith("config.") and fact.endswith("_enabled"):
                name = fact[len("config.") : -len("_enabled")]
                try:
                    return Stage(name) not in p.skip
                except ValueError:
                    return True  # unknown stage-enabled fact → do not skip
            return False  # unknown fact → default off

        return facts

    def _engine_post_node(
        self, p: _Pipeline, inputs: NodeInputs, snapshot: FlowSnapshot
    ) -> Callable[[FlowNode, NodeOutcome, int], None]:
        """Engine post-node hook: let the supervisor layer observe the completed step, persist a
        node's output_artifact slot, resolve plan skills, and — for the decomposition
        ``proposed_by`` node — decide + materialize the decomposition."""
        decomp = snapshot.doc.decomposition

        def post_node(node: FlowNode, outcome: NodeOutcome, node_run_id: int) -> None:
            # The constant supervisor layer observes every completed step read-only (advisory) —
            # except the terminal publish node, whose finalize hook already wrote the summary.
            if self._supervisor is not None and node.kind != "publish":
                self._supervisor.observe(
                    task_id=p.task.id,
                    node_id=node.id,
                    node_run_id=node_run_id,
                    outcome_kind=outcome.kind,
                    final_message=outcome.final_message,
                )
            if not isinstance(node, AgentNode):
                return
            path = apply_output_artifact(
                node,
                outcome,
                artifacts_root=self._artifacts_root,
                task_id=p.task.id,
                inputs=inputs,
                register=self._register_artifact,
            )
            if node.output_artifact == "plan" and path is not None:
                self._engine_apply_skills(p, outcome, inputs, path)
            if decomp is not None and node.id == decomp.proposed_by:
                self._engine_materialize_decomposition(p, outcome)

        return post_node

    def _engine_materialize_decomposition(self, p: _Pipeline, outcome: NodeOutcome) -> None:
        """Decide decomposition from the proposed_by node's contract, persist it + write the subtask
        specs/rows (legacy ``_planning`` block, triggered by data not the stage name)."""
        gate_on = self._decomposition_gate_on()
        decision = read_decomposition(
            outcome, gate_on=gate_on, max_subtasks=self._config.agents.decomposition.max_subtasks
        )
        p.decomposition = decision
        self._store.update_task(
            p.task.id,
            decomposition_enabled=gate_on,
            decomposition_accepted=decision.accepted,
            decomposition_reason=decision.reason,
            subtask_count=decision.n if decision.accepted else None,
            active_subtask=1 if decision.accepted else None,
        )
        if not decision.accepted:
            return
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

    def _engine_apply_skills(
        self, p: _Pipeline, outcome: NodeOutcome, inputs: NodeInputs, plan_path: str
    ) -> None:
        """Resolve planning-proposed skills, surface them downstream, append the plan section."""
        structured = outcome.structured_output
        skills_raw = structured.get("skills") if isinstance(structured, Mapping) else None
        proposed = (
            tuple(str(s) for s in skills_raw) if isinstance(skills_raw, list | tuple) else ()
        )
        selection = resolve_planning_skills(proposed, p.skill_inventory)
        p.selected_skills = selection.refs
        inputs.skill_paths = tuple(ref.path for ref in selection.refs)
        self._persist_selected_skills(p, selection.refs)
        section = self._render_skill_section(selection, ())
        if section:
            existing = Path(plan_path).read_text(encoding="utf-8")
            Path(plan_path).write_text(existing + section, encoding="utf-8")

    def _persist_selected_skills(self, p: _Pipeline, refs: tuple[SkillRef, ...]) -> None:
        """Persist the planning-selected skills so a resume past planning can restore them (§2.1).

        ``p.selected_skills`` is in-memory only (set when planning runs this process), so without
        this a resumed implementation/fixing node would lose ``{skills_path}`` /
        ``skill_reference_paths``. The fresh run still embeds the skills in plan.md; this restores
        the dedicated reference-path channel (see :meth:`_restore_engine_inputs`)."""
        task_dir = task_artifact_dir(self._artifacts_root, p.task.id)
        data = [{"name": r.name, "description": r.description, "path": r.path} for r in refs]
        path = task_dir / "selected_skills.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self._register_artifact(p.task.id, "selected_skills", str(path))

    def _finish_engine_run(self, p: _Pipeline, result: FlowRunResult) -> PipelineResult:
        """Map a terminal :class:`FlowRunResult` to a :class:`PipelineResult` (+ auto-merge)."""
        if result.status is Status.DONE:
            pr_url = self._git.recorded_pr_url(p.task.id)
            if pr_url and Stage.REVIEW in p.skip and self._auto_merge_on(p.task):
                self._log(p.task.id).warning(
                    "[AUTO-MERGE] review skipped AND auto_merge enabled — task will merge without "
                    "any review gate",
                    extra={"pr_url": pr_url},
                )
            if pr_url and self._auto_merge_on(p.task):
                return self._auto_merge(p, pr_url)
            return self._go_terminal(p, Status.DONE, pr_url=pr_url, already_moved=True)
        if result.status is Status.MANUAL_ACTION_REQUIRED:
            return self._go_terminal(
                p, Status.MANUAL_ACTION_REQUIRED, manual_reason=result.limit_name or "stuck"
            )
        return self._fail(p, result.limit_name or "flow run failed")

    def _check_preflight(self, p: _Pipeline) -> None:
        """Resolve the launchable check profile at task start, before any branch (§11, §1.2).

        Skipped when no resolver is wired (legacy behavior: the Check Runner uses
        ``checks.commands``). A non-ready profile raises :class:`PipelineFailed` — the task fails
        before a branch is created and without consuming any fix iteration. With
        ``checks.discovery.run_at_task_start`` (default on), ``auto`` mode may run the opt-in agent
        fallback here (the resolver still gates it on mode + agent_fallback + a configured model). A
        *changed* set of check commands goes through the sensitive-change approval gate (§1.2).
        """
        if self._resolver is None:
            return
        prev = self._resolver.store.load()
        prev_approved_sig = prev.commands_signature if (prev and prev.approved) else ""
        allow_agent = self._config.checks.discovery.run_at_task_start
        profile = self._resolver.resolve(allow_agent=allow_agent)
        profile = self._gate_check_commands(p, profile, prev_approved_sig)
        p.check_profile = profile
        if profile.ready:
            self._log(p.task.id).info(
                "check preflight ready",
                extra={"source": profile.source.value, "checks": len(profile.checks)},
            )
            return
        self._log(p.task.id).warning(
            "check preflight failed", extra={"source": profile.source.value}
        )
        raise PipelineFailed(
            "check preflight: no launchable check profile could be resolved "
            "(set checks.commands, or use checks.discovery.mode to detect them)"
        )

    def _gate_check_commands(
        self, p: _Pipeline, profile: ResolvedCheckProfile, prev_approved_sig: str
    ) -> ResolvedCheckProfile:
        """Approve the *set* of check commands when it changed (§1.2). Returns an approved profile.

        First-ever resolution for a repo is auto-approved and recorded (approval is for a *change*,
        not for the first ever set); an unchanged set is reused; a changed set requires human
        approval (fail-closed on deny/timeout/no-notifier) unless the operator disabled the gate.
        """
        if not profile.ready or not profile.checks:
            return (
                profile  # nothing to run → no command set to approve (readiness handled by caller)
            )
        sig = profile.commands_signature
        if profile.approved and sig == prev_approved_sig:
            return profile  # already approved this exact set (cache reuse)
        if sig == prev_approved_sig and prev_approved_sig:
            return self._stamp_check_approval(profile, "reuse")  # same set as last approved
        if not prev_approved_sig:
            return self._stamp_check_approval(
                profile, "bootstrap"
            )  # first-ever → record, no prompt
        # The command set CHANGED from a previously approved set — a sensitive change (§1.2).
        if not self._config.checks.discovery.approve_command_changes:
            self._log(p.task.id).warning(
                "check command set changed; approval gate disabled by config",
                extra={"signature": sig},
            )
            return self._stamp_check_approval(profile, "approval-disabled")
        if not self._ask_check_command_approval(p, profile):
            raise ManualActionRequired(
                "the set of check commands changed and the change was not approved"
            )
        return self._stamp_check_approval(profile, discovery_interaction_id(p.task.id, sig))

    def _stamp_check_approval(
        self, profile: ResolvedCheckProfile, interaction: str
    ) -> ResolvedCheckProfile:
        """Record the approval on the profile and persist it (the audit trail, §1.2)."""
        approved = replace(
            profile,
            approved=True,
            approved_at=self._clock(),
            approved_interaction_id=interaction,
        )
        if self._resolver is not None:
            self._resolver.store.save(approved)
        return approved

    def _ask_check_command_approval(self, p: _Pipeline, profile: ResolvedCheckProfile) -> bool:
        """Ask the human to approve a changed check-command set; fail-closed (§1.2).

        Reuses the durable HITL interaction machinery under a discovery-specific artifact. On a
        restart it resumes a still-waiting interaction for the *same* command set; a denial,
        timeout, transport error, or absent notifier raises :class:`ManualActionRequired`.
        """
        path = discovery_interaction_path(self._artifacts_root, p.task.id)
        sig = profile.commands_signature
        wanted = discovery_interaction_id(p.task.id, sig)
        persisted = load_interaction(path)
        if persisted is not None and persisted.get("interaction_id") == wanted:
            status = str(persisted.get("status", ""))
            if status in {"answered", "consumed"}:
                return persisted.get("approved") is True
            if status == "waiting":
                handle = handle_from_artifact(persisted)
                result = self._notifier.wait_for_answer(handle)
                write_answer(path, result)
                self._register_artifact(p.task.id, "hitl", str(path))
                self._require_human_result(p, Stage.PLANNING, "approval", result)
                if result.approved is True:
                    mark_consumed(path)
                return result.approved is True

        commands = "\n".join(f"{c.name}: {' '.join(c.argv)}" for c in profile.checks)
        signal = HumanInputSignal(
            kind="approval",
            question="The set of quality-gate check commands changed. Approve running them?",
            context=f"Resolved check commands:\n{commands}",
            risk="other",
            paths=tuple(c.name for c in profile.checks),
        )
        handle = self._notifier.start_ask(
            question=signal.question,
            context=signal.context,
            task_id=p.task.id,
            kind="approval",
            timeout_s=self._config.telegram.ask_timeout_s,
            interaction_id=wanted,
            contacts=tuple(p.task.contacts),
        )
        write_waiting_interaction(
            path,
            task_id=p.task.id,
            stage=Stage.PLANNING,
            subtask=None,
            signal=signal,
            handle=handle,
        )
        self._register_artifact(p.task.id, "hitl", str(path))
        result = self._notifier.wait_for_answer(handle)
        write_answer(path, result)
        self._register_artifact(p.task.id, "hitl", str(path))
        self._require_human_result(p, Stage.PLANNING, "approval", result)
        if result.approved is True:
            mark_consumed(path)
        return result.approved is True

    def _reresolve_on_launch_failure(self, p: _Pipeline) -> bool:
        """Re-resolve the check commands once after a *launch* failure (§1.2).

        Returns ``True`` when a new, different, ready profile is now active (so checks can be
        re-run), else ``False`` (the caller then fails the task). Bounded to once per task. Only an
        infrastructure launch failure reaches here — never a quality failure. A changed command set
        is routed through the same sensitive-change approval gate (fail-closed on denial). In
        ``configured`` mode the re-resolve yields the same commands, so this is a no-op (the
        operator's commands are their responsibility).
        """
        if self._resolver is None or p.reresolved_once:
            return False
        p.reresolved_once = True
        prev_sig = p.check_profile.commands_signature if p.check_profile else ""
        prev_approved_sig = (
            p.check_profile.commands_signature
            if (p.check_profile and p.check_profile.approved)
            else ""
        )
        allow_agent = self._config.checks.discovery.run_at_task_start
        new_profile = self._resolver.reresolve(
            allow_agent=allow_agent, reason=ReResolveReason.LAUNCH_FAILED
        )
        if not new_profile.ready or not new_profile.checks:
            return False
        if new_profile.commands_signature == prev_sig:
            return False  # same set re-resolved → re-running would launch-fail identically
        gated = self._gate_check_commands(p, new_profile, prev_approved_sig)
        p.check_profile = gated
        self._log(p.task.id).info(
            "checks re-resolved after launch failure",
            extra={"source": gated.source.value, "checks": len(gated.checks)},
        )
        return True

    def _prepare_branch(self, p: _Pipeline) -> None:
        """Complete the persisted ``preparing`` checkpoint and attach the task branch."""
        # Guarantee the `.worc/` runtime home is gitignored in this clone, regardless of how it was
        # scaffolded, so it never leaks into the operator's git status (no branch exists yet).
        self._git.ensure_runtime_excludes()
        p.slug = slugify(p.task.title)
        p.branch = self._observe(
            p,
            "branch preparation",
            lambda: self._git.prepare_branch(p.task.id, p.slug),
        )
        self._store.update_task(p.task.id, branch=p.branch, slug=p.slug)

    def _render_skill_section(
        self, selection: SkillSelection, dedup: tuple[SkillDedupEntry, ...]
    ) -> str:
        """Render the deterministic, auditable plan.md skills block (§2.1/§2.2)."""
        if not (selection.refs or selection.dropped_unknown or selection.dropped_excluded):
            return ""
        lines = ["", "## Skills (planning-selected, read-only references)", ""]
        if selection.refs:
            lines += [f"- `{ref.name}`: {ref.path}" for ref in selection.refs]
        else:
            lines.append("- (none)")
        dropped = [f"`{n}` (not in the repo skill inventory)" for n in selection.dropped_unknown]
        dropped += [
            f"`{n}` (gate-duplicating; owned by the orchestrator)"
            for n in selection.dropped_excluded
        ]
        if dropped:
            lines += ["", "Dropped: " + "; ".join(dropped) + "."]
        if dedup:
            lines += [
                "",
                "De-duplication — your appended planning instructions take precedence; these "
                "referenced skill sections cover the same topics:",
            ]
            lines += [f"- `{e.skill}`: {', '.join(e.overlapping_headings)}" for e in dedup]
        return "\n" + redact_text("\n".join(lines) + "\n")

    def _skip_section_md(self, p: _Pipeline) -> str:
        """A ``## Pipeline stages skipped`` markdown block, or ``""`` when nothing was skipped."""
        if not p.skip:
            return ""
        lines = "\n".join(f"- `{s.value}`" for s in sorted(p.skip, key=lambda s: s.value))
        return f"\n## Pipeline stages skipped\n\n{lines}\n"

    def _append_skip_section(self, p: _Pipeline) -> None:
        """Append the skipped-stages section to ``summary.md`` (idempotent within a run)."""
        section = self._skip_section_md(p)
        if not section:
            return
        md_path = task_artifact_dir(self._artifacts_root, p.task.id) / "summary.md"
        if not md_path.exists():
            return
        existing = md_path.read_text(encoding="utf-8")
        if "## Pipeline stages skipped" in existing:
            return
        md_path.write_text(existing.rstrip("\n") + "\n" + section, encoding="utf-8")

    def _auto_merge(self, p: _Pipeline, pr_url: str) -> PipelineResult:
        """Merge the just-created PR, bypassing human review. Audited, idempotent, non-destructive.

        A blocked merge (branch protection / pending checks / conflict) raises ManualActionRequired,
        so the task ends ``manual_action_required`` with the PR left open — never FAILED, never a
        force-merge, never ``--admin``. Idempotent on restart via the ``pr_merge`` publish op.
        """
        git = self._config.git
        self._log(p.task.id).warning(
            "[AUTO-MERGE] merging PR without human review",
            extra={
                "pr_url": pr_url,
                "strategy": git.auto_merge_strategy.value,
                "wait_for_checks": git.auto_merge_wait_for_checks,
            },
        )
        try:
            outcome = self._observe(
                p,
                "auto-merge",
                lambda: self._git.merge_pr(
                    p.task.id,
                    pr_url,
                    strategy=git.auto_merge_strategy,
                    wait_for_checks=git.auto_merge_wait_for_checks,
                ),
            )
        except GitCommandError as exc:
            raise ManualActionRequired(f"auto-merge blocked: {exc}") from exc
        return self._go_terminal(
            p, Status.DONE, pr_url=pr_url, already_moved=True, merge_outcome=outcome
        )

    def _fallback_summary_path(self, p: _Pipeline) -> str:
        """The logs/ working copy of summary.md — PR body fallback when no task file is on disk."""
        return str(task_artifact_dir(self._artifacts_root, p.task.id) / "summary.md")

    def _summary_md_body(self, p: _Pipeline) -> str:
        """The human-readable summary text; falls back to a deterministic minimal summary (§5.2)."""
        md_path = task_artifact_dir(self._artifacts_root, p.task.id) / "summary.md"
        if not md_path.exists():
            write_minimal_summary(
                self._artifacts_root,
                p.task.id,
                title=p.task.title,
                diff_stat=self._git.diff_stat(),
                task_ref=self._task_ref(p),
            )
            self._append_skip_section(p)
        return md_path.read_text(encoding="utf-8") if md_path.exists() else (p.task.title + "\n")

    def _task_ref(self, p: _Pipeline) -> str | None:
        """A short sibling-relative pointer to the task file for the committed summary (§5.2).

        The committed ``<id>.summary.md`` lives next to the moved ``<id>.md`` task file, so the
        basename is the correct, move-independent reference. ``None`` for a synthetic ``run`` path.
        """
        return Path(p.task_file).name if p.task_file else None

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
        merge_outcome: str | None = None,
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
        # The flow checkpoint marks where ``rerun --continue`` re-enters — meaningful only for a
        # non-success terminal. A ``done`` task has no resume position, so clear it (``node_runs``
        # stay for the audit trail); otherwise keep ``current_node`` for the operator to continue.
        if final is Status.DONE:
            self._store.update_task(
                p.task.id, current_node=None, flow_run_counters=None, flow_fingerprint=None
            )
        self._transition(p, final, finished_at=self._clock())
        if not already_moved:
            self._move_task_file(p, final)
        self._append_ledger(
            p, final, pr_url=pr_url, cleanup_safe=cleanup.safe, merge_outcome=merge_outcome
        )
        self._notify_terminal(
            task_id=p.task.id,
            final_status=final,
            pr_url=pr_url,
            reason=manual_reason,
            contacts=tuple(p.task.contacts),
        )
        self._log(p.task.id).info(
            "terminal",
            extra={"final_status": final.value, "pr_url": pr_url, "cleanup_safe": cleanup.safe},
        )
        return PipelineResult(task_id=p.task.id, final_status=final, pr_url=pr_url)

    def _move_task_file(self, p: _Pipeline, final: Status) -> Path | None:
        """Move the task file to its lifecycle folder; see _relocate_task_file (§20.2)."""
        return self._relocate_task_file(p.task_file, p.task.id, final)

    def _relocate_task_file(
        self, task_file: str | None, task_id: str, final: Status
    ) -> Path | None:
        """Move a task file into its lifecycle folder (``tasks/done`` / ``tasks/failed``, §20.2).

        Pipeline-free (used by both the pipeline's `_move_task_file` and the operator `finalize`).
        ``done`` and ``failed`` move; ``manual_action_required`` stays put for the operator to
        resolve (§8.3) — so a finalize `--as abandoned` leaves the file where it is. A §19 gate
        reject is quarantined separately (§19.4). Idempotent: returns the destination whether it
        moved now or was already in place; returns ``None`` when there is nothing to do.
        """
        folder_name = {Status.DONE: "done", Status.FAILED: "failed"}.get(final)
        if folder_name is None or not task_file:
            return None
        src = Path(task_file)
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
            self._store.update_task(task_id, source_path=str(dest))
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

    # --- human-input guards (check-command approval) --------------------------------------

    def _require_human_result(
        self,
        p: _Pipeline,
        stage: Stage,
        kind: AskKind,
        result: AskResult,
    ) -> None:
        failure = result.failure
        if failure is None and result.answered:
            if kind == "approval" and isinstance(result.approved, bool):
                return
            if kind == "question" and isinstance(result.text, str) and result.text.strip():
                return
            failure = "invalid_response"
        elif failure is None:
            failure = "invalid_response"
        self._raise_human_failure(p, stage, failure)

    def _raise_human_failure(
        self,
        p: _Pipeline,
        stage: Stage,
        failure: str,
    ) -> None:
        self._log(p.task.id).warning(
            "human input failed",
            extra={"stage": stage.value, "failure": failure},
        )
        raise ManualActionRequired(f"{stage.value} human input failed: {failure}")

    def _resolved_checks(self, p: _Pipeline) -> tuple[ResolvedCheck, ...] | None:
        """The resolved profile's checks; on resume, fall back to the cached profile (§13)."""
        if p.check_profile is not None:
            return p.check_profile.checks
        if self._resolver is None:
            return None  # legacy path: the Check Runner normalizes checks.commands
        cached = self._resolver.store.load()
        if cached is not None:
            p.check_profile = cached
            return cached.checks
        return None

    # --- artifact + logging helpers -------------------------------------------------------

    def _prompt_secrets(self) -> tuple[str, ...]:
        """Denied-read file secrets to scrub from the rendered prompt before storage (§6)."""
        return read_denied_secrets(
            self._config.repo.local_path, self._config.security.denied_read_paths
        )

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

    def _decomposition_gate_on(self) -> bool:
        """Whether decomposition is permitted at all (PRE.3): the config default. The flow's
        ``decomposition:`` block + the planning node's gate proposal decide whether a split happens;
        a clean task carries no decompose flag."""
        return self._config.agents.decomposition.enabled

    def _prompt_audit_on(self, task: NormalizedTask) -> bool:
        """Resolve the effective prompt-audit decision: the task value always overrides the global.

        A per-task ``prompt_audit: true``/``false`` is honored verbatim; absent (None) defers to the
        global ``config.prompt_audit``. There is no operator gate — unlike auto-merge, recording a
        prompt is not a privilege escalation.
        """
        if task.prompt_audit is True:
            return True
        if task.prompt_audit is False:
            return False
        return self._config.prompt_audit

    def _auto_merge_on(self, task: NormalizedTask) -> bool:
        """Resolve the effective auto-merge decision (DANGER: bypasses human review).

        The task value wins outright (PRE.2): an explicit per-task ``True``/``False`` is honored
        verbatim; absent (``None``) defers to the instance default ``git.auto_merge``. Auto-merge
        is a publishing-policy choice owned by the operator (the same trusted author as the config),
        not a sandbox/approvals ceiling, so there is no separate operator gate. See
        ``docs/operations.md``: skipping the human PR review is the operator's call.
        """
        if task.auto_merge is not None:
            return task.auto_merge
        return self._config.git.auto_merge

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
        contacts: tuple[str, ...] = (),
    ) -> None:
        """Best-effort terminal notification (§4.7). Never raises and never alters the outcome."""
        try:
            self._notifier.send_notification(
                task_id=task_id,
                final_status=final_status.value,
                pr_url=pr_url,
                reason=reason,
                contacts=contacts,
            )
        except Exception as exc:  # noqa: BLE001 — notifier is best-effort by contract
            self._log(task_id).warning(
                "terminal notification failed", extra={"error_type": type(exc).__name__}
            )

    def _append_ledger(
        self,
        p: _Pipeline,
        final: Status,
        *,
        pr_url: str | None,
        cleanup_safe: bool,
        merge_outcome: str | None = None,
    ) -> None:
        task_row = self._store.get_task(p.task.id)
        attempt = self._rerun_attempt.get(p.task.id, 1)
        self._ledger.append(
            LedgerRecord(
                id=p.task.id,
                title=p.task.title,
                branch=p.branch or None,
                pr_url=pr_url,
                final_status=final.value,
                auto_merged=merge_outcome is not None,
                merge_outcome=merge_outcome,
                fix_iterations=p.counters.fix_iterations,
                terminal_cleanup="completed" if cleanup_safe else "blocked",
                finished_at=self._clock(),
                failure_report=task_row.failure_report_path if task_row else None,
                decomposed=p.decomposition.accepted,
                subtask_count=p.decomposition.n if p.decomposition.accepted else None,
                subtasks_completed=task_row.subtasks_completed if task_row else None,
                attempt=attempt,
                rerun_of=p.task.id if attempt > 1 else None,
            )
        )


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
    is_recovery_rerun: Callable[[str], bool] = lambda _id: False,
) -> Orchestrator:
    """Wire the full dependency graph from a validated config (used by the CLI and e2e tests).

    Constructs the real provider adapters, Router, State Store (``<artifacts_root>/state.db``),
    ledger (``<artifacts_root>/logs/completed.jsonl``), Git Manager, Check Runner, loop controller,
    and validation gate. The Core depends only on these interfaces — never on a provider directly.

    ``is_recovery_rerun`` is threaded into the §19 gate so the ``rerun`` command can admit exactly
    the re-run id past the duplicate-id check (scoped to one id; every other gate check still runs).
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
    # Agent-assisted discovery is opt-in: build_discovery returns None unless agent_fallback + a
    # cheap model are configured, so default runs stay deterministic (§1.2).
    discovery = build_discovery(config, providers, str(root))
    resolver = CheckResolver(
        config, repo_root=config.repo.local_path, artifacts_root=str(root), discovery=discovery
    )
    gate = ValidationGate(
        config,
        store_has_task_id=store.task_id_exists,
        ledger_has_task_id=ledger.has_task_id,
        is_recovery_rerun=is_recovery_rerun,
    )
    notifier = build_notifier(config.telegram)
    return Orchestrator(
        config,
        router=router,
        git=git,
        checks=checks,
        store=store,
        ledger=ledger,
        gate=gate,
        artifacts_root=str(root),
        notifier=notifier,
        resolver=resolver,
    )
