"""The deterministic Orchestrator Core pipeline.

Drives one task end to end: validation gate → slot → branch → refinement (deterministic skip) →
planning (+ decomposition) → per-unit [implementation → testing → review → fixing] → summary →
publishing → terminal cleanup → ledger. The Core **never** builds a CLI command — it calls only the
Agent Router for agent stages, the Check Runner for ``testing``, and the Git Manager for everything
that touches git. Context is handed to agents **only as artifact file paths** on the request.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from wastech_orchestrator.check_runner import CheckRunner
from wastech_orchestrator.checks.model import ResolvedCheckSet
from wastech_orchestrator.checks.resolver import CheckResolver
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.decomposition import (
    REASON_N_OUT_OF_RANGE,
    DecompositionDecision,
    SubtaskSpec,
    decide_operator_decomposition,
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
    EvaluatorInfraError,
    NodeInfraError,
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.output_policy import is_within
from wastech_orchestrator.core.flow.postprocess import apply_output_artifact, read_decomposition
from wastech_orchestrator.core.flow.recorder import StateStoreRunRecorder, hydrate_run_state
from wastech_orchestrator.core.flow.registry import FlowRegistry, FlowResolutionError
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import AgentNode, FlowNode
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.flow.validator import (
    FlowValidationError,
    validate_disabled_nodes,
)
from wastech_orchestrator.core.flow.wiring import build_node_inputs, build_node_services
from wastech_orchestrator.core.hitl import (
    consume_pending_interactions,
    reset_pending_interactions,
)
from wastech_orchestrator.core.loop_control import LoopCounters
from wastech_orchestrator.core.node_overrides import resolve_node_overrides
from wastech_orchestrator.core.recovery import (
    RecoveryAction,
    RecoveryPlan,
    RecoveryReconciler,
)
from wastech_orchestrator.core.skills import (
    SkillInventory,
    SkillInventoryScanner,
    SkillRef,
    SkillSelection,
    resolve_skills,
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
    write_failure_report,
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
    TRANSIENT_RETRYABLE,
    AgentProvider,
    ProviderId,
)
from wastech_orchestrator.providers.redaction import read_denied_secrets
from wastech_orchestrator.routing.router import AgentRouter
from wastech_orchestrator.security.env import build_child_env
from wastech_orchestrator.security.isolation import check_isolation
from wastech_orchestrator.state_store import (
    ArtifactRow,
    StateStore,
    SubtaskRow,
    TaskRow,
)
from wastech_orchestrator.task.model import NormalizedTask
from wastech_orchestrator.task.parser import (
    SubtaskSpecFile,
    load_normalized,
    read_subtask_spec,
    read_task_source,
    slugify,
    write_normalized,
)
from wastech_orchestrator.task.validation_gate import (
    Completeness,
    ValidationGate,
    ValidationReason,
    ValidationResult,
    write_validation_report,
)

_LOG = logging.getLogger(__name__)

# The gitignored runtime home holding operator flows under ``<repo>/.worc/flows/`` (P4.1). Mirrors
# ``cli.WORC_HOME``; duplicated here because the core must not import the CLI (would be circular).
_WORC_HOME = ".worc"

# The lifecycle folders a task file moves between under ``tasks/`` (registration → done/failed).
_LIFECYCLE_FOLDERS = ("pending", "processing", "done", "failed")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Map a task-level artifact filename to its registry ``kind``. Unknown names fall back to the
# filename so registration is always meaningful even if a new artifact is added.
_ARTIFACT_KINDS: dict[str, str] = {
    "task.enriched.md": "enriched",
    "plan.md": "plan",
    "fixing-context.json": "fixing_context",
    "rendered-prompt.md": "rendered_prompt",
}


@dataclass(frozen=True)
class RerunPlan:
    """The reconciled facts + refusals for a ``rerun``/``rerun --continue`` (read-only)."""

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
    """The reconciled facts + warnings/refusals for a ``finalize`` (read-only)."""

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


def effective_skip(task: NormalizedTask) -> frozenset[str]:
    """The flow node ids disabled for ``task`` — its own ``nodes.<node-id>.enabled: false``
    overrides (per-task node-disable control; the bounded per-task exception).

    The gate validated the ``nodes:`` block shape; node existence and routing soundness against the
    task's resolved flow are checked at flow resolution (``validate_disabled_nodes``), so by the
    time the engine consumes this set it is known to name real, safely-skippable nodes.
    """
    return task.disabled_nodes()


@dataclass(frozen=True)
class PipelineResult:
    """The terminal outcome of running one task."""

    task_id: str
    final_status: Status
    pr_url: str | None = None
    validation_reason: str | None = None


class SlotBusyError(Exception):
    """Raised when the single processing slot is already held by another active task."""


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
    check_sets: tuple[ResolvedCheckSet, ...] = ()  # normalized command_sets, resolved at preflight
    # Repo skill inventory discovered at task start (`git ls-files`, whole-repo). `skill_map` is the
    # effective per-node selection (operator pins ∪ the accepted supervisor proposal), keyed by node
    # id and persisted to `skill_map.json`, so a resume restores it without re-proposing (see
    # `_resolve_skill_layers`). Both are populated in `_engine_run`, not at construction.
    skill_inventory: SkillInventory = field(default_factory=SkillInventory)
    skill_map: dict[str, tuple[SkillRef, ...]] = field(default_factory=dict)
    # Per-task disabled flow node ids (``nodes.<id>.enabled: false``). Re-derived every run/resume
    # from front-matter, so a restart recovers it without persistence (node-disable control).
    skip: frozenset[str] = frozenset()
    # Operator-authored decomposition built + validated pre-slot from the task's ``subtasks:``
    # manifest (fresh run only). When set, it is materialized at preflight (before branch) and the
    # planning ``proposed_by`` post-hook does not re-read the agent's proposal. ``None`` on resume —
    # the decision is rebuilt from the persisted ``subtasks`` rows (source-agnostic).
    operator_decomposition: DecompositionDecision | None = None


class Eligibility(StrEnum):
    """A pending task's dependency-readiness verdict (``depends_on`` merge-gated scheduling).

    ``ELIGIBLE`` runs now; ``WAITING`` is a non-blocking skip (the scheduler tries other eligible
    tasks and re-evaluates next tick — including the "wait forever" case of a terminal-but-unmerged
    dependency, which is never auto-failed); ``BROKEN`` is a fail-closed terminal reject (a cycle,
    an unknown reference, or a self-reference — a malformed task, not a waiting one).
    """

    ELIGIBLE = "eligible"
    WAITING = "waiting"
    BROKEN = "broken"


@dataclass(frozen=True)
class DependencyVerdict:
    """An :class:`Eligibility` plus a human-readable reason (advisory log / reject detail)."""

    state: Eligibility
    detail: str = ""


class Orchestrator:
    """The single-slot deterministic Core. One instance drives one task at a time."""

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
        heartbeat_seconds: float = 30.0,
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
        # The orchestrator-wide ``--heartbeat-seconds`` interval (shared with providers/git/checks),
        # threaded into NodeServices so the blocking HITL human-input wait heartbeats too.
        self._heartbeat_seconds = heartbeat_seconds
        self._notifier: Notifier = notifier if notifier is not None else NullNotifier()
        # The check resolver normalizes ``checks.command_sets`` at preflight (before any branch).
        # ``None`` skips it — the Check Runner then normalizes the config itself.
        self._resolver = resolver
        # Repo skill inventory scanner. Defaults to the target repo clone's `.claude/skills`.
        self._skill_scanner = skill_scanner or self._default_skill_scanner()
        # Per-id attempt number stamped onto the next ledger record, set by ``rerun``/``continue``.
        self._rerun_attempt: dict[str, int] = {}
        # Flow registry: resolves a task's flow snapshot. Operator flows live in ``<repo>/.worc/
        # flows/`` and override packaged built-ins (P4.1); passing the config turns on the
        # config-aware validation layer (P4.2) on every resolve, including resume.
        self._flow_registry = FlowRegistry(
            operator_flows_dir=Path(config.repo.local_path) / _WORC_HOME / "flows",
            config=config,
        )
        # The constant supervisor layer (P2.1) — rebuilt per task in ``_engine_run`` (it carries the
        # task's own resume_own_lineage session). Single-slot, so one live instance at a time.
        self._supervisor: Supervisor | None = None

    @property
    def notifier(self) -> Notifier:
        """The notifier transport (Telegram or a null fallback) — read-only.

        Exposed for the CLI ``watch`` loop's next-task confirmation gate (idea 27), which asks the
        operator before claiming a pending task. The orchestration decision (claim vs skip) stays in
        the watch loop; the orchestrator only owns the transport."""
        return self._notifier

    def _max_turns_gate_enabled(self) -> bool:
        """Whether the Claude max-turns continue/stop gate is configured on (idea 29).

        Reads ``agents.providers.claude.max_turns_gate``; ``False`` when claude is not configured
        (codex-only setups never produce ``error_max_turns``). Preflight guarantees ``telegram`` is
        enabled when this is on, so a configured gate always has a live transport."""
        claude = self._config.agents.providers.get(ProviderId.CLAUDE)
        return claude is not None and claude.max_turns_gate

    def _default_skill_scanner(self) -> SkillInventoryScanner:
        return SkillInventoryScanner(
            self._config.repo.local_path,
            self._git.list_tracked_skill_files,
            denied_read_paths=self._config.security.denied_read_paths,
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

        # Operator-authored decomposition (``subtasks:``): the IO-bearing manifest validation the
        # IO-free gate cannot do — runs pre-slot so a bad manifest is quarantined to
        # ``tasks/rejected/`` with a report and never reaches a branch.
        operator_reject, operator_decision = self._validate_operator_subtasks(task, task_file)
        if operator_reject is not None:
            return self._reject(task_file, operator_reject)

        if not self.acquire_slot(task.id):
            raise SlotBusyError(f"another task is active; {task.id} must wait")

        self._register_task(task, task_file, result)
        pipeline = _Pipeline(
            task=task,
            task_file=task_file,
            status=Status.VALIDATED,
            counters=LoopCounters(),
            decomposition=DecompositionDecision(accepted=False, reason="pending", n=1),
            skip=effective_skip(task),
            operator_decomposition=operator_decision,
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
        """True iff no *other* task currently owns the processing slot."""
        return not any(t.task_id != task_id for t in self._store.find_active_tasks())

    # --- operator-authored decomposition (``subtasks:`` manifest) -------------------------

    def _validate_operator_subtasks(
        self, task: NormalizedTask, task_file: str
    ) -> tuple[ValidationResult | None, DecompositionDecision | None]:
        """Validate a root task's ``subtasks:`` manifest and build its decomposition decision.

        Runs after the (IO-free) gate but **before** slot acquisition, so a bad manifest is rejected
        before any branch. The operator supplies the *content* of the split; this resolves the
        referenced files and runs the same units gate as the agent split (``max_subtasks``, linear
        deps), so the operator cannot weaken it. Returns ``(None, None)`` for a task with no
        ``subtasks``, ``(reject, None)`` on any failure, or ``(None, decision)`` on success.
        """
        if not task.subtasks:
            return None, None
        try:
            snapshot = self._flow_registry.resolve(task.task_type)
        except (FlowResolutionError, FlowValidationError) as exc:
            return self._operator_reject(ValidationReason.FLOW_CANNOT_DECOMPOSE, str(exc))
        if snapshot.doc.decomposition is None:
            flow_name = task.task_type or "implementation"
            return self._operator_reject(
                ValidationReason.FLOW_CANNOT_DECOMPOSE,
                f"flow '{flow_name}' has no decomposition block to host a split",
            )

        root_dir = Path(task_file).resolve().parent
        slug_to_order: dict[str, int] = {}
        files: list[tuple[int, SubtaskSpecFile]] = []
        for order, ref in enumerate(task.subtasks, start=1):
            reject = self._resolve_subtask_path(ref, root_dir)
            if isinstance(reject, ValidationResult):
                return reject, None
            text = reject.read_text(encoding="utf-8")
            spec_file = read_subtask_spec(text)
            if spec_file is None:
                return self._operator_reject(
                    ValidationReason.SUBTASK_MALFORMED,
                    f"subtask file {ref!r} is malformed (need front-matter title + a body)",
                )
            if spec_file.slug in slug_to_order:
                return self._operator_reject(
                    ValidationReason.SUBTASK_MALFORMED, f"duplicate subtask slug {spec_file.slug!r}"
                )
            slug_to_order[spec_file.slug] = order
            files.append((order, spec_file))

        specs: list[SubtaskSpec] = []
        for order, spec_file in files:
            dep_orders: list[int] = []
            for dep_slug in spec_file.depends_on:
                dep_order = slug_to_order.get(dep_slug)
                if dep_order is None or dep_order >= order:
                    return self._operator_reject(
                        ValidationReason.SUBTASK_DEPENDS_FORWARD,
                        f"subtask {spec_file.slug!r} depends on {dep_slug!r}, "
                        "which is not an earlier subtask",
                    )
                dep_orders.append(dep_order)
            specs.append(
                SubtaskSpec(
                    order=order,
                    title=spec_file.title,
                    slug=spec_file.slug,
                    acceptance_criteria=spec_file.acceptance_criteria,
                    depends_on=tuple(dep_orders),
                    spec_body=spec_file.body,
                )
            )

        decision = decide_operator_decomposition(
            specs, max_subtasks=self._config.agents.decomposition.max_subtasks
        )
        if not decision.accepted:
            reason = (
                ValidationReason.SUBTASK_COUNT_OUT_OF_RANGE
                if decision.reason == REASON_N_OUT_OF_RANGE
                else ValidationReason.SUBTASK_DEPENDS_FORWARD
            )
            return self._operator_reject(reason, f"subtasks rejected: {decision.reason}")
        return None, decision

    def _resolve_subtask_path(self, ref: str, root_dir: Path) -> Path | ValidationResult:
        """Resolve one ``subtasks`` reference safely, or return a reject (fail-closed path rules).

        The path must be repo-relative (no absolute / ``..``), resolve under the root task file's
        directory, live in a **subfolder** (never beside the root, where ``select_pending`` would
        pick it up as a standalone task), and point at an existing file.
        """
        ref_path = Path(ref)
        if ref_path.is_absolute() or ".." in ref_path.parts:
            return self._operator_reject(
                ValidationReason.INVALID_SUBTASK_PATH,
                f"subtask path {ref!r} must be repo-relative with no '..'",
            )[0]
        resolved = (root_dir / ref_path).resolve()
        if not is_within(root_dir, resolved):
            return self._operator_reject(
                ValidationReason.INVALID_SUBTASK_PATH,
                f"subtask path {ref!r} escapes the task directory",
            )[0]
        if resolved.parent == root_dir:
            return self._operator_reject(
                ValidationReason.INVALID_SUBTASK_PATH,
                f"subtask {ref!r} must live in a subfolder, not beside the root task",
            )[0]
        if not resolved.is_file():
            return self._operator_reject(
                ValidationReason.SUBTASK_FILE_MISSING, f"subtask file {ref!r} not found"
            )[0]
        return resolved

    @staticmethod
    def _operator_reject(reason: ValidationReason, detail: str) -> tuple[ValidationResult, None]:
        return ValidationResult(passed=False, reason=reason, detail=detail), None

    # --- task dependencies (``depends_on`` merge-gated scheduling) -------------------------

    def dependency_eligibility(
        self,
        task_id: str,
        depends_on: Sequence[str],
        *,
        pending: Mapping[str, Sequence[str]],
    ) -> DependencyVerdict:
        """Classify a pending dependent: eligible to run / waiting (skip) / broken (reject).

        Non-blocking merge-gated scheduling: a task is **eligible** iff every id in ``depends_on``
        is **merged**. ``pending`` maps every currently-pending id to its ``depends_on`` (the
        scheduler's lightweight front-matter scan), so an unknown reference is distinguishable from
        a known-but-pending one and cross-task cycles are resolvable here (the per-task gate sees
        one task in isolation and cannot see the graph). Resolves each dependency against the store
        + ledger, probes PR merge state via the Git Manager, and backfills the real merge SHA when
        it first observes an armed PR merged. Read-only except for that SHA backfill.
        """
        if not depends_on:
            return DependencyVerdict(Eligibility.ELIGIBLE)
        if self._in_cycle(task_id, pending):
            return DependencyVerdict(Eligibility.BROKEN, f"dependency cycle involving '{task_id}'")
        verdict = DependencyVerdict(Eligibility.ELIGIBLE)
        for dep in depends_on:
            state, detail = self._resolve_dependency(dep, pending)
            if state is Eligibility.BROKEN:
                return DependencyVerdict(Eligibility.BROKEN, detail)
            if state is Eligibility.WAITING and verdict.state is Eligibility.ELIGIBLE:
                verdict = DependencyVerdict(Eligibility.WAITING, detail)
        return verdict

    def _resolve_dependency(
        self, dep: str, pending: Mapping[str, Sequence[str]]
    ) -> tuple[Eligibility, str]:
        """Resolve one dependency id to (ELIGIBLE | WAITING | BROKEN, detail)."""
        row = self._store.get_task(dep)
        if row is not None:
            if row.status is Status.DONE:
                return self._dependency_merged(dep)
            if row.status in (Status.FAILED, Status.MANUAL_ACTION_REQUIRED):
                return Eligibility.WAITING, f"dependency '{dep}' is {row.status.value} (unmerged)"
            return Eligibility.WAITING, f"dependency '{dep}' is in flight ({row.status.value})"
        if dep in pending:
            return Eligibility.WAITING, f"dependency '{dep}' is pending (not yet run)"
        if self._ledger.has_task_id(dep):
            done = any(
                rec.get("id") == dep and rec.get("final_status") == Status.DONE.value
                for rec in self._ledger.records()
            )
            if done:
                return self._dependency_merged(dep)
            return Eligibility.WAITING, f"dependency '{dep}' terminated unmerged"
        return Eligibility.BROKEN, f"depends on unknown task '{dep}'"

    def _dependency_merged(self, dep: str) -> tuple[Eligibility, str]:
        """A terminal-``DONE`` dependency: satisfied iff its PR is merged (or local-commit mode)."""
        pr_url = self._git.recorded_pr_url(dep)
        if pr_url is None:
            return Eligibility.ELIGIBLE, ""  # local-commit mode: DONE means commits on base
        state, sha = self._git.pr_merge_state(pr_url)
        if state == "MERGED":
            if sha is not None:
                self._git.backfill_merge_sha(dep, sha)
            return Eligibility.ELIGIBLE, ""
        if state is None:
            return Eligibility.WAITING, f"dependency '{dep}' merge state unconfirmable"
        return Eligibility.WAITING, f"dependency '{dep}' PR is {state} (unmerged)"

    @staticmethod
    def _in_cycle(start: str, pending: Mapping[str, Sequence[str]]) -> bool:
        """True iff ``start`` lies on a cycle within the pending dependency graph.

        Edges are restricted to deps that are themselves pending (a terminal record is resolved, so
        it cannot close a cycle). ``start`` is on a cycle iff it is reachable from itself.
        """
        stack = [d for d in pending.get(start, ()) if d in pending]
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == start:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(d for d in pending.get(node, ()) if d in pending)
        return False

    def reject_dependency(self, task_file: str, detail: str) -> PipelineResult:
        """Fail-closed terminal reject of a dependency-broken task (cycle / unknown / self-ref).

        Reuses the Phase-A reject machinery: quarantine to ``tasks/rejected/``, a ``failed`` ledger
        record with the ``invalid_depends_on`` reason, and ``validation_report.json`` — **no branch
        is ever created**.
        """
        result = ValidationResult(
            passed=False, reason=ValidationReason.INVALID_DEPENDS_ON, detail=detail
        )
        return self._reject(task_file, result)

    # --- rerun (operator-driven re-attempt of a terminal task) ----------------------------

    def _resolve_task_source(self, row: TaskRow) -> tuple[str | None, tuple[str, ...]]:
        """Resolve a task's source file for rerun, tolerant of lifecycle-folder desync.

        The stored ``source_path`` can point at a stale lifecycle folder (e.g. ``tasks/failed/``)
        while the file now lives in another (``tasks/pending/``) — a manual or external move then
        makes the task un-rerunnable if we trust the single stored path. So: if the stored path is
        a file, use it; otherwise search ``tasks/{pending,processing,done,failed}/`` for the task by
        id (``<id>.md``/``<id>.json``), then by slug. Returns ``(path, ())`` on a unique resolution,
        ``(None, ())`` when nothing matches, and ``(None, candidates)`` when more than one file
        matches (never guessed — the caller surfaces the ambiguity). Read-only.
        """
        stored = row.source_path
        if stored and Path(stored).is_file():
            return stored, ()
        if not stored:
            return None, ()
        parent = Path(stored).parent
        tasks_root = parent.parent if parent.name in _LIFECYCLE_FOLDERS else parent
        stems = [row.task_id]
        slug = row.slug or (slugify(row.title) if row.title else "")
        if slug and slug != row.task_id:
            stems.append(slug)
        matches: list[str] = []
        seen: set[str] = set()
        for stem in stems:
            for folder in _LIFECYCLE_FOLDERS:
                for suffix in (".md", ".json"):
                    candidate = tasks_root / folder / f"{stem}{suffix}"
                    key = str(candidate.resolve())
                    if candidate.is_file() and key not in seen:
                        seen.add(key)
                        matches.append(str(candidate))
            if matches:
                break  # id matched: do not widen the search to the slug
        if len(matches) == 1:
            return matches[0], ()
        if len(matches) > 1:
            return None, tuple(sorted(matches))
        return None, ()

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
        source_path, ambiguous = self._resolve_task_source(row)
        if ambiguous:
            refusals.append(
                f"task source file is ambiguous for '{task_id}'; multiple files match "
                f"({', '.join(ambiguous)}); leave exactly one and retry"
            )
        elif not source_path:
            refusals.append(
                f"task source file is missing ({row.source_path or 'unset'}); cannot rerun"
            )
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
        """Fresh attempt of a terminal task from the *current* ``base_branch``.

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
        self._git.reset_branch_to_base(
            task_id,
            slug,
            branch_name=row.branch,
            force_reset_remote=force_reset_remote,
        )
        self._store.reset_task_for_rerun(task_id)
        self._rerun_attempt[task_id] = prior + 1
        self._log(task_id).info("rerun: fresh attempt", extra={"attempt": prior + 1})
        return self.run_task(source_path)

    def continue_task(self, task_id: str) -> PipelineResult:
        """Fix-and-continue: revive a terminal task at the stage it failed and resume it.

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
        """Best-effort fetch/pull of ``base_branch`` so git-pushed tasks become visible.

        Called by the ``watch`` loop between ticks. Delegates to the Git Manager, which no-ops
        unless the working copy is on ``base_branch`` (the slot is free after terminal cleanup), so
        it never disturbs an active or interrupted task branch.
        """
        self._git.refresh_base()

    def resume(self) -> PipelineResult | None:
        """Reconcile persisted state on startup and resume the single unfinished task.

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
        """Mark every ambiguously-active task ``manual_action_required`` and record it."""
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
        """Finish an interrupted terminal cleanup: checkout base once, then ledger."""
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
        """Rebuild the context for the one active task and continue it idempotently."""
        assert plan.task_id is not None
        row = self._store.get_task(plan.task_id)
        assert row is not None
        try:
            task = load_normalized(self._artifacts_root, plan.task_id)
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            # A corrupt/truncated/missing normalized manifest can't be resumed — fail closed to
            # manual rather than crashing out of resume(). The persisted TaskRow still carries
            # enough (id/title/branch/slug/status) to run terminal cleanup + ledger.
            return self._go_terminal(
                self._degraded_pipeline(row),
                Status.MANUAL_ACTION_REQUIRED,
                manual_reason=f"corrupt normalized task artifact: {exc}",
            )
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
        )

        try:
            return self._resume_via_engine(p)
        except ManualActionRequired as exc:
            return self._go_terminal(p, Status.MANUAL_ACTION_REQUIRED, manual_reason=exc.reason)
        except (PipelineFailed, GitCommandError) as exc:
            return self._go_terminal(p, Status.FAILED, manual_reason=str(exc))

    def _degraded_pipeline(self, row: TaskRow) -> _Pipeline:
        """A minimal pipeline context for a task whose normalized manifest can't be read, so the
        terminal handler (cleanup + ledger + notify) still runs without the manifest. Counters
        and decomposition come from the controlled SQLite row, not the corrupt on-disk artifact."""
        return _Pipeline(
            task=NormalizedTask(id=row.task_id, title=row.title, description=""),
            task_file=row.source_path or "",
            status=row.status,
            counters=self._store.get_counters(row.task_id),
            decomposition=self._rebuild_decomposition(row.task_id, row),
            branch=row.branch or "",
            slug=row.slug or "",
        )

    def _resume_via_engine(self, p: _Pipeline) -> PipelineResult:
        """Resume the engine from the persisted checkpoint (node-based recovery).

        Hydrates the :class:`FlowRunState` from ``node_runs`` + the ``tasks`` checkpoint and
        continues from ``current_node`` (the decomposed re-entry is handled in :meth:`_run_phases`).
        A task with no flow checkpoint (interrupted before the engine started, or a flow whose
        fingerprint no longer matches) restarts from the top. Side-effect idempotency (commit/push/
        PR) lives in ``publish_operations``, so a resumed run never duplicates them."""
        snapshot = self._resolve_flow(p)
        run_state = hydrate_run_state(self._store, p.task.id)
        # B-lite ceiling: a task parked because every provider was transiently unavailable resumes
        # here. If it has stayed parked longer than ``agents.retry.max_blocked_s`` (total parked
        # wall-clock), stop waiting and go terminal — nothing must hang forever.
        ceiling = self._park_ceiling_exceeded(p)
        if ceiling is not None:
            return self._fail(
                p,
                ceiling,
                node_id=run_state.current_node if run_state is not None else None,
                run_state=run_state,
            )
        if run_state is None or run_state.flow_fingerprint != snapshot.flow_fingerprint:
            # No usable checkpoint (interrupted before the engine wrote one, or the flow changed) →
            # restart from the top via the full driver (re-does preflight + branch prep + engine).
            if p.status not in (Status.NEW, Status.PENDING, Status.VALIDATED):
                self._store.set_status(p.task.id, Status.VALIDATED)  # reset to re-enter the driver
                p.status = Status.VALIDATED
            return self._drive_via_engine(p, self._gate.phase_b(p.task))
        self._check_preflight(p)  # re-resolve the launchable check profile (idempotent)
        p.branch = self._git.prepare_branch(
            p.task.id,
            p.slug,
            epoch=int(time.time()),  # shadowed by the persisted branch override on a normal resume
            branch_name=p.branch or p.task.branch_name,
        )  # re-attach the existing branch (reused)
        self._store.update_task(p.task.id, branch=p.branch, slug=p.slug)
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
        # The per-node skill map is restored separately by ``_resolve_skill_layers`` (from
        # ``skill_map.json``), since it must be in place for a fresh run too — not only on resume.
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

    def _resolve_flow(self, p: _Pipeline) -> FlowSnapshot:
        """Resolve the task's flow by its ``task_type`` (P0.4 dispatch + P4.2 config-aware gate).

        ``task_type=None`` defaults to ``implementation``. An unknown ``task_type`` (no flow file)
        or a flow that fails validation — structural **or** config-aware (provider/ceiling/git/
        budget) — raises :class:`PipelineFailed` (caught by ``run_task`` → terminal ``failed``). The
        task's per-task disabled-node set is validated against the resolved snapshot here too (it
        names a real, safely-skippable node), so a bad ``nodes:`` override fails the same way.
        Resolution runs before branch prep, so either failure happens before any side effect; on
        resume it re-validates against the live config, so a flow made unsafe by a config change is
        rejected rather than run (the recovery ceiling never widens).
        """
        try:
            snapshot = self._flow_registry.resolve(p.task.task_type)
            validate_disabled_nodes(snapshot, p.task.disabled_nodes())
            return snapshot
        except (FlowResolutionError, FlowValidationError) as exc:
            raise PipelineFailed(str(exc)) from exc

    def _resolve_node_overrides(
        self, p: _Pipeline, snapshot: FlowSnapshot
    ) -> Mapping[str, Mapping[str, object]]:
        """Resolve the task's per-node model/reasoning/provider overrides into a field overlay.

        Best-effort (watch-mode compat): an override invalid for this flow/config is warned +
        skipped here, the flow's declared value stands, and the task runs unaffected. Re-derived
        from front matter every run/resume (never persisted), like the ``disabled_nodes`` set.
        """
        resolution = resolve_node_overrides(snapshot, p.task.node_overrides, self._config)
        for warning in resolution.warnings:
            self._log(p.task.id).warning("task node override skipped", extra={"detail": warning})
        return resolution.overlay

    def _drive_via_engine(self, p: _Pipeline, completeness: Completeness) -> PipelineResult:
        """Drive the task through the :class:`FlowEngine`.

        Keeps the orchestrator-owned preamble (isolation + check preflight, branch prep) and the
        terminal handling (auto-merge + cleanup); the refinement→…→publish body is expressed as the
        validated flow graph and executed by the engine. Per-node post-processing (artifact slots,
        skills) runs in the post-node hook; the publish node finalizes the task file + opens the PR.
        Infra failure → ``failed``; a node needing human action → ``manual_action_required``.
        """
        self._resolve_flow(p)  # fail closed on an unknown/invalid flow before any side-effect
        if p.operator_decomposition is not None:
            # Operator-authored split: materialize the manifest-built decision now, before any
            # branch, so it is in place whether or not the planning ``proposed_by`` node runs (a
            # disabled planning node never fires the post-hook). Validated already at preflight.
            self._persist_decomposition(p, p.operator_decomposition, gate_on=True)
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
        snapshot = self._resolve_flow(p)  # task_type → flow (P0.4 dispatch)
        assert snapshot.source_path is not None
        node_overrides = self._resolve_node_overrides(p, snapshot)
        if run_state is None:
            run_state = FlowRunState(flow_fingerprint=snapshot.flow_fingerprint)
        # The constant supervisor layer starts at task start and lives the whole cycle (P2.1); it
        # carries this task's own resume_own_lineage session.
        self._supervisor = self._build_supervisor(snapshot)
        inputs = build_node_inputs(
            p,
            flow_dir=snapshot.source_path.parent,
            check_sets=self._check_sets(p),  # normalized command_sets; () = no gate
            pull_request_title=p.task.title,
            commit_message=f"feat({p.task.id}): {p.task.title}",
            summary_body_path=self._fallback_summary_path(p),
        )
        if resume:
            self._restore_engine_inputs(p, inputs)  # diff/checks/review/plan paths from disk
        # Resolve the per-node skill selection before any node runs: discover the inventory, apply
        # operator pins (strict/warn) + the supervisor's once-per-task proposal, and thread the
        # effective map into ``inputs``. On resume the persisted map is restored (no re-proposal).
        self._resolve_skill_layers(p, snapshot, inputs, resume=resume)
        services = build_node_services(
            router=self._router,
            check_runner=self._checks,
            store=self._store,
            repo_dir=self._config.repo.local_path,
            artifacts_root=str(self._artifacts_root),
            clock=self._clock,
            git=self._git,
            notifier=self._notifier,
            snapshot_hook=self._git,
            ask_timeout_s=self._config.telegram.ask_timeout_s,
            ask_heartbeat_seconds=self._heartbeat_seconds,
            # Claude-only max-turns gate (idea 29): resolved once from the claude provider block
            # (absent in a codex-only setup → off). Preflight guarantees telegram when it is on.
            max_turns_gate=self._max_turns_gate_enabled(),
            prompt_audit=self._prompt_audit_on(p.task),
            prompt_secrets=self._prompt_secrets(),
            register_artifact=self._register_artifact,
            finalize=lambda: self._engine_finalize(p),
            # The dependency_scan checker launches its argv scanners through the same safe runner
            # and allowlisted env the Check Runner uses (a test's fake runner drives both).
            run_process=self._checks.run_process,
            process_env=build_child_env(self._config.security.allowed_environment),
            scan_timeout_s=self._config.checks.timeout_seconds,
            deletion_approval_exempt_paths=self._config.security.deletion_approval_exempt_paths,
        )
        recorder = StateStoreRunRecorder(
            self._store, p.task.id, artifacts_root=self._artifacts_root
        )
        try:
            result = self._run_phases(
                p,
                snapshot,
                run_state,
                recorder,
                services,
                inputs,
                completeness,
                node_overrides=node_overrides,
                resume=resume,
            )
        except NodeManualRequired as exc:
            self._sync_counters_from_run_state(p, run_state)
            return self._go_terminal(p, Status.MANUAL_ACTION_REQUIRED, manual_reason=str(exc))
        except EvaluatorInfraError as exc:
            # An evaluator that could not *run* (infra/misconfig) must not discard an already-green
            # diff: degrade to manual (branch preserved, operator reviews/publishes), not failed.
            self._sync_counters_from_run_state(p, run_state)
            return self._fail(
                p,
                str(exc),
                status=Status.MANUAL_ACTION_REQUIRED,
                node_id=run_state.current_node,
                run_state=run_state,
            )
        except NodeInfraError as exc:
            self._sync_counters_from_run_state(p, run_state)
            if exc.error_class in TRANSIENT_RETRYABLE:
                # Every allowed provider is transiently unavailable (retries + fallback exhausted).
                # Don't discard the task: park it as resumable (B-lite). The checkpoint is already
                # persisted; the next watch tick / process start resumes from current_node.
                return self._park(p, run_state, exc)
            return self._fail(p, str(exc), node_id=run_state.current_node, run_state=run_state)
        self._sync_counters_from_run_state(p, run_state)
        return self._finish_engine_run(p, result)

    def _park(
        self, p: _Pipeline, run_state: FlowRunState, exc: NodeInfraError
    ) -> PipelineResult:
        """Soft, resumable pause on transient infra exhaustion (B-lite). NOT a terminal transition.

        The task stays ``RUNNING`` (active) so :meth:`resume` picks it up via the reconciler next
        tick / next start; the flow checkpoint is already saved (``current_node``). Records the
        first park instant in ``tasks.blocked_since`` (kept across re-parks so the ceiling measures
        total parked wall-clock); the ceiling is checked on resume in :meth:`_resume_via_engine`. No
        commit/push and no failure report — the partial work is preserved by the checkpoint."""
        existing = self._store.get_task(p.task.id)
        if existing is None or existing.blocked_since is None:
            self._store.update_task(p.task.id, blocked_since=self._clock())
        log = bind(_LOG, task_id=p.task.id)
        log.info(
            "task parked (resumable): every provider transiently unavailable",
            extra={
                "node_id": run_state.current_node,
                "error_class": exc.error_class.value if exc.error_class else None,
            },
        )
        return PipelineResult(task_id=p.task.id, final_status=Status.RUNNING)

    def _park_ceiling_exceeded(self, p: _Pipeline) -> str | None:
        """Return a terminal reason if the task has been parked (B-lite) past ``max_blocked_s``.

        ``None`` when the task is not parked or is still within the ceiling. Measured from the first
        park (``tasks.blocked_since``) to now, using the injected clock; a malformed timestamp is
        treated as not-exceeded (the next park re-stamps it)."""
        task = self._store.get_task(p.task.id)
        if task is None or task.blocked_since is None:
            return None
        try:
            elapsed = (
                datetime.fromisoformat(self._clock()) - datetime.fromisoformat(task.blocked_since)
            ).total_seconds()
        except ValueError:
            return None
        ceiling = self._config.agents.retry.max_blocked_s
        if elapsed > ceiling:
            return (
                f"provider outage exceeded agents.retry.max_blocked_s "
                f"({elapsed:.0f}s parked > {ceiling:.0f}s)"
            )
        return None

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
        node_overrides: Mapping[str, Mapping[str, object]] = MappingProxyType({}),
        resume: bool = False,
    ) -> FlowRunResult:
        """Drive the flow in phases. Fresh: a flow with no decomposition runs in one pass; a
        decomposed one runs pre (entry…proposed_by) once, the sub_flow region once per subtask
        (commit between), then post once. Resume: continue from the hydrated ``current_node`` — a
        run still in ``pre`` re-runs pre (planning re-decides), a single-unit run continues from
        ``current_node``, and a decomposed run re-enters the active uncommitted subtask at the
        region entry (committed subtasks are skipped, never re-committed)."""
        post_node = self._engine_post_node(p, inputs, snapshot)
        facts = self._engine_facts(completeness, snapshot)
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
                disabled_nodes=p.skip,
                node_overrides=node_overrides,
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
        the post-region phase. A subtask with a verified commit is never re-run (recovery).

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
        update_subtask_index(
            self._artifacts_root, p.task.id, unit.order, status="committed", commit_sha=sha
        )
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
        summary is *always* written, one way or the other (``config.summary_enabled`` is gone).

        The transitions below are logged (with elapsed time on the supervisor synthesis) so the
        end-of-run tail is never a silent window: the whole-task summary can take minutes of
        context assembly + one LLM call, and without a heartbeat that looked like a hang."""
        log = self._log(p.task.id)
        log.info("task finalize: starting (whole-task summary, then publish prep)")
        if self._supervisor is not None:
            started = time.monotonic()
            self._supervisor.finalize(task_id=p.task.id, task_title=p.task.title)
            log.info(
                "task finalize: supervisor summary written",
                extra={"elapsed_seconds": round(time.monotonic() - started, 1)},
            )
        self._append_skip_section(p)  # note skipped nodes on the supervisor summary (idempotent)
        log.info("task finalize: publish prep (committed summary + task-file move)")
        summary_md = self._finalize_task_artifacts(p, Status.DONE)
        return str(summary_md) if summary_md is not None else None

    def _engine_facts(
        self, completeness: Completeness, snapshot: FlowSnapshot
    ) -> Callable[[str], bool]:
        """Resolve a flow ``when`` fact (``derived.*`` / ``config.*``) to a boolean.

        Per-task node-disable no longer flows through a ``config.*_enabled`` fact — it is handed to
        the engine directly as ``disabled_nodes`` (keyed by node id), so the only facts left are the
        deterministic refinement-skip and the flow-capability ``config.external_research``.
        """
        # Refinement-skip is deterministic — driven purely by completeness classification, never a
        # task flag (PRE.3): a ``complete`` task skips refinement, anything else runs it.
        needs_refinement = completeness is not Completeness.COMPLETE
        # External research (deep_research) is available iff the flow grants network — there is no
        # separate config knob (config.yaml stays infra-only): an optional node's availability is a
        # capability of the flow, not an orchestrator flag.
        external_research = snapshot.doc.network_policy is not None

        def facts(fact: str) -> bool:
            if fact == "derived.needs_refinement":
                return needs_refinement
            if fact == "config.external_research":
                return external_research
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
            # Best-effort live progress trace: one message per executed node finish (never on a
            # skip). Gated on the flag alone — when Telegram is off the notifier is a NullNotifier
            # and this is a no-op. Carries only node id + outcome (no secrets); never raises.
            if self._config.telegram.trace:
                self._notifier.send_trace(
                    task_id=p.task.id, node_id=node.id, outcome=outcome.kind
                )
            if not isinstance(node, AgentNode):
                return
            apply_output_artifact(
                node,
                outcome,
                artifacts_root=self._artifacts_root,
                task_id=p.task.id,
                inputs=inputs,
                register=self._register_artifact,
            )
            # Operator-authored splits are materialized at preflight (the decision comes from the
            # ``subtasks:`` manifest, not this node), so this post-hook is a no-op for them.
            if (
                decomp is not None
                and node.id == decomp.proposed_by
                and p.operator_decomposition is None
            ):
                gate_on = self._decomposition_gate_on(p.task)
                decision = read_decomposition(
                    outcome,
                    gate_on=gate_on,
                    max_subtasks=self._config.agents.decomposition.max_subtasks,
                )
                self._persist_decomposition(p, decision, gate_on=gate_on)

        return post_node

    def _persist_decomposition(
        self, p: _Pipeline, decision: DecompositionDecision, *, gate_on: bool
    ) -> None:
        """Persist a decomposition decision + write the subtask specs/rows. Source-agnostic.

        Shared by the agent path (``proposed_by`` post-hook) and the operator path (preflight). For
        an accepted operator split the spec files carry the operator's verbatim subtask bodies.
        """
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

    def _resolve_skill_layers(
        self, p: _Pipeline, snapshot: FlowSnapshot, inputs: NodeInputs, *, resume: bool
    ) -> None:
        """Resolve the per-node skill selection once at task start (before any node runs).

        Discovers the whole-repo inventory, applies operator pins (subject to ``skills.strict``) and
        — when ``skills.dynamic`` and the inventory is non-empty — the supervisor's once-per-task
        proposal, then threads the effective per-node map (pins ∪ the accepted proposal) into
        ``inputs`` as absolute read-only reference paths. The map is persisted to ``skill_map.json``
        so a resume restores it without re-proposing; a resume whose map file is missing
        (interrupted before it was written) falls back to a fresh, idempotent resolution.
        """
        map_file = task_artifact_dir(self._artifacts_root, p.task.id) / "skill_map.json"
        if resume and map_file.exists():
            p.skill_map = self._load_skill_map(map_file)
            inputs.skill_paths_by_node = self._skill_paths_by_node(p.skill_map)
            return
        p.skill_inventory = self._skill_scanner.collect()
        agent_nodes = [n for n in snapshot.doc.nodes if isinstance(n, AgentNode)]
        pins = {n.id: resolve_skills(n.skills, p.skill_inventory) for n in agent_nodes}
        self._enforce_pin_strictness(p, pins)
        proposed = self._propose_skill_map(p, agent_nodes)
        # Effective set per node = Core_filter(pins ∪ accepted proposal). Dynamic tokens that do
        # not resolve are dropped here (only ``.refs`` kept); pins already passed strict/warn.
        skill_map = {
            n.id: resolve_skills((*n.skills, *proposed.get(n.id, ())), p.skill_inventory).refs
            for n in agent_nodes
        }
        p.skill_map = {nid: refs for nid, refs in skill_map.items() if refs}
        self._persist_skill_map(p, map_file)
        inputs.skill_paths_by_node = self._skill_paths_by_node(p.skill_map)

    def _enforce_pin_strictness(self, p: _Pipeline, pins: dict[str, SkillSelection]) -> None:
        """Strict/warn handling for unresolved operator pins (a dynamic proposal is never an error).

        ``skills.strict`` true stops the task in ``manual_action_required`` with a report; false
        (default, fail-open) logs a warning per node and continues, dropping the unresolved pins.
        """
        unresolved = {nid: sel.unresolved for nid, sel in pins.items() if sel.unresolved}
        if not unresolved:
            return
        report = "; ".join(f"{nid}: {', '.join(toks)}" for nid, toks in sorted(unresolved.items()))
        if self._config.skills.strict:
            raise ManualActionRequired(f"unresolved skill pin(s) (skills.strict): {report}")
        self._log(p.task.id).warning(
            "unresolved skill pin(s) skipped (skills.strict=false)", extra={"pins": report}
        )

    def _propose_skill_map(
        self, p: _Pipeline, agent_nodes: list[AgentNode]
    ) -> dict[str, tuple[str, ...]]:
        """The supervisor's once-per-task proposal (when ``dynamic`` and skills exist), else {}."""
        if not self._config.skills.dynamic or not p.skill_inventory.skills:
            return {}
        if self._supervisor is None:  # defensive — the layer is built before this runs
            return {}
        return self._supervisor.propose_skill_map(
            task_id=p.task.id,
            agent_node_ids=[n.id for n in agent_nodes],
            inventory=p.skill_inventory,
            task_spec_text=self._skill_task_spec_text(p),
        )

    def _skill_task_spec_text(self, p: _Pipeline) -> str:
        """A bounded task spec (title + description) the proposal uses to judge skill relevance."""
        text = f"{p.task.title}\n\n{(p.task.description or '').strip()}".strip()
        return text[:8000]

    def _persist_skill_map(self, p: _Pipeline, map_file: Path) -> None:
        """Persist the effective per-node skill map so a resume restores it without re-proposing."""
        data = {
            nid: [{"name": r.name, "description": r.description, "path": r.path} for r in refs]
            for nid, refs in p.skill_map.items()
        }
        map_file.parent.mkdir(parents=True, exist_ok=True)
        map_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self._register_artifact(p.task.id, "skill_map", str(map_file))

    def _load_skill_map(self, map_file: Path) -> dict[str, tuple[SkillRef, ...]]:
        """Rebuild the per-node skill map from ``skill_map.json`` (repo-relative paths kept)."""
        raw = json.loads(map_file.read_text(encoding="utf-8"))
        return {
            nid: tuple(
                SkillRef(name=d["name"], description=d["description"], path=d["path"]) for d in refs
            )
            for nid, refs in raw.items()
        }

    def _skill_paths_by_node(
        self, skill_map: dict[str, tuple[SkillRef, ...]]
    ) -> dict[str, tuple[str, ...]]:
        """Node id → absolute POSIX reference paths (repo-relative identity joined to the clone)."""
        repo = Path(self._config.repo.local_path)
        return {
            nid: tuple((repo / r.path).as_posix() for r in refs) for nid, refs in skill_map.items()
        }

    def _finish_engine_run(self, p: _Pipeline, result: FlowRunResult) -> PipelineResult:
        """Map a terminal :class:`FlowRunResult` to a :class:`PipelineResult` (+ auto-merge)."""
        if result.status is Status.DONE:
            pr_url = self._git.recorded_pr_url(p.task.id)
            if pr_url and self._auto_merge_on(p.task):
                # A skipped check (toolchain absent) means the quality gate did not fully run — an
                # incomplete gate is never auto-merged; hand the open PR to a human.
                if self._store.task_had_skipped_checks(p.task.id):
                    self._log(p.task.id).warning(
                        "[AUTO-MERGE] skipped: a check was skipped (toolchain absent) — the gate "
                        "is incomplete; leaving the PR open for human review"
                    )
                    return self._go_terminal(p, Status.DONE, pr_url=pr_url, already_moved=True)
                return self._auto_merge(p, pr_url)
            return self._go_terminal(p, Status.DONE, pr_url=pr_url, already_moved=True)
        if result.status is Status.MANUAL_ACTION_REQUIRED:
            return self._go_terminal(
                p, Status.MANUAL_ACTION_REQUIRED, manual_reason=result.limit_name or "stuck"
            )
        return self._fail(p, result.limit_name or "flow run failed")

    def _check_preflight(self, p: _Pipeline) -> None:
        """Normalize ``checks.command_sets`` onto the pipeline at task start (before any branch).

        Trivial now (no discovery): an empty ``command_sets`` mapping resolves to ``()`` — no gate.
        Selection of which sets to run happens later, in the checks node, once the diff is known.
        Skipped when no resolver is wired (the Check Runner then normalizes the config itself).
        """
        if self._resolver is None:
            return
        p.check_sets = self._resolver.resolve()

    def _prepare_branch(self, p: _Pipeline) -> None:
        """Complete the persisted ``preparing`` checkpoint and attach the task branch."""
        # Guarantee the `.worc/` runtime home is gitignored in this clone, regardless of how it was
        # scaffolded, so it never leaks into the operator's git status (no branch exists yet).
        self._git.ensure_runtime_excludes()
        p.slug = slugify(p.task.title)
        epoch = int(time.time())  # makes a fresh attempt's branch unique (re-run never collides)
        p.branch = self._observe(
            p,
            "branch preparation",
            lambda: self._git.prepare_branch(
                p.task.id,
                p.slug,
                epoch=epoch,
                branch_name=p.task.branch_name,
            ),
        )
        self._store.update_task(p.task.id, branch=p.branch, slug=p.slug)

    def _skip_section_md(self, p: _Pipeline) -> str:
        """A ``## Pipeline nodes skipped`` markdown block, or ``""`` when nothing was skipped."""
        if not p.skip:
            return ""
        lines = "\n".join(f"- `{node_id}`" for node_id in sorted(p.skip))
        return f"\n## Pipeline nodes skipped\n\n{lines}\n"

    def _append_skip_section(self, p: _Pipeline) -> None:
        """Append the skipped-nodes section to ``summary.md`` (idempotent within a run)."""
        section = self._skip_section_md(p)
        if not section:
            return
        md_path = task_artifact_dir(self._artifacts_root, p.task.id) / "summary.md"
        if not md_path.exists():
            return
        existing = md_path.read_text(encoding="utf-8")
        if "## Pipeline nodes skipped" in existing:
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
        """The human-readable summary text; falls back to a deterministic minimal summary."""
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
        """A short sibling-relative pointer to the task file for the committed summary.

        The committed ``<id>.summary.md`` lives next to the moved ``<id>.md`` task file, so the
        basename is the correct, move-independent reference. ``None`` for a synthetic ``run`` path.
        """
        return Path(p.task_file).name if p.task_file else None

    def _finalize_task_artifacts(self, p: _Pipeline, final: Status) -> Path | None:
        """Move the task into its lifecycle folder; write the committed `<id>.summary.md` alongside.

        Runs **before** the commit so both land in the task (audit) commit. Returns the
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

    def _fail(
        self,
        p: _Pipeline,
        error: str,
        *,
        status: Status = Status.FAILED,
        node_id: str | None = None,
        run_state: FlowRunState | None = None,
    ) -> PipelineResult:
        """Terminal on an infra failure. Always writes a failure report (so every infra terminal is
        diagnosable — no silent ``failed`` without ``failure_report.json``/``stuck.md``).

        When a task branch exists, finalize like a success — move the task to ``tasks/failed/``
        (``failed`` only), write its ``summary.md``, commit (code + task) and push — so the attempt
        and its summary are stored in git. No PR is opened. When no branch was created yet (e.g. an
        isolation-preflight failure), nothing is published.

        ``status=MANUAL_ACTION_REQUIRED`` degrades an evaluator that could not *run*: the green diff
        is shippable, so the branch is preserved (the task file stays put) for the operator to
        review/publish instead of being discarded as ``failed``.

        The git operations are best-effort: a failed task must still reach a terminal state even if
        git is unhappy, so a publish error here is logged, not raised.
        """
        self._write_infra_failure_report(p, node_id=node_id, error=error, run_state=run_state)
        if not p.branch:
            return self._go_terminal(p, status, manual_reason=error)
        moved = False
        try:
            moved = self._finalize_task_artifacts(p, status) is not None
            verb = "failed attempt" if status is Status.FAILED else "manual action required"
            self._git.commit_code(p.task.id, f"chore({p.task.id}): {verb} — {p.task.title}")
            self._git.commit_audit(p.task.id)
            self._git.push(p.task.id, p.branch)
        except (GitCommandError, OSError) as exc:
            self._log(p.task.id).warning(
                "infra-terminal publish incomplete", extra={"error": str(exc)}
            )
        return self._go_terminal(p, status, manual_reason=error, already_moved=moved)

    def _write_infra_failure_report(
        self,
        p: _Pipeline,
        *,
        node_id: str | None,
        error: str,
        run_state: FlowRunState | None,
    ) -> None:
        """Write ``failure_report.json`` + ``stuck.md`` for an infra terminal (best-effort).

        Reuses the flow-neutral ledger writer. There is no fix-loop budget here, so ``loop="infra"``
        and ``limit_name`` carries the infra error. A write failure must never mask the terminal
        outcome, so it is logged, not raised.
        """
        try:
            report_path, _stuck = write_failure_report(
                self._artifacts_root,
                p.task.id,
                loop="infra",
                limit_name=error,
                counters=dict(run_state.loop_counters) if run_state is not None else {},
                last_check_log=None,
                last_review_findings=None,
                final_diff="",
                node_id=node_id,
            )
            self._store.update_task(p.task.id, failure_report_path=report_path)
        except OSError as exc:
            self._log(p.task.id).warning("failure report not written", extra={"error": str(exc)})

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
        """Run terminal cleanup, set the final status, append exactly one ledger record.

        ``already_moved`` is set when the task file was moved + committed during finalize; the
        move is then complete on the task branch, so this must not re-move it on ``base_branch``
        after the cleanup checkout.
        """
        final = status
        cleanup = self._observe(
            p, "terminal cleanup", lambda: self._git.terminal_cleanup(p.task.id)
        )
        if not cleanup.safe and status is Status.DONE:
            # Publishing finished but the working copy could not be safely restored → manual.
            final = Status.MANUAL_ACTION_REQUIRED
        # Record the terminal-cleanup outcome and the reason this task stopped (when applicable).
        last_error = cleanup.error or manual_reason
        self._store.update_task(
            p.task.id,
            cleanup_target_branch=cleanup.target_branch,
            cleanup_completed=cleanup.safe,
            cleanup_completed_at=self._clock() if cleanup.safe else None,
            cleanup_last_error=last_error,
            blocked_since=None,  # B-lite: a terminal task is no longer parked
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
        """Move the task file to its lifecycle folder; see _relocate_task_file."""
        return self._relocate_task_file(p.task_file, p.task.id, final)

    def _relocate_task_file(
        self, task_file: str | None, task_id: str, final: Status
    ) -> Path | None:
        """Move a task file into its lifecycle folder (``tasks/done`` / ``tasks/failed``).

        Pipeline-free (used by both the pipeline's `_move_task_file` and the operator `finalize`).
        ``done`` and ``failed`` move; ``manual_action_required`` stays put for the operator to
        resolve — so a finalize `--as abandoned` leaves the file where it is. A gate
        reject is quarantined separately. Idempotent: returns the destination whether it
        moved now or was already in place; returns ``None`` when there is nothing to do.
        """
        folder_name = {Status.DONE: "done", Status.FAILED: "failed"}.get(final)
        if folder_name is None or not task_file:
            return None
        src = Path(task_file)
        parent = src.parent
        tasks_root = parent.parent if parent.name in _LIFECYCLE_FOLDERS else parent
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
        """Handle a Phase-A reject: failed, quarantine, report, ledger — no branch."""
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
        label: str,
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
        self._raise_human_failure(p, label, failure)

    def _raise_human_failure(
        self,
        p: _Pipeline,
        label: str,
        failure: str,
    ) -> None:
        self._log(p.task.id).warning(
            "human input failed",
            extra={"label": label, "failure": failure},
        )
        raise ManualActionRequired(f"{label} human input failed: {failure}")

    def _check_sets(self, p: _Pipeline) -> tuple[ResolvedCheckSet, ...]:
        """The normalized command sets; recompute from config if not resolved yet (e.g. on resume).

        Idempotent and cheap (no I/O) — ``()`` when no resolver is wired or no sets are configured.
        """
        if p.check_sets:
            return p.check_sets
        if self._resolver is None:
            return ()
        p.check_sets = self._resolver.resolve()
        return p.check_sets

    # --- artifact + logging helpers -------------------------------------------------------

    def _prompt_secrets(self) -> tuple[str, ...]:
        """Denied-read file secrets to scrub from the rendered prompt before storage."""
        return read_denied_secrets(
            self._config.repo.local_path, self._config.security.denied_read_paths
        )

    def _log(self, task_id: str) -> logging.LoggerAdapter[logging.Logger]:
        """A task-scoped structured logger: every record carries ``task_id``."""
        return bind(_LOG, task_id=task_id)

    def _register_artifact(self, task_id: str, kind: str, path: str | None) -> None:
        """Register a durable artifact in SQLite with a sha256 checksum (best-effort).

        Idempotent (the store upserts on ``(task_id, kind, path)``); a missing file is skipped and
        registration never raises into the terminal path. Requires the ``tasks`` row to exist (FK),
        so a-rejected task — which has no row — is not registered here.
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

    def _decomposition_gate_on(self, task: NormalizedTask) -> bool:
        """Whether decomposition is permitted at all (PRE.3): the task value overrides the global.

        Mirrors :meth:`_prompt_audit_on`: a per-task ``decomposition: true``/``false`` is honored
        verbatim (true permits a split even when ``agents.decomposition.enabled`` is off, false
        forbids one even when it is on); absent (None) defers to the global. This only flips the
        *gate* — the flow's ``decomposition:`` block + the planning node's proposal (or an operator
        ``subtasks:`` manifest) still decide whether a split actually happens. There is no operator
        gate (the task author owns the config too)."""
        if task.decomposition is True:
            return True
        if task.decomposition is False:
            return False
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
        """Best-effort terminal notification. Never raises and never alters the outcome."""
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

    ``is_recovery_rerun`` is threaded into the gate so the ``rerun`` command can admit exactly
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
    # The resolver just normalizes the operator's ``checks.command_sets`` (no discovery).
    resolver = CheckResolver(config)
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
        heartbeat_seconds=heartbeat_seconds,
    )
