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
import shutil
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from wastech_orchestrator.check_runner import CheckRunner
from wastech_orchestrator.checks.model import ResolvedCheckSet
from wastech_orchestrator.checks.resolver import CheckResolver
from wastech_orchestrator.config.schema import (
    BranchMode,
    MergeStrategy,
    ObserveMode,
    OrchestratorConfig,
)
from wastech_orchestrator.core import observe_cadence
from wastech_orchestrator.core.decomposition import (
    REASON_N_OUT_OF_RANGE,
    DecompositionDecision,
    SubtaskSpec,
    decide_operator_decomposition,
    subtask_handoff_path,
    subtask_spec_path,
    update_subtask_index,
    write_subtask_artifacts,
)
from wastech_orchestrator.core.flow.control_bundle import (
    ControlBundleError,
    FrozenControlBundle,
    digest_live_control_inputs,
    diverged_control_inputs,
    freeze_control_bundle,
    load_control_bundle,
)
from wastech_orchestrator.core.flow.engine import (
    Finding,
    FlowCancelled,
    FlowRunResult,
    NodeOutcome,
    entry_node_id,
)
from wastech_orchestrator.core.flow.engine_driver import (
    DecompositionRegions,
    drive_flow,
    partition_decomposition,
)
from wastech_orchestrator.core.flow.exchange_seal import (
    ExchangeCleanupBlocked,
    ExchangeSealError,
    clear_foreign_exchange_entries,
    ensure_current_exchange,
    quarantine_contaminated,
    seal_exchange,
)
from wastech_orchestrator.core.flow.instruction_bundle import (
    REPO_INSTRUCTION_NAMES,
    TASK_PACKET_KEY,
    InstructionBundleError,
    assert_no_required_secret,
    discover_repository_instructions,
    freeze_repository_instructions,
    freeze_task_packet,
    governance_changed_paths,
    instruction_bundle_dir,
    load_instruction_bundle,
    write_instruction_manifest,
)
from wastech_orchestrator.core.flow.nodes.base import (
    EvaluatorInfraError,
    NodeInfraError,
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.nodes.exchange_publish import (
    ExchangeMutationManual,
    publish_artifact,
    publish_file,
    publish_node_run_file,
)
from wastech_orchestrator.core.flow.output_policy import is_within, resolve_output_policy
from wastech_orchestrator.core.flow.postprocess import (
    apply_output_artifact,
    read_decomposition,
    write_node_output,
)
from wastech_orchestrator.core.flow.recorder import (
    StateStoreRunRecorder,
    fell_back_from,
    hydrate_run_state,
    read_final_diff,
    read_last_findings,
)
from wastech_orchestrator.core.flow.registry import FlowRegistry, FlowResolutionError
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import AgentNode, EvaluatorNode, FlowNode
from wastech_orchestrator.core.flow.security_preamble import build_orchestrator_security_preamble
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot, load_flow
from wastech_orchestrator.core.flow.tools_registry import ToolRegistry
from wastech_orchestrator.core.flow.validator import (
    FlowValidationError,
    validate_disabled_nodes,
)
from wastech_orchestrator.core.flow.wiring import build_node_inputs, build_node_services
from wastech_orchestrator.core.follow_ups import (
    FOLLOW_UPS_FILENAME,
    FollowUp,
    append_task_follow_ups,
    evaluator_finding_follow_ups,
    merge_follow_ups,
    render_gate_digest,
)
from wastech_orchestrator.core.hitl import (
    consume_pending_interactions,
    reset_pending_interactions,
)
from wastech_orchestrator.core.infra_disposition import (
    InfraDisposition,
    classify_exhaustion,
)
from wastech_orchestrator.core.loop_control import (
    ExhaustedLoop,
    LoopCounters,
    exhausted_fix_loops,
    global_backstop_exhausted,
)
from wastech_orchestrator.core.node_overrides import resolve_node_overrides
from wastech_orchestrator.core.recovery import (
    RecoveryAction,
    RecoveryPlan,
    RecoveryReconciler,
)
from wastech_orchestrator.core.state_machine import Status, assert_transition
from wastech_orchestrator.core.summary_report import (
    SKIPPED_NODES_HEADING,
    SUMMARY_MD_FILENAME,
    render_skipped_nodes_section,
    write_summary_report,
)
from wastech_orchestrator.core.supervisor import Supervisor
from wastech_orchestrator.core.supervisor_packet import build_packet_facts
from wastech_orchestrator.core.supervisor_usage import summarize_spend
from wastech_orchestrator.git_manager import (
    CleanupOutcome,
    GitCommandError,
    GitManager,
    ManualActionRequired,
)
from wastech_orchestrator.ledger import (
    INFRA_LOOP,
    STUCK_FILENAME,
    Ledger,
    LedgerRecord,
    NodeFailureEvidence,
    write_failure_report,
)
from wastech_orchestrator.memory import (
    AuditActor,
    AuditContext,
    CandidateDelta,
    DerivedIndex,
    EpisodeRecord,
    MemoryLayout,
    MemoryService,
    PacketBuilder,
    TrustLevel,
    WriteSource,
    ensure_store,
)
from wastech_orchestrator.notify import (
    TRACE_ADOPTED_COMMITS,
    TRACE_FINDINGS_WITHOUT_A_PATH,
    TRACE_GIT_CONTROL_DRIFT,
    TRACE_REWORK_EXHAUSTED,
    TRACE_UNEXPECTED_WRITE,
    Notifier,
    NullNotifier,
    TerminalDetails,
    TerminalFinding,
)
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.providers.artifacts import (
    PathIdentityError,
    append_node_history,
    archive_task_artifacts,
    node_run_dir,
    sha256_file,
    task_artifact_dir,
)
from wastech_orchestrator.providers.base import (
    ErrorClass,
    ProviderId,
)
from wastech_orchestrator.providers.exchange import (
    ExchangeError,
    clear_exchange_task_dir,
    diff_exchange_manifests,
)
from wastech_orchestrator.providers.redaction import (
    read_denied_secrets,
    redact_text,
    secret_env_values,
)
from wastech_orchestrator.routing.router import AgentRouter
from wastech_orchestrator.runs_retention import remove_task_runs
from wastech_orchestrator.runtime_layout import (
    CONTROL_BUNDLE_DIRNAME,
    InternalDenyPolicy,
    RuntimeLayout,
)
from wastech_orchestrator.security.env import (
    build_child_env,
    describe_expansions,
    expand_allowed_environment,
)
from wastech_orchestrator.security.isolation import (
    HostFloorCheck,
    IsolationCheck,
    check_isolation,
    describe_advanced_mode,
    describe_host_floor,
)
from wastech_orchestrator.state_store import (
    ArtifactRow,
    EvaluationRow,
    ProviderAttemptRow,
    StateStore,
    SubtaskRow,
    TaskRow,
)
from wastech_orchestrator.task.model import DEFAULT_COMMIT_TYPE, NormalizedTask
from wastech_orchestrator.task.parser import (
    SubtaskSpecFile,
    load_normalized,
    read_subtask_refs,
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

# The lifecycle folders a task file moves between under ``tasks/`` (registration → done/failed).
# "Currently running" is tracked by the task's ``state.db`` status, not a physical folder.
_LIFECYCLE_FOLDERS = ("pending", "done", "failed")

# Node kinds the constant supervisor layer does NOT observe. ``publish`` is terminal (its finalize
# hook already wrote the summary); ``tool`` and ``checks`` are deterministic, so their result is
# already a durable fact the finalize packet carries verbatim (``node_runs.outcome`` /
# ``check_runs``) and an advisory LLM note about a pass/fail adds nothing to the summary for a full
# turn's cost. Keyed on the engine's node *kind*, never on a node id or a flow name — flow-agnostic.
_UNOBSERVED_NODE_KINDS = frozenset({"tool", "checks", "publish"})

# The statuses ``rerun`` will re-enter: an unrecoverable failure, an operator-action park, and a
# stale ``running`` row (a killed/crashed task, daemon-less by the time it reaches the plan).
#
# ``DONE`` is deliberately absent, and something else depends on that: a successful task's frozen
# bundles and sealed exchange are evicted at its terminal transition, which is only safe because
# nothing can ever ask to resume from them. Admitting ``DONE`` here would silently start deleting
# restore data a rerun needs.
RERUN_ELIGIBLE_STATUSES: frozenset[Status] = frozenset(
    {Status.FAILED, Status.MANUAL_ACTION_REQUIRED, Status.RUNNING}
)


def task_commit_subject(task_id: str, title: str, commit_type: str | None = None) -> str:
    """The Conventional-Commits subject for this task's own commit.

    One place, because every commit a task produces carries it: the code commit on the task branch,
    each subtask commit of a decomposition, and the squash/merge commit that lands them on the base
    branch. They disagreed before — the squash subject was left to the target repository, which took
    the bare pull-request title — and the target's own first git rule was "Conventional Commits", so
    the tool's merge path violated it.

    ``commit_type`` is the task's own front-matter key, defaulting to ``feat`` (what every task
    produced before the key existed). The scope is always the task id: it is the one thing that
    makes a subject greppable back to the task that produced it, and unlike the type it is never
    the author's to choose. The task file is the whole channel into this subject — no node can
    write a commit message — which is why the type is an operator field rather than an agent's.
    """
    return f"{commit_type or DEFAULT_COMMIT_TYPE}({task_id}): {title}"


def merge_commit_subject(
    task_id: str, title: str, pr_url: str | None, commit_type: str | None = None
) -> str:
    """:func:`task_commit_subject` plus the pull-request number, for the squash/merge commit.

    The ``(#N)`` suffix is what GitHub appends itself when it is left to compose the subject, and
    the target repository's history is full of it — so keeping it means the explicit subject reads
    like every neighbouring commit rather than announcing that a tool wrote it.
    """
    number = _pr_number(pr_url)
    suffix = f" (#{number})" if number else ""
    return f"{task_commit_subject(task_id, title, commit_type)}{suffix}"


def _pr_number(pr_url: str | None) -> str | None:
    """The trailing number of a pull-request URL, or ``None`` when it does not end in one."""
    if not pr_url:
        return None
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else None


def lifecycle_destination(task_file: str | None, final: Status) -> Path | None:
    """Where ``final`` sends a task file, or ``None`` when it sends it nowhere.

    Pure, so a plan can state the move before it happens: ``finalize`` moves a tracked file in the
    operator's own working tree and commits nothing (by contract — it may be on ``main``, and
    committing there behind their back is worse than a change they can see), which left the move
    both unannounced and uncommitted. Naming it is what the two surfaces owe.
    """
    folder_name = {Status.DONE: "done", Status.FAILED: "failed"}.get(final)
    if folder_name is None or not task_file:
        return None
    src = Path(task_file)
    parent = src.parent
    tasks_root = parent.parent if parent.name in _LIFECYCLE_FOLDERS else parent
    return tasks_root / folder_name / src.name


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    reset_fix_budget: bool = False  # --reset-fix-budget: grant a fresh consecutive fix budget
    from_node: str | None = None  # --from <node>: re-enter here instead of the checkpoint
    # A plain ``rerun`` (no --continue) on an operator-owned branch (``existing``/``current``) that
    # never reached a flow checkpoint: re-drive from the top on the branch as-is, no base reset.
    restart_in_place: bool = False
    notes: tuple[str, ...] = ()  # non-fatal advisories surfaced in --dry-run / the confirm prompt
    refusals: tuple[str, ...] = ()
    # Already-exhausted named fix loops reachable forward from the resume node, using the live
    # flow's budgets — empty unless the resume will actually land in place with counters preserved.
    exhausted_fix_loops: tuple[ExhaustedLoop, ...] = ()
    global_backstop_exhausted: bool = False  # the hard max_total_fix_iterations ceiling is spent


@dataclass(frozen=True)
class MergePlan:
    """The reconciled facts + warnings/refusals for ``merge-task`` (the read-only dry-run view)."""

    task_id: str
    found: bool = False
    status: Status | None = None
    branch: str | None = None
    base_branch: str = ""
    pr_url: str | None = None
    verify_state: str | None = None  # gh PR state when checked (MERGED/OPEN/CLOSED)
    already_merged: bool = False  # PR is MERGED on GitHub → merge-task is an idempotent no-op
    #: The subject the squash/merge commit would carry (``None`` for a rebase, which makes none).
    #: In the plan because it is the one thing a merge leaves in the base branch's history forever,
    #: and the dry run used to be silent about it.
    commit_subject: str | None = None
    warnings: tuple[str, ...] = ()  # non-fatal (e.g. PR state unverifiable; will still attempt)
    refusals: tuple[str, ...] = ()  # fatal; abort with exit 1


@dataclass(frozen=True)
class PrSyncEntry:
    """One task's outcome from ``prs --sync`` reconciliation against GitHub (one printed line)."""

    task_id: str
    pr_url: str | None
    state: str | None  # gh PR state: MERGED / CLOSED / OPEN / None (unverifiable)
    action: str  # record-merge | closed-no-merge | still-open | unverifiable
    finalized_done: bool = False  # a blocked manual_action_required task was flipped to done


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
    returns_to_base: bool = True  # whether terminal cleanup will check out base_branch (else stay)
    pr_url: str | None = None
    pr_url_source: str = "none"  # explicit | recorded | none
    verify_state: str | None = None  # gh PR state when checked (MERGED/OPEN/CLOSED)
    dirty_paths: tuple[str, ...] = ()
    #: Where the task file will move (``from``, ``to``), or ``None`` when it stays put (an
    #: ``abandoned`` finalize, or a task with no on-disk file). The move is a tracked change in the
    #: operator's working tree that ``finalize`` deliberately does not commit, so both the plan and
    #: the result say it happened rather than leaving it to be discovered in ``git status``.
    task_file_move: tuple[str, str] | None = None
    #: The spec files a decomposition root takes with it (``from``, ``to`` each), empty for an
    #: ordinary task. They ride along because a ``subtasks:`` ref is relative to the root file's
    #: directory, so a root separated from its specs cannot resolve its own manifest. Named for the
    #: same reason as the move above: they are tracked files this dirties and does not commit, and
    #: five of them appearing in ``git status`` unannounced is worse than one.
    subtask_spec_moves: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()  # non-fatal; require confirmation (no URL / not merged)
    refusals: tuple[str, ...] = ()  # fatal; abort with exit 1


def _ledger_has_manual(ledger: Ledger, task_id: str) -> bool:
    """True iff the ledger already holds an operator-finalized (``manual``) record for the id."""
    return any(r.get("id") == task_id and r.get("manual") for r in ledger.records())


def _ledger_attempt_count(ledger: Ledger, task_id: str) -> int:
    """How many terminal records the ledger already holds for ``task_id`` (prior attempts)."""
    return sum(1 for rec in ledger.records() if rec.get("id") == task_id)


def _format_predecessor_floor(
    spec: SubtaskSpec,
    commit_sha: str,
    changed_files: list[str],
    spec_path: str,
    *,
    declared: bool,
) -> str:
    """One predecessor subtask's deterministic factual floor for the handoff brief (ground truth).

    Assembled purely from artifacts that already exist — the subtask's spec (title / acceptance
    criteria / spec pointer), its committed SHA, and the files that commit changed — so it is
    present even when the supervisor (the interpretive layer) is unavailable.

    ``declared`` marks a predecessor the successor's ``depends_on`` names, which is the author's
    "build on this one" signal; the unmarked ones landed on the same branch first and are facts the
    successor still has to live with (see :meth:`Orchestrator._assemble_predecessor_context`).
    """
    criteria = "\n".join(f"  - {c}" for c in spec.acceptance_criteria) or "  - (none recorded)"
    files = "\n".join(f"  - {p}" for p in changed_files) or "  - (none)"
    marker = " (declared dependency)" if declared else ""
    return (
        f"### Subtask {spec.order:02d}: {spec.title}{marker}\n"
        f"- Commit: {commit_sha}\n"
        f"- Spec: {spec_path}\n"
        f"- Acceptance criteria:\n{criteria}\n"
        f"- Changed files:\n{files}"
    )


def effective_skip(task: NormalizedTask) -> frozenset[str]:
    """The flow node ids disabled for ``task`` — its own ``nodes.<node-id>.enabled: false``
    overrides (per-task node-disable control; the bounded per-task exception).

    The gate validated the ``nodes:`` block shape; node existence and routing soundness against the
    task's resolved flow are checked at flow resolution (``validate_disabled_nodes``), so by the
    time the engine consumes this set it is known to name real, safely-skippable nodes.
    """
    return task.disabled_nodes()


def _render_governance_section(paths: tuple[str, ...]) -> str:
    """Markdown callout for the summary / PR body: this run edited governance files.

    A reviewer-facing notice, not a report of wrongdoing — editing governance/instruction files is
    ordinary work. Appended only when ``paths`` is non-empty (no section on ordinary runs).
    """
    bullets = "\n".join(f"- `{path}`" for path in paths)
    return (
        "\n\n## Governance files changed\n\n"
        "This run edited repository governance/instruction files (a notice, not a block):\n\n"
        f"{bullets}\n"
    )


# --- Terminal-notification enrichment (read the on-disk diagnosis for the operator) --------------

_FINDING_SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def _read_failure_report(path: str | None) -> dict[str, Any] | None:
    """Load ``failure_report.json`` for the terminal notification; ``None`` when absent.

    Best-effort and total: a missing path, unreadable file, or non-object JSON yields ``None`` so
    the notification simply degrades to its terse form.
    """
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _top_blocking_finding(findings: object) -> TerminalFinding | None:
    """The single most-severe review finding (high → medium → low) for the notification.

    ``findings`` is the failure report's ``last_review_findings`` — a list of
    ``{severity, reason, paths}`` (:mod:`core.flow.nodes.evaluator`). ``None`` when there are none.
    """
    if not isinstance(findings, list):
        return None
    candidates = [f for f in findings if isinstance(f, dict)]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda f: _FINDING_SEVERITY_RANK.get(str(f.get("severity", "")).lower(), 3),
    )
    paths_raw = best.get("paths")
    paths = tuple(str(p) for p in paths_raw) if isinstance(paths_raw, list | tuple) else ()
    return TerminalFinding(
        severity=str(best.get("severity") or "unknown"),
        reason=str(best.get("reason") or ""),
        paths=paths,
    )


#: Longest finding text carried into the pathless-gating-verdict warning. The operator needs enough
#: to tell "I could not review the diff" from a real defect written without a path; the whole
#: finding is already on disk in ``findings.json``, which the log line's node id points at.
_WARNED_FINDING_MAX = 200


def _first_finding_reason(findings: Sequence[Finding]) -> str:
    """The first finding's text, bounded, for a log line — ``""`` when there is none."""
    if not findings:
        return ""
    reason = " ".join(findings[0].reason.split())
    if len(reason) <= _WARNED_FINDING_MAX:
        return reason
    return reason[: _WARNED_FINDING_MAX - 1].rstrip() + "…"


def _stuck_report_path(failure_report_path: str | None) -> str | None:
    """The operator-readable ``stuck.md`` beside ``failure_report.json``, as a POSIX path.

    ``stuck.md`` is the sibling the operator opens; only the JSON path is persisted on the row.
    Rendered with :meth:`PurePath.as_posix` so the displayed path is stable across platforms.
    """
    if not failure_report_path:
        return None
    return Path(failure_report_path).with_name(STUCK_FILENAME).as_posix()


@dataclass(frozen=True)
class PipelineResult:
    """The terminal outcome of running one task."""

    task_id: str
    final_status: Status
    pr_url: str | None = None
    validation_reason: str | None = None
    #: the offending field + cause for a validation reject — the machine ``reason`` alone is
    #: opaque ("injection_suspected"); this carries e.g. ``agents.review: forbidden flag shape`` so
    #: the operator sees WHICH field and WHY on the console without opening the JSON report.
    validation_detail: str | None = None


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
    # The frozen ``(bundle-key, sha256)`` entries accumulated while the agent inputs are frozen
    # (task packet, then the root repository instructions). Combined with the control-plane digest
    # into the composite ``instruction_manifest_digest`` by ``_finalize_instruction_bundle``.
    instruction_entries: list[tuple[str, str]] = field(default_factory=list)
    # Per-task disabled flow node ids (``nodes.<id>.enabled: false``). Re-derived every run/resume
    # from front-matter, so a restart recovers it without persistence (node-disable control).
    skip: frozenset[str] = frozenset()
    # The resolved flow's name, captured when the control bundle is bound, so the deterministic
    # report can name the pipeline that produced the change. ``None`` before the bundle exists (a
    # resume that goes terminal on the park ceiling), where the report omits that line.
    flow_name: str | None = None
    # Operator-authored decomposition built + validated pre-slot from the task's ``subtasks:``
    # manifest (fresh run only). When set, it is materialized at preflight (before branch) and the
    # planning ``proposed_by`` post-hook does not re-read the agent's proposal. ``None`` on resume —
    # the decision is rebuilt from the persisted ``subtasks`` rows (source-agnostic).
    operator_decomposition: DecompositionDecision | None = None
    # Repo-relative governance/instruction paths (`AGENTS.md`, `.agents/rules/**`, …) this
    # run's net-task diff changed, captured at finalize (before terminal cleanup restores the tree).
    # Empty on ordinary runs; drives the non-blocking operator notice (console/log WARNING, PR
    # summary, ledger record, Telegram) — editing governance files is ordinary work, never a block.
    governance_changed: tuple[str, ...] = ()
    # This task's follow-ups, carried from whichever producer computed them (the supervisor's
    # finalize turn, or the deterministic report's derivation from the evaluations) to the single
    # append site in `_finalize_task_artifacts`. Taken from memory, never re-derived from state.db.
    follow_ups: tuple[FollowUp, ...] = ()


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


class _EphemeralRunRecorder:
    """A no-op :class:`~wastech_orchestrator.core.flow.engine.RunRecorder` for the transactional
    merge-flow run (``worc merge-task``). It persists **no** flow checkpoint to the task row (so the
    ephemeral merge run never collides with ``rerun --continue`` on the task's own implementation
    flow) and no failure report. The merge run starts fresh every time and is aborted on failure, so
    there is nothing to resume."""

    def record_skip(self, node: FlowNode, *, reason: str, subtask_order: int | None) -> None:
        return None

    def save_checkpoint(self, run_state: FlowRunState) -> None:
        return None

    def write_failure_report(
        self,
        *,
        node_id: str,
        loop: str | None,
        limit_name: str,
        run_state: FlowRunState,
        subtask_order: int | None = None,
    ) -> str:
        return ""


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
        layout: RuntimeLayout,
        deny_policy: InternalDenyPolicy | None = None,
        clock: Callable[[], str] = _utc_now_iso,
        monotonic: Callable[[], float] = time.monotonic,
        notifier: Notifier | None = None,
        resolver: CheckResolver | None = None,
        heartbeat_seconds: float = 30.0,
        isolation_checks: Mapping[ProviderId, IsolationCheck] | None = None,
        host_floor_checks: Mapping[ProviderId, HostFloorCheck] | None = None,
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        self._config = config
        self._router = router
        self._git = git
        self._checks = checks
        self._store = store
        self._ledger = ledger
        self._gate = gate
        # The one provider-neutral runtime layout. Each consumer reads the surface it
        # owns: private runtime state (DB/logs/memory/HITL/process-control) from ``private_home``,
        # the operator control plane (flows/tools) from ``control_home``, and the agent-facing
        # exchange from ``exchange_root``. ``control_home`` and ``private_home`` are the same today.
        self._layout = layout
        self._artifacts_root: Path = layout.private_home
        # Internal provider deny policy: the control/private homes and the resolved
        # env-file to deny. Stored here for the adapters to project into
        # provider enforcement; not consumed yet.
        self._deny_policy = deny_policy
        # The provider-readable exchange root ``<repo>/.worc-io``, a sibling of the
        # private ``.worc`` home; every exchange builder/publisher still takes it as an argument.
        self._exchange_root = layout.exchange_root
        self._clock = clock
        self._monotonic = monotonic
        # The orchestrator-wide ``--heartbeat-seconds`` interval (shared with providers/git/checks),
        # threaded into NodeServices so the blocking HITL human-input wait heartbeats too.
        self._heartbeat_seconds = heartbeat_seconds
        self._notifier: Notifier = notifier if notifier is not None else NullNotifier()
        # The check resolver normalizes ``checks.command_sets`` at preflight (before any branch).
        # ``None`` skips it — the Check Runner then normalizes the config itself.
        self._resolver = resolver
        # Composition-root-injected ProviderId→isolation-check table for the strict_isolation
        # preflight; empty for a directly-constructed Orchestrator (binds no concrete adapter).
        self._isolation_checks: Mapping[ProviderId, IsolationCheck] = isolation_checks or {}
        # Its advisory twin: what the host cannot enforce. Injected the same way and for the same
        # reason; empty means the run says nothing about the floor rather than claiming one exists.
        self._host_floor_checks: Mapping[ProviderId, HostFloorCheck] = host_floor_checks or {}
        # The watch daemon's cross-platform stop predicate. The same callable is also wired into
        # the Router, so a stop either interrupts at a clean node boundary or suppresses fallback
        # when a hard-killed provider exits abnormally mid-node.
        self._is_cancelled = is_cancelled
        # Per-id attempt number stamped onto the next ledger record, set by ``rerun``/``continue``.
        self._rerun_attempt: dict[str, int] = {}
        # Task ids whose next resume is an operator ``rerun --continue`` that ADOPTS the current
        # on-disk control plane (re-freeze) instead of loading the frozen bundle. Set in
        # ``continue_task`` for the span of one resume; automatic crash-recovery never sets it, so
        # an agent-side control mutation before a crash is never silently adopted.
        self._continue_adopt: set[str] = set()
        # Flow registry: resolves a task's flow snapshot. Operator flows live in ``<repo>/.worc/
        # flows/`` and override packaged built-ins; passing the config turns on the
        # config-aware validation layer on every resolve, including resume.
        self._flow_registry = FlowRegistry(
            operator_flows_dir=layout.control_home / "flows",
            config=config,
        )
        # Operator tool registry: resolves a ``tool`` node's name → its executable under
        # ``<repo>/.worc/tools/`` at run time. Stateless (just the dir), built once and shared by
        # every unit's NodeServices; the FlowRegistry above validates the same tools at resolve.
        self._tool_registry = ToolRegistry(layout.control_home / "tools")
        # The constant supervisor layer — rebuilt per task in ``_engine_run`` (it carries the
        # task's own resume_own_lineage session). Single-slot, so one live instance at a time.
        self._supervisor: Supervisor | None = None

    @property
    def notifier(self) -> Notifier:
        """The notifier transport (Telegram or a null fallback) — read-only.

        Exposed for the CLI ``watch`` loop's next-task confirmation gate, which asks the
        operator before claiming a pending task. The orchestration decision (claim vs skip) stays in
        the watch loop; the orchestrator only owns the transport."""
        return self._notifier

    def _max_turns_gate_enabled(self) -> bool:
        """Whether the Claude max-turns continue/stop gate is configured on.

        Reads ``agents.providers.claude.max_turns_gate``; ``False`` when claude is not configured
        (codex-only setups never produce ``error_max_turns``). Preflight guarantees ``telegram`` is
        enabled when this is on, so a configured gate always has a live transport."""
        claude = self._config.agents.providers.get(ProviderId.CLAUDE)
        return claude is not None and claude.max_turns_gate

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

        # The IO-bearing branch-mode checks (ref existence for `existing`, detached-HEAD for
        # `current`) — pre-slot, so a bad target is quarantined before any branch is taken.
        branch_mode_reject = self._validate_branch_mode(task)
        if branch_mode_reject is not None:
            return self._reject(task_file, branch_mode_reject)

        if not self.acquire_slot(task.id):
            # Name the blocker (id + checkpoint node) so the refusal points at a resumable task, not
            # a live run — the operator can then finalize/continue it instead of guessing.
            blocking = next(
                (t for t in self._store.find_active_tasks() if t.task_id != task.id), None
            )
            if blocking is not None:
                node = self._store.get_flow_checkpoint(blocking.task_id)[0]
                where = f" at node {node}" if node else ""
                raise SlotBusyError(
                    f"another task is active: {blocking.task_id}{where}; {task.id} must wait"
                )
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

    def lookup_task(self, task_id: str) -> TaskRow | None:
        """The persisted task row for ``task_id`` (read-only), or ``None`` if unknown.

        The scanner uses this to skip a pending file whose id already reached a terminal state (its
        own leftover) instead of re-running it into a ``duplicate_task_id`` reject — a
        ``manual_action_required`` task keeps its file in ``pending/`` by design.
        """
        return self._store.get_task(task_id)

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

    def _validate_branch_mode(self, task: NormalizedTask) -> ValidationResult | None:
        """The IO-bearing branch-mode checks the IO-free gate cannot do.

        Runs after the gate but **before** the slot/branch, so a bad target is quarantined with a
        report and never reaches a branch. ``existing``: ``branch_ref`` must already exist locally
        or on the remote (no auto-create). ``current``: HEAD must be on a branch (a detached HEAD
        has nothing to commit on) — and, because it rides the operator's live checkout, it emits a
        warning (a poor fit for unattended ``watch``, though not forbidden). Returns a reject or
        ``None`` (ok).
        """
        mode = self._branch_mode(task)
        if mode is BranchMode.EXISTING:
            ref = task.branch_ref or ""  # the gate guarantees a non-empty ref for `existing`
            if not self._git.local_or_remote_branch_exists(ref):
                return ValidationResult(
                    passed=False,
                    reason=ValidationReason.INVALID_BRANCH_MODE,
                    detail=f"branch_ref {ref!r} not found locally or on origin (no auto-create)",
                )
        elif mode is BranchMode.CURRENT:
            if self._git.current_branch() is None:
                return ValidationResult(
                    passed=False,
                    reason=ValidationReason.INVALID_BRANCH_MODE,
                    detail="branch_mode 'current' needs a branch, but HEAD is detached",
                )
            self._log(task.id).warning(
                "branch_mode 'current' rides the working tree's live checkout — a poor fit for "
                "unattended watch; the task commits on whatever branch HEAD is on"
            )
        return None

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
                hint = self._replacement_hint(dep, row.title)
                detail = f"dependency '{dep}' is {row.status.value} (unmerged){hint}"
                return Eligibility.WAITING, detail
            return Eligibility.WAITING, f"dependency '{dep}' is in flight ({row.status.value})"
        if dep in pending:
            return Eligibility.WAITING, f"dependency '{dep}' is pending (not yet run)"
        if self._ledger.has_task_id(dep):
            records = self._ledger.records()
            done = any(
                rec.get("id") == dep and rec.get("final_status") == Status.DONE.value
                for rec in records
            )
            if done:
                return self._dependency_merged(dep)
            dep_title = next((rec.get("title") for rec in records if rec.get("id") == dep), None)
            hint = self._replacement_hint(dep, dep_title, records=records)
            return Eligibility.WAITING, f"dependency '{dep}' terminated unmerged{hint}"
        return Eligibility.BROKEN, f"depends on unknown task '{dep}'"

    def _replacement_hint(
        self, dep: str, dep_title: str | None, *, records: Sequence[Mapping[str, Any]] | None = None
    ) -> str:
        """A 'did you mean X?' hint when an abandoned dependency was retried under a NEW id.

        abandon+retry-under-a-new-id leaves every dependent pointing at the dead id forever with no
        clue that a done replacement exists. Scan the ledger for a later ``done`` record sharing the
        dead dep's title and name it — advisory only (never auto-relink; too implicit). Returns an
        empty string when there is no confident same-title match."""
        if not dep_title:
            return ""
        norm = " ".join(dep_title.lower().split())
        rows = records if records is not None else self._ledger.records()
        for rec in rows:
            other_id = rec.get("id")
            if (
                other_id != dep
                and rec.get("final_status") == Status.DONE.value
                and " ".join(str(rec.get("title", "")).lower().split()) == norm
            ):
                return f" — '{dep}' looks abandoned; did you mean '{other_id}' (same title, done)?"
        return ""

    def _dependency_merged(self, dep: str) -> tuple[Eligibility, str]:
        """A terminal-``DONE`` dependency: satisfied iff its PR is merged (or local-commit mode)."""
        pr_url = self._git.recorded_pr_url(dep)
        if pr_url is None:
            return Eligibility.ELIGIBLE, ""  # local-commit mode: DONE means commits on base
        state, sha = self._git.pr_merge_state(pr_url)
        if state == "MERGED":
            # The daemon auto-advances a dependent on this LIVE merged-PR check. Persist the
            # `pr_merge` audit op here (idempotent, no network) so the merge event is recorded even
            # when the PR was merged out of band — otherwise the audit ledger of merge events is
            # incomplete for watch-driven merge-gated tasks (the only other `pr_merge` writer is
            # `worc prs --sync`, which refuses while the daemon owns the clone).
            self._git.record_external_merge(dep, pr_url)
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
        a file, use it; otherwise search ``tasks/{pending,done,failed}/`` for the task by
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

    def _worktree_is_task_output(self, task_id: str) -> bool:
        """Whether a dirty working tree is the task's own uncommitted work rather than foreign.

        True once a node that writes to the tree has run for this task: an ``agent`` node (the
        writers themselves) or an ``evaluator`` / ``checks`` / ``publish`` node (which operate on
        *produced* code, so writing already happened upstream). Counting the writers directly is
        what closes the blind window every flow has between its first writing node and its first
        critic — with only the downstream kinds counted, a task parked mid-writing was told its own
        finished work was foreign and had to be thrown away before ``--continue`` would run. Before
        any of those kinds has run (nothing entered the graph yet, or only ``tool`` / ``hitl`` nodes
        did) a dirty tree is almost certainly foreign and stays refused. Read off the recorded
        ``node_runs`` (``node_kind``) rather than re-loading the flow, so it needs no ``task_type``
        and stays correct even if the flow file drifted since the interrupted run.

        Two known limitations, both needing a carrier ``node_runs`` does not have today:

        * A ``read-only`` agent node counts like a writing one — ``node_kind`` is all the row
          records, and telling the two apart needs a ``permission_profile`` column. In advanced mode
          every node class has a shell anyway, so a ``read-only`` node writes no worse than a
          ``workspace-write`` one.
        * The answer is sticky across attempts: ``get_node_runs`` returns the task's whole history,
          so a task that once passed a writing node keeps answering True even if the current attempt
          parked before writing anything. Bounding it to the current attempt needs a per-attempt
          marker on the row.
        """
        return any(
            r.node_kind in ("agent", "evaluator", "checks", "publish")
            for r in self._store.get_node_runs(task_id)
        )

    def plan_rerun(
        self,
        task_id: str,
        *,
        continue_mode: bool = False,
        force_reset_remote: bool = False,
        reset_fix_budget: bool | None = None,
        from_node: str | None = None,
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
        # A stale ``running`` row is a killed/crashed task, directly recoverable: ``cmd_rerun``
        # already refused if any executor owned the slot — the watch daemon or a ``worc run``, each
        # with its own liveness marker — so a ``running`` row reaching here belongs to no live
        # process (the "parked (no daemon)" state) and needs no ``finalize --as failed`` dance. The
        # active-slot check below excludes this task's own id, so it never self-blocks.
        if row.status not in RERUN_ELIGIBLE_STATUSES:
            refusals.append(
                f"task '{task_id}' is {row.status.value}; rerun is for failed / "
                "manual_action_required / stale-running tasks (use `run` for a new task)"
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
        interrupted_node: str | None = None
        has_remote = False
        pr_url: str | None = None
        notes: list[str] = []
        current_node: str | None = None
        counters_json: str | None = None
        fingerprint: str | None = None
        resume_tolerates_wip = False
        if continue_mode:
            # --continue re-enters at the flow checkpoint (current_node); the node id is surfaced
            # in the dry-run view.
            current_node, counters_json, fingerprint = self._store.get_flow_checkpoint(task_id)
            interrupted_node = current_node
            if not current_node:
                refusals.append(
                    "no recoverable node was recorded for this task; use a fresh `rerun` "
                    "(without --continue)"
                )
            else:
                resume_tolerates_wip = self._worktree_is_task_output(task_id)
        # A fresh rerun resets the branch to base, so a dirty tree would be
        # destroyed and is always refused. On ``--continue`` the branch is reused (never reset) and
        # the task's own uncommitted work is the legitimate input to a writing / review / fixing /
        # publish re-entry — tolerated once ``resume_tolerates_wip`` holds (a node that writes to
        # the tree has run). Before that a dirty tree is still unexpected and refused. Artifact
        # dirs (`.worc/`, tasks/) stay excluded by ``unaccounted_dirty_paths`` in every mode.
        dirty = self._git.unaccounted_dirty_paths()
        if dirty and not resume_tolerates_wip:
            refusals.append(
                f"the working tree has unaccounted changes ({', '.join(sorted(dirty))}); "
                "resolve them before rerun"
            )
        elif dirty:
            # The task's own WIP is preserved across the resume and is committed only when the flow
            # reaches the publish node — never on a re-park to manual. Known limitation:
            # commit_code then stages ALL non-artifact dirty paths, so any foreign WIP on the branch
            # is swept into that commit (own-vs-foreign discrimination is a deferred follow-up).
            notes.append(
                f"uncommitted changes ({', '.join(sorted(dirty))}) are preserved and will be "
                "committed into the task when the flow reaches publish"
            )
        # --reset-fix-budget and --from are continue-only controls.
        if reset_fix_budget is not None and not continue_mode:
            refusals.append("--reset-fix-budget requires --continue")
        if from_node is not None and not continue_mode:
            refusals.append("--from requires --continue")
        exhausted: tuple[ExhaustedLoop, ...] = ()
        backstop_exhausted = False
        if continue_mode and current_node:
            live = self._persisted_flow_snapshot(task_id)
            resume_node = from_node or current_node
            # Operator --continue adopts the current on-disk control plane and resumes at
            # resume_node (the --from override, or the checkpoint node), so that node must still
            # exist in the edited flow. --from is an explicit override; the checkpoint node is
            # implicit — a flow edit that removed it is refused with an actionable message.
            resume_refusals = self._resume_node_refusals(
                resume_node, live=live, is_from=from_node is not None
            )
            refusals.extend(resume_refusals)
            resumes_in_place = not resume_refusals
            if (
                resumes_in_place
                and live is not None
                and self._control_plane_drifted(task_id, live, checkpoint_fingerprint=fingerprint)
            ):
                notes.append(
                    "the control plane changed since the checkpoint; --continue will adopt the "
                    f"current on-disk flow/roles/tools and resume at '{resume_node}'"
                )
            # The resume adopts and lands at resume_node under the live flow, so evaluate the live
            # fix-loop budgets there (a vanished resume node was already refused above).
            if resumes_in_place and live is not None:
                counters = json.loads(counters_json) if counters_json else {}
                exhausted = tuple(
                    exhausted_fix_loops(
                        live, counters, self._config.agents.max_fix_cycles, resume_node
                    )
                )
                backstop_exhausted = global_backstop_exhausted(
                    live.doc.budgets, self._config.agents.max_total_fix_iterations, counters
                )
        restart_in_place = False
        display_branch = row.branch
        if not continue_mode:
            # A fresh rerun resets the branch to base (delete + recreate) — safe only on a branch
            # the orchestrator owns (``new`` mode). On an operator-owned branch (``existing``/
            # ``current``) the outcome depends on whether the run produced any work: a checkpoint
            # means resume in place with --continue; no checkpoint means the run died before any
            # work, so there is nothing to reset and no resume point — restart it in place.
            rerun_mode = self._persisted_branch_mode(task_id)
            if rerun_mode is BranchMode.NEW:
                pr_url = self._git.recorded_pr_url(task_id)
                has_remote = bool(row.branch) and self._git.remote_branch_exists(row.branch or "")
                if (has_remote or pr_url) and not force_reset_remote:
                    # A note, not a refusal: a leftover remote branch or open PR is ordinary
                    # working state that publishing reuses (the push recovers from a diverged
                    # remote, and an open PR on this head is appended to and recorded). Refusing
                    # here forced `finalize` or --force-reset-remote for a condition that needs
                    # neither. --force-reset-remote remains for deliberately deleting the branch.
                    notes.append(
                        f"a prior attempt left a remote branch / open PR ({pr_url or row.branch}); "
                        "it will be reused — pass --force-reset-remote to delete the remote branch "
                        "instead (this closes the PR)"
                    )
            elif self._store.get_flow_checkpoint(task_id)[0]:
                refusals.append(
                    f"task '{task_id}' runs in branch_mode '{rerun_mode.value}' (operator-owned); "
                    "a fresh rerun would reset a branch the orchestrator does not own. Use "
                    "`rerun --continue` to resume in place, or clean up the branch manually"
                )
            else:
                restart_in_place = True
                display_branch = self._restart_display_branch(task_id, rerun_mode) or row.branch
                notes.append(
                    f"restart-in-place: re-drives from the top on branch '{display_branch}' "
                    "without resetting it (no checkpoint to resume)"
                )
        return RerunPlan(
            task_id=task_id,
            continue_mode=continue_mode,
            found=True,
            current_status=row.status,
            source_path=source_path,
            branch=display_branch,
            base_branch=self._config.repo.base_branch,
            attempt=_ledger_attempt_count(self._ledger, task_id) + 1,
            interrupted_node=interrupted_node,
            dirty_paths=tuple(sorted(dirty)),
            has_remote_branch=has_remote,
            pr_url=pr_url,
            reset_fix_budget=reset_fix_budget is True,
            from_node=from_node,
            notes=tuple(notes),
            refusals=tuple(refusals),
            exhausted_fix_loops=exhausted,
            global_backstop_exhausted=backstop_exhausted,
            restart_in_place=restart_in_place,
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
        # Defense-in-depth over ``plan_rerun``'s refusal: never reset a branch the orchestrator does
        # not own (``existing``/``current``). The CLI already gates on the refusal; this guards the
        # public API path too.
        rerun_mode = self._persisted_branch_mode(task_id)
        if rerun_mode is not BranchMode.NEW:
            raise PipelineFailed(
                f"cannot fresh-rerun '{task_id}' in branch_mode '{rerun_mode.value}' (operator-"
                "owned branch); use `rerun --continue` to resume in place"
            )
        slug = row.slug or slugify(row.title)
        prior = _ledger_attempt_count(self._ledger, task_id)
        archive_task_artifacts(self._artifacts_root, task_id, prior)
        # Fresh rerun starts with a clean exchange; the run re-publishes into it.
        clear_exchange_task_dir(self._exchange_root, task_id)
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

    def restart_task_in_place(self, task_id: str, *, source_path: str) -> PipelineResult:
        """Re-drive a terminal task from the top on its existing (operator-owned) branch.

        For a pre-checkpoint failure (no flow checkpoint recorded) in ``existing``/``current`` mode
        there is no per-attempt work to reset and no resume point, so clear the DB row state and
        re-run, reusing the branch as-is. Unlike ``rerun_task`` this never touches git (no
        reset-to-base) — the branch is the operator's. Preserves the ledger attempt linkage.
        """
        row = self._store.get_task(task_id)
        if row is None:
            raise PipelineFailed(f"unknown task id '{task_id}'")
        # Defense-in-depth over ``plan_rerun``'s routing: restart-in-place is only for an
        # operator-owned branch (it must never reset one) and only when nothing was checkpointed
        # (a checkpoint means there is work to resume via --continue).
        rerun_mode = self._persisted_branch_mode(task_id)
        if rerun_mode is BranchMode.NEW:
            raise PipelineFailed(
                f"cannot restart-in-place '{task_id}' in branch_mode 'new'; use a fresh `rerun`"
            )
        current_node = self._store.get_flow_checkpoint(task_id)[0]
        if current_node:
            raise PipelineFailed(
                f"'{task_id}' has a recorded checkpoint at '{current_node}'; use `rerun --continue`"
            )
        prior = _ledger_attempt_count(self._ledger, task_id)
        archive_task_artifacts(self._artifacts_root, task_id, prior)
        # Restart-in-place also starts clean; the run re-publishes into the exchange.
        clear_exchange_task_dir(self._exchange_root, task_id)
        self._store.reset_task_for_rerun(task_id)  # DB-only reset; the branch is left untouched
        self._rerun_attempt[task_id] = prior + 1
        self._log(task_id).info("rerun: restart in place", extra={"attempt": prior + 1})
        return self.run_task(source_path)

    def continue_task(
        self,
        task_id: str,
        *,
        reset_fix_budget: bool = False,
        from_node: str | None = None,
    ) -> PipelineResult:
        """Fix-and-continue: revive a terminal task at the stage it failed and resume it.

        Reuses the existing branch and all prior work; only the terminal markers are cleared and
        any un-answered HITL prompt is reset so the re-entered stage asks fresh. The whole pipeline
        re-run is delegated to the resume engine (``resume`` → ``_resume_task``), which
        idempotently re-enters at the recovered stage. Two optional operator controls patch the
        checkpoint first: ``reset_fix_budget`` grants a fresh consecutive fix budget (the global
        backstop is untouched) and ``from_node`` re-enters at a chosen node instead of the recorded
        one.
        """
        row = self._store.get_task(task_id)
        if row is None:
            raise PipelineFailed(f"unknown task id '{task_id}'")
        current_node, counters_json, fingerprint = self._store.get_flow_checkpoint(task_id)
        if not current_node:
            raise PipelineFailed(
                f"cannot continue '{task_id}': no recoverable stage recorded; use a fresh rerun"
            )
        self._rerun_attempt[task_id] = _ledger_attempt_count(self._ledger, task_id) + 1
        self._apply_continue_controls(
            task_id,
            current_node=current_node,
            counters_json=counters_json,
            fingerprint=fingerprint,
            reset_fix_budget=reset_fix_budget,
            from_node=from_node,
        )
        reset = reset_pending_interactions(self._artifacts_root, task_id)
        if reset:
            self._log(task_id).info(
                "rerun --continue: reset pending HITL", extra={"reset": len(reset)}
            )
        # Revive the terminal task as active; the resume engine re-enters at the persisted
        # ``current_node`` (the flow checkpoint, possibly overridden above), reusing branch + work.
        self._store.revive_task_for_continue(task_id, Status.RUNNING)
        self._log(task_id).info(
            "rerun --continue: revived", extra={"node": from_node or current_node}
        )
        # Adopt the current on-disk control plane on this operator resume (consumed in
        # ``_engine_run``). Scoped to this call via ``finally`` so a resume that never reaches the
        # engine cannot leave the flag set for a later automatic crash-recovery to pick up.
        self._continue_adopt.add(task_id)
        try:
            result = self.resume()
        finally:
            self._continue_adopt.discard(task_id)
        if result is None:
            raise PipelineFailed(f"continue '{task_id}' did not resume (no active task found)")
        return result

    def _apply_continue_controls(
        self,
        task_id: str,
        *,
        current_node: str,
        counters_json: str | None,
        fingerprint: str | None,
        reset_fix_budget: bool,
        from_node: str | None,
    ) -> None:
        """Rebaseline the persisted flow checkpoint for a ``rerun --continue`` before ``resume()``
        hydrates it — the one-shot operator seam (also applies ``--reset-fix-budget`` / ``--from``).

        Deliberately here and not in ``hydrate``/``_resume_via_engine``: that path is shared by
        ordinary crash-recovery, so applying a budget grant there would re-grant on every restart
        and escape ``max_fix_cycles``. Here it is applied exactly once per ``--continue``. A budget
        grant preserves the global ``fix_iterations`` / ``total_fix:*`` counters, so the
        ``max_total_fix_iterations`` backstop is never weakened, even across repeated grants.

        A ``--continue`` adopts the live control plane (``_prepare_control_bundle`` re-freezes
        it). When the flow YAML drifted, the checkpoint fingerprint is rebaselined to the live
        flow's — else ``_resume_via_engine``'s equality gate would mismatch the re-frozen flow and
        route to manual. A no-drift ``--continue`` with no controls returns early (a rewrite would
        re-derive the mirrored global fix counter for nothing); a role/tool-only edit leaves the
        ``flow_fingerprint`` unchanged (what the gate keys off), so it needs no rebaseline either.
        """
        baseline_fingerprint = fingerprint or ""
        live = self._persisted_flow_snapshot(task_id)
        if live is not None:  # defensive: plan_rerun already required a resolvable/valid flow
            baseline_fingerprint = live.flow_fingerprint
        if (
            not reset_fix_budget
            and from_node is None
            and baseline_fingerprint == (fingerprint or "")
        ):
            return
        run_state = FlowRunState(
            flow_fingerprint=baseline_fingerprint,
            current_node=from_node or current_node,
            loop_counters=json.loads(counters_json) if counters_json else {},
        )
        if reset_fix_budget:
            run_state.reset_consecutive_fix_budget()
        self._store.save_flow_checkpoint(
            task_id,
            current_node=run_state.current_node,
            counters_json=json.dumps(run_state.loop_counters, sort_keys=True),
            flow_fingerprint=run_state.flow_fingerprint,
            fix_iterations=run_state.fix_iterations,
        )
        if reset_fix_budget:
            # Keep the operator-facing scalar mirror consistent right away (the authoritative run
            # state re-syncs it at the terminal transition too).
            counters = self._store.get_counters(task_id)
            self._store.save_counters(
                task_id, replace(counters, test_fix_cycles=0, review_fix_cycles=0)
            )
        self._log(task_id).info(
            "rerun --continue: applied controls",
            extra={
                "reset_fix_budget": reset_fix_budget,
                "from_node": from_node,
                "fix_iterations": run_state.fix_iterations,
            },
        )

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
        task_move: tuple[str, str] | None = None
        spec_moves: tuple[tuple[str, str], ...] = ()
        if row.source_path and (dest := lifecycle_destination(row.source_path, declared)):
            task_move = (row.source_path, str(dest))
            root = Path(row.source_path)
            spec_moves = tuple(
                (str(root.parent / ref), str(dest.parent / ref))
                for ref in read_subtask_refs(root)
                if (root.parent / ref).is_file()
            )
        return FinalizePlan(
            task_id=task_id,
            declared=declared,
            found=True,
            current_status=row.status,
            source_path=row.source_path,
            branch=row.branch,
            base_branch=self._config.repo.base_branch,
            returns_to_base=self._git.returns_to_base(self._persisted_branch_mode(task_id)),
            pr_url=resolved_url,
            pr_url_source=source,
            verify_state=verify_state,
            dirty_paths=tuple(sorted(dirty)),
            task_file_move=task_move,
            subtask_spec_moves=spec_moves,
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
        cleanup = self._git.terminal_cleanup(task_id, mode=self._persisted_branch_mode(task_id))
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
        # Finalize reconciles a task the orchestrator never terminated itself (e.g. stopped
        # mid-flow, finished by hand). The operator-facing counter columns are only mirrored at a
        # clean terminal transition, so without this they stay stale at the last sync while the
        # authoritative flow checkpoint holds the real churn. Mirror them from the checkpoint so
        # ``status`` / the ledger report the true fix-loop totals; ``None`` = the engine never ran.
        checkpoint = hydrate_run_state(self._store, task_id)
        if checkpoint is not None:
            self._store.save_counters(task_id, LoopCounters.from_run_state(checkpoint))
        self._store.set_status(task_id, declared)  # out-of-band operator override (no assert)
        # The hand-finish path is the common landing spot after a ``--force-full`` stop,
        # which SIGKILLs the daemon mid-node and leaves orphan ``running`` node runs + an unbilled
        # killed attempt. Reconcile them here so the aborted run is auditable (closed nodes + an
        # ``unknown`` attempt row) and logged, rather than silently stranded.
        self._reconcile_open_node_runs(
            task_id, reason=note or f"finalized as {declared.value} by operator"
        )
        # The operator finalize/merge/PR-sync paths are terminal producers that bypass
        # ``_go_terminal``, so they must seal the exchange too. Idempotent — a no-op when the
        # pipeline terminal already sealed and removed the active exchange for this task.
        self._seal_terminal_exchange(task_id, final=declared)
        self._evict_run_artifacts(task_id, final=declared)
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
                advanced_mode=self._advanced_mode,
            )
        )
        self._log(task_id).info(
            "finalized", extra={"final_status": declared.value, "pr_url": pr_url, "manual": True}
        )
        return PipelineResult(task_id=task_id, final_status=declared, pr_url=pr_url)

    def plan_merge(self, task_id: str, *, verify: bool = True) -> MergePlan:
        """Gather the facts + warnings/refusals for ``merge-task`` (read-only; mutates nothing)."""
        row = self._store.get_task(task_id)
        if row is None:
            return MergePlan(
                task_id=task_id, found=False, refusals=(f"unknown task id '{task_id}'",)
            )
        refusals: list[str] = []
        warnings: list[str] = []
        if any(t.task_id != task_id for t in self._store.find_active_tasks()):
            refusals.append("another task owns the processing slot; merge-task needs it idle")
        pr_url = self._git.recorded_pr_url(task_id)
        if pr_url is None:
            refusals.append(f"task '{task_id}' has no recorded PR to merge")
        verify_state: str | None = None
        already_merged = False
        if pr_url is not None and verify:
            verify_state = self._git.verify_pr_state(pr_url)
            if verify_state == "MERGED":
                already_merged = True  # idempotent: merge-task will just record it
            elif verify_state == "CLOSED":
                refusals.append(f"the PR is CLOSED (not merged); nothing to do: {pr_url}")
            elif verify_state is None:
                warnings.append(
                    "could not verify PR state (gh offline/unauthenticated); will still attempt"
                )
            # OPEN → proceed
        return MergePlan(
            task_id=task_id,
            found=True,
            status=row.status,
            branch=row.branch,
            base_branch=self._config.repo.base_branch,
            pr_url=pr_url,
            verify_state=verify_state,
            already_merged=already_merged,
            commit_subject=merge_commit_subject(
                task_id, row.title, pr_url, self._persisted_commit_type(task_id)
            ),
            warnings=tuple(warnings),
            refusals=tuple(refusals),
        )

    def merge_task(
        self,
        task_id: str,
        *,
        strategy: MergeStrategy,
        wait_for_checks: bool,
        resolve: bool = True,
    ) -> PipelineResult:
        """Operator go-ahead: pull base into the task branch, resolve any conflicts (agent-assisted
        via the merge flow), then merge the PR. The human-in-the-loop counterpart to ``auto_merge``.

        Holds the single slot for its duration and cleans up after itself, so it needs no new task
        status. Transactional: on any failure it runs ``git merge --abort`` and leaves the PR open.
        Git/pipeline failures surface as :class:`PipelineFailed` (a ``DONE`` task is never
        downgraded); a staging gate that demands a human keeps its own class and is re-raised.
        Idempotent: a PR that is already merged (through us earlier, or out of band) is recorded
        and succeeds without re-merging. ``resolve=False`` aborts on a conflict instead of
        launching the merge flow."""
        row = self._store.get_task(task_id)
        if row is None:
            raise PipelineFailed(f"unknown task id '{task_id}'")
        if any(t.task_id != task_id for t in self._store.find_active_tasks()):
            raise PipelineFailed("another task owns the processing slot; merge-task needs it idle")
        pr_url = self._git.recorded_pr_url(task_id)
        if pr_url is None:
            raise PipelineFailed(f"task '{task_id}' has no recorded PR to merge")

        state = self._git.verify_pr_state(pr_url)
        if state == "MERGED":  # merged already (through us earlier or out of band) → record + done
            self._git.record_external_merge(task_id, pr_url)
            return self._merge_finalize(row, pr_url)
        if state == "CLOSED":
            raise PipelineFailed(f"the PR is CLOSED (not merged); nothing to do: {pr_url}")
        # OPEN or unverifiable → proceed.

        branch = row.branch
        if not branch:
            raise PipelineFailed(f"task '{task_id}' has no recorded branch to merge")

        log = self._log(task_id)
        self._git.merge_abort()  # clear a stale merge from a prior crash before starting
        try:
            conflicted = self._git.update_branch_with_base(branch, self._config.repo.base_branch)
            if conflicted:
                log.info("[MERGE-TASK] conflicts; running merge flow", extra={"branch": branch})
                if not resolve:
                    raise PipelineFailed(
                        f"base merge conflicts and --no-resolve was set; PR left open: {pr_url}"
                    )
                p = self._degraded_pipeline(row)  # minimal pipeline from the stored row
                if not self._run_merge_flow(p, self._resolve_merge_flow()):
                    raise PipelineFailed(
                        f"the merge flow produced no clean, passing tree; PR left open: {pr_url}"
                    )
            else:
                log.info("[MERGE-TASK] clean base merge", extra={"branch": branch})
            # Finalize the merge — the clean ``--no-commit`` staged tree OR the resolved
            # conflict — through the gated commit path. A no-op when nothing is in flight (a
            # fast-forward / already-current branch), so those still make no commit.
            self._git.commit_merge_resolution(
                task_id, f"merge({task_id}): integrate base '{self._config.repo.base_branch}'"
            )
            self._git.push_branch_update(task_id, branch)
            outcome = self._git.merge_pr(
                task_id,
                pr_url,
                strategy=strategy,
                wait_for_checks=wait_for_checks,
                subject=merge_commit_subject(
                    task_id, row.title, pr_url, self._persisted_commit_type(task_id)
                ),
                # No body: every commit this orchestrator makes on the branch is a single line, so
                # a body assembled from them can only be a list of internal subjects — which is how
                # `chore(orchestrator): audit trail` reached a real `main`. The readable account of
                # the change is the pull request body, which stays on the pull request.
                body="",
            )
            log.info("[MERGE-TASK] merged", extra={"pr_url": pr_url, "outcome": outcome})
        except (GitCommandError, PipelineFailed) as exc:
            self._git.merge_abort()  # transactional: restore the tree, leave the PR open
            raise PipelineFailed(f"merge-task failed: {exc}") from exc
        except Exception:
            # The staging gates inside `commit_merge_resolution` raise ManualActionRequired, not
            # GitCommandError, so without this clause they escape past the abort and leave the tree
            # mid-merge, which blocks cleanup and the next task. Abort here too and re-raise
            # unchanged, so the class of the outcome (a block a human must clear) is preserved
            # rather than downgraded.
            self._git.merge_abort()
            raise
        return self._merge_finalize(row, pr_url)

    def _merge_finalize(self, row: TaskRow, pr_url: str) -> PipelineResult:
        """Record the terminal outcome of a successful operator merge.

        The merge itself already persists in the ``pr_merge`` publish op (which unblocks
        ``depends_on`` dependents). A task that was ``manual_action_required`` because its earlier
        auto-merge was blocked is flipped to ``DONE`` via the operator ``finalize`` path (its block
        is now resolved); a ``DONE`` task stays ``DONE`` — never re-finalized."""
        if row.status is Status.MANUAL_ACTION_REQUIRED:
            return self.finalize_task(
                row.task_id, declared=Status.DONE, pr_url=pr_url, note="merged via merge-task"
            )
        return PipelineResult(task_id=row.task_id, final_status=row.status, pr_url=pr_url)

    def sync_external_merges(self, *, write: bool) -> list[PrSyncEntry]:
        """Reconcile open-PR tasks against GitHub for ``prs --sync``: probe each, record externally
        merged ones (the counterpart to ``merge-task`` for PRs merged directly on GitHub).

        Read-only when ``write`` is False (the dry-run): every task is probed, nothing is written.
        When ``write`` is True, a ``MERGED`` PR gets a ``pr_merge`` publish op (unblocking
        ``depends_on`` dependents) and, if the task was ``manual_action_required`` because its merge
        was blocked, is finalized to ``DONE`` via the ``finalize`` path. ``CLOSED`` (not merged) and
        still-``OPEN`` PRs are only reported. Idempotent: an already-recorded merge never re-appears
        (it drops out of ``find_open_pr_tasks``)."""
        entries: list[PrSyncEntry] = []
        for row in self._store.find_open_pr_tasks():
            pr_url = self._git.recorded_pr_url(row.task_id)
            state = self._git.verify_pr_state(pr_url) if pr_url else None
            if state == "MERGED" and pr_url is not None:
                finalized = False
                if write:
                    self._git.record_external_merge(row.task_id, pr_url)
                    if row.status is Status.MANUAL_ACTION_REQUIRED:
                        self.finalize_task(
                            row.task_id,
                            declared=Status.DONE,
                            pr_url=pr_url,
                            note="merged externally (prs --sync)",
                        )
                        finalized = True
                entries.append(PrSyncEntry(row.task_id, pr_url, state, "record-merge", finalized))
            elif state == "CLOSED":
                entries.append(PrSyncEntry(row.task_id, pr_url, state, "closed-no-merge"))
            elif state is None:
                entries.append(PrSyncEntry(row.task_id, pr_url, state, "unverifiable"))
            else:  # OPEN
                entries.append(PrSyncEntry(row.task_id, pr_url, state, "still-open"))
        return entries

    def refresh_repo(self) -> None:
        """Best-effort fetch/pull of ``base_branch`` so git-pushed tasks become visible.

        Called by the ``watch`` loop between ticks. Delegates to the Git Manager, which no-ops
        unless the working copy is on ``base_branch`` (the slot is free after terminal cleanup), so
        it never disturbs an active or interrupted task branch.
        """
        self._git.refresh_base()

    def resume(self) -> PipelineResult | None:
        """Reconcile persisted state on startup and resume the single unfinished task.

        Returns the terminal result of the resumed task, or ``None`` when the slot is free so a
        caller may pick a pending task — no active task, no interrupted cleanup, or a cleanup that
        stays blocked (a terminal task's stuck cleanup owns no slot and must not freeze the
        queue; it is recorded + logged and re-tried each tick).
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
                        advanced_mode=self._advanced_mode,
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
        """Finish an interrupted terminal cleanup once (checkout base or stay per the task's branch
        mode / ``repo.checkout_base_on_cleanup``), then append the ledger record.

        The cleanup decision mirrors the primary terminal path (:meth:`_resume_terminal_cleanup`),
        so it honors the quiescence barrier and preserves a resumable manual-park's own WIP
        — the retry must not undo what ``_go_terminal`` deliberately did.

        Returns the terminal result when the cleanup completes; returns ``None`` when it stays
        blocked so the caller treats the slot as free (a terminal task is ``_NON_ACTIVE`` and owns
        no slot) and scans ``pending/``. A stuck janitorial cleanup on an already-terminal task must
        not freeze the whole queue: the block is recorded (``cleanup_last_error``) and
        logged, the next task still fail-closes at its own pre-launch guards, and the blocked task
        is re-elected each tick so cleanup self-heals once the operator clears the tree.
        """
        if task_id is None:
            return None
        row = self._store.get_task(task_id)
        if row is None:
            return None
        cleanup = self._resume_terminal_cleanup(task_id, row.status)
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
                    advanced_mode=self._advanced_mode,
                )
            )
            self._notify_terminal(
                task_id=task_id,
                final_status=row.status,
                pr_url=None,
                reason=cleanup.error,
            )
        if not cleanup.safe:
            # The cleanup is stuck, but this task is already terminal and holds no slot, so
            # the queue is NOT frozen — return None so ``watch_once`` proceeds to scan pending.
            self._log(task_id).warning(
                "terminal cleanup still blocked on resume; queue not frozen (task is terminal and "
                "owns no slot) — pending tasks are still scanned. Clear the working tree, then "
                "rerun/finalize.",
                extra={"reason": cleanup.error},
            )
            return None
        return PipelineResult(task_id=task_id, final_status=row.status)

    def _resume_terminal_cleanup(self, task_id: str, status: Status) -> CleanupOutcome:
        """Compute the terminal-cleanup outcome for the resume path, matching ``_go_terminal``.

        Kept in lockstep with the primary path's cleanup decision: the quiescence barrier
        withholds any Git action over a tree not proven quiescent, and a resumable manual-park
        carrying the task's own WIP preserves it instead of fail-closing on ``unaccounted
        changes``. Drift between this and ``_go_terminal`` is exactly the regression to avoid.
        """
        if self._exchange_active_unsafe(task_id):
            return CleanupOutcome(
                safe=False,
                target_branch=self._config.repo.base_branch,
                error="terminal cleanup withheld: provider tree not proven quiescent",
            )
        preserve_own_wip = (
            status is Status.MANUAL_ACTION_REQUIRED and self._worktree_is_task_output(task_id)
        )
        return self._git.terminal_cleanup(
            task_id, mode=self._persisted_branch_mode(task_id), preserve_own_wip=preserve_own_wip
        )

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
            # _restore_engine_inputs owns the plan path on resume: it republishes plan.md into the
            # exchange when it exists. No private pre-seed here (it would leak a private
            # path when planning has not produced plan.md yet).
            plan_path=None,
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
        A task with no flow checkpoint (interrupted before the engine started) restarts from the
        A checkpointed continue reuses the **frozen** control bundle (verified in
        :meth:`_engine_run`), so a live-flow edit never silently restarts the run — it is a
        parked-conflict the operator resolves with a fresh rerun/restart. Side-effect idempotency
        (commit/push/PR) lives in ``publish_operations``, so a resumed run never duplicates them."""
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
        row = self._store.get_task(p.task.id)
        if row is not None and self._still_blocked(row.blocked_until):
            # A provider named the instant its own limit window reopens, so this tick is one cheap
            # no-op instead of a full re-entry that would prepare git, launch an agent, and be
            # refused in seconds. Deliberately AFTER the ceiling — that must always win over a
            # provider-supplied instant — and before any git or provider work. The caller stops on a
            # non-terminal resume, so the slot stays held and the wait is bounded by the poll
            # interval; there is no timer and no sleep, and that bound is the whole point.
            self._log(p.task.id).info(
                "parked task waiting on a provider window",
                extra={"blocked_until": row.blocked_until},
            )
            return PipelineResult(task_id=p.task.id, final_status=Status.RUNNING)
        if run_state is None:
            # No usable checkpoint (interrupted before the engine wrote one) → restart from the top
            # via the full driver (re-does preflight + branch prep + a fresh freeze + engine).
            if p.status not in (Status.NEW, Status.PENDING, Status.VALIDATED):
                self._store.set_status(p.task.id, Status.VALIDATED)  # reset to re-enter the driver
                p.status = Status.VALIDATED
            return self._drive_via_engine(p, self._gate.phase_b(p.task))
        self._announce_security_posture(p)
        self._check_preflight(p)  # re-resolve the launchable check profile (idempotent)
        p.branch = self._git.prepare_branch(
            p.task.id,
            p.slug,
            epoch=int(time.time()),  # shadowed by the persisted branch override on a normal resume
            branch_name=p.branch or p.task.branch_name,
            # The mode has to travel with the resume: without it an `existing`/`current` task
            # re-entered through the `new` path, which never restores the task's own start commit —
            # and the dangerous-diff gate then measured from `base_branch`, i.e. from the whole
            # unmerged chain, so `rerun --continue` asked about previous tasks' deletions.
            mode=self._branch_mode(p.task),
            branch_ref=p.task.branch_ref,
        )  # re-attach the existing branch (reused)
        self._store.update_task(p.task.id, branch=p.branch, slug=p.slug)
        return self._engine_run(p, self._gate.phase_b(p.task), resume=True, run_state=run_state)

    def _restore_engine_inputs(self, p: _Pipeline, inputs: NodeInputs) -> None:
        """Repopulate the artifact paths a resumed fixing/review node reads: the diff, the latest
        failed check log, the review findings, and the plan — from disk + the store, scoped to the
        active subtask when decomposed."""
        # Recovery parity: a resumed node must read the SAME exchange paths a fresh run
        # produced, never the private originals. Re-publish each restored artifact into the current
        # task's exchange and point NodeInputs at the exchange copy.
        secrets = self._memory_extra_secrets()
        task_dir = task_artifact_dir(self._artifacts_root, p.task.id)
        diff = task_dir / "current.diff"
        if diff.exists():
            inputs.diff_path = publish_file(
                str(self._exchange_root),
                p.task.id,
                "current.diff",
                str(diff),
                extra_secrets=secrets,
            )
        plan = task_dir / "plan.md"
        if plan.exists():
            inputs.plan_path = publish_file(
                str(self._exchange_root), p.task.id, "plan.md", str(plan), extra_secrets=secrets
            )
        # Review findings are now per-run under stages/<node>/run-<id>/findings.json (history
        # preserved across fix→review cycles). The store's evaluations table is the source of truth
        # for which run produced the latest verdict: take the last in_flow_verdict row (skip the
        # supervisor_step/final rows, which carry no node_id/run_id) and rebuild its findings path.
        verdicts = [
            e for e in self._store.get_evaluations(p.task.id) if e.kind == "in_flow_verdict"
        ]
        if verdicts:
            last = verdicts[-1]  # get_evaluations is ORDER BY id ASC → last == most recent
            if last.node_id is not None and last.source_node_run_id is not None:
                review = (
                    node_run_dir(
                        self._artifacts_root, p.task.id, last.node_id, last.source_node_run_id
                    )
                    / "findings.json"
                )
                if review.exists():
                    inputs.review_path = publish_node_run_file(
                        str(self._exchange_root),
                        p.task.id,
                        last.node_id,
                        last.source_node_run_id,
                        "findings.json",
                        review.read_bytes(),
                        extra_secrets=secrets,
                        private_path=str(review),
                    )
        # The fixing-resume check log is task-scoped — a decomposed subtask re-runs its region
        # from the top (region entry), regenerating its own check log.
        latest_check = self._store.latest_failed_check_log(p.task.id, None)
        if latest_check and Path(latest_check).exists():
            inputs.checks_path = publish_file(
                str(self._exchange_root),
                p.task.id,
                f"checks/{Path(latest_check).name}",
                latest_check,
                extra_secrets=secrets,
            )

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
        """Resolve the task's flow by its ``task_type`` (dispatch + the config-aware gate).

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

    def _control_bundle_dir(self, task_id: str) -> Path:
        """The private per-task frozen-control-bundle dir (a provider deny target)."""
        return self._layout.runs_home / CONTROL_BUNDLE_DIRNAME / task_id

    def _freeze_live_control_bundle(
        self, p: _Pipeline, bundle_dir: Path
    ) -> tuple[FlowSnapshot, FrozenControlBundle, Path]:
        """Snapshot the live control plane into a fresh bundle and record its digest.

        Shared by fresh/restart and by an operator ``rerun --continue`` that adopts the current
        on-disk control plane. Returns the live snapshot, the bound bundle, and the live flow dir.
        """
        from wastech_orchestrator import __version__

        live = self._resolve_flow(p)
        assert live.source_path is not None
        live_flow_dir = live.source_path.parent
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle = freeze_control_bundle(
            bundle_dir,
            live,
            live_flow_dir,
            self._tool_registry,
            # ``advanced_mode`` records the posture the task STARTED in, which is the half the
            # ledger cannot cover: the bundle is written before the first node runs, so it is there
            # for a task that never reached a terminal transition. The two do not back each other up
            # — they divide the timeline. A successful run sweeps its own ``runs/`` subtree
            # (``clean_runs_on_success``, the shipped default), so this copy is gone exactly when
            # the ledger record exists, and present exactly when it does not.
            metadata={
                "orchestrator_version": __version__,
                "advanced_mode": "true" if self._advanced_mode else "false",
            },
        )
        self._store.update_task(p.task.id, control_bundle_digest=bundle.bundle_digest)
        return live, bundle, live_flow_dir

    def _prepare_control_bundle(
        self, p: _Pipeline, *, resume: bool, adopt: bool = False
    ) -> tuple[FlowSnapshot, FrozenControlBundle, Path]:
        """Freeze (fresh/restart/adopt) or load+verify (crash-recovery continue) the control plane.

        Returns the flow snapshot to execute, the bound frozen bundle, and the **live** flow dir
        (the baseline the post-node hook re-hashes to detect an in-run mutation).

        * Fresh/restart snapshots the live flow YAML, every referenced role file, the supervisor
          prompts, and the referenced tool executables into a fresh private bundle and records its
          digest in the state store.
        * ``adopt`` (an operator ``rerun --continue``) re-freezes from the live control plane the
          same way, so the resume runs the operator's between-run edits and records the new digest.
          The post-node tamper hook rebaselines to this new digest, so agent-side mutation *during*
          the resumed run is still caught — the tamper guarantee is preserved.
        * A plain crash-recovery resume reuses the original frozen bytes, verified against the
          parent-held digest, and reconstitutes the flow from the frozen YAML. An edit to this
          flow's live control inputs while the task was parked is a conflict here: it refuses (fail
          closed), because a crash could follow an *agent* mutation — only a deliberate operator
          ``--continue`` (``adopt``) adopts a live edit.

        Raises :class:`ControlBundleError` on any identity/integrity/parked-conflict failure; the
        caller routes it to ``manual_action_required``.
        """
        bundle_dir = self._control_bundle_dir(p.task.id)
        if not resume or adopt:
            live, bundle, live_flow_dir = self._freeze_live_control_bundle(p, bundle_dir)
            self._log(p.task.id).info(
                "control plane adopted on --continue" if adopt else "control plane frozen",
                extra={"bundle_digest": bundle.bundle_digest[:12]},
            )
            return live, bundle, live_flow_dir

        expected = self._store.get_control_bundle_digest(p.task.id)
        if expected is None:
            raise ControlBundleError(
                "no persisted control-bundle digest to resume against; use a fresh rerun"
            )
        bundle = load_control_bundle(bundle_dir, expected)
        frozen = load_flow(bundle.flow_source_path)
        # Parked-conflict guard: an edit to THIS flow's live control inputs while the task was
        # parked is a conflict — continue keeps the frozen bytes and refuses to ignore the edit.
        try:
            live = self._resolve_flow(p)
            assert live.source_path is not None
            live_flow_dir = live.source_path.parent
            live_digest = digest_live_control_inputs(frozen, live_flow_dir, self._tool_registry)
        except (PipelineFailed, ControlBundleError) as exc:
            raise ControlBundleError(
                f"live control plane changed since the task was frozen ({exc}); "
                "use a fresh rerun/restart"
            ) from exc
        if live_digest != bundle.bundle_digest:
            raise ControlBundleError(
                "live control plane was edited while the task was parked; "
                "use a fresh rerun/restart to adopt it"
            )
        return frozen, bundle, live_flow_dir

    def _verify_control_plane_unchanged(
        self,
        p: _Pipeline,
        snapshot: FlowSnapshot,
        live_flow_dir: Path,
        expected_digest: str,
        bundle_dir: Path | None,
        reported: set[str],
    ) -> None:
        """Compare the live control plane against the frozen digest; two findings, two verdicts.

        A **substituted** file — a planted symlink or hard-link where a control input should be,
        surfaced by the no-follow identity checks — stays a fail-closed
        ``manual_action_required``. That is not "the file changed"; it is the file replaced by a
        pointer at something else, and nothing an operator does in the ordinary course looks like
        it.

        A **content** change is a warning and the run continues. The code cannot tell an agent
        rewriting a role prompt from the operator editing their own flow YAML mid-run, and on a
        repository where the operator edits flows several times a day the second reading is the
        common one — parking would come down to whether they saved the file a minute before the
        freeze or a minute after. Nothing is lost by continuing, because that is what the freeze is
        for: the run continues on the **frozen** bundle, so the edit cannot select control bytes for
        a downstream node either way. It applies from the next run, or from a ``rerun --continue``,
        which adopts it on purpose.

        The warning names the diverged keys, because "the control plane changed" is not actionable
        and "flows/content_chapter.yaml" is. ``reported`` is the run's set of live digests already
        warned about: the check runs after every node, and one edit would otherwise print the same
        line once per remaining node in the flow.
        """
        try:
            live_digest = digest_live_control_inputs(snapshot, live_flow_dir, self._tool_registry)
        except ControlBundleError as exc:
            raise NodeManualRequired(f"control plane changed during the run: {exc}") from exc
        if live_digest == expected_digest or live_digest in reported:
            return
        reported.add(live_digest)
        self._log(p.task.id).warning(
            "the live control plane no longer matches the frozen bundle; this run continues on the "
            "frozen copy, so the edit takes effect from the next run or from `rerun --continue`",
            extra={"changed": self._diverged_control_keys(snapshot, live_flow_dir, bundle_dir)},
        )

    def _diverged_control_keys(
        self, snapshot: FlowSnapshot, live_flow_dir: Path, bundle_dir: Path | None
    ) -> str:
        """The diverged control-input keys for the warning, as one comma-separated field.

        Best-effort and message-only: the verdict above is already decided against the parent-held
        digest. This reads the frozen copies off disk to name what moved, so it cannot see a
        provider that rewrote a live file and its frozen twin together — which is exactly the case
        the parent-held digest catches, and the case this reports as ``unidentified``.
        """
        if bundle_dir is None:
            return "unidentified"
        try:
            keys = diverged_control_inputs(snapshot, live_flow_dir, self._tool_registry, bundle_dir)
        except (ControlBundleError, OSError):
            return "unidentified"
        return ", ".join(keys) if keys else "unidentified"

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

    def _build_engine_services(
        self,
        p: _Pipeline,
        *,
        finalize: Callable[[], str | None] | None,
        tool_registry: ToolRegistry | None = None,
    ) -> NodeServices:
        """Assemble the per-unit :class:`NodeServices` for an engine run.

        Shared by the task driver (:meth:`_engine_run`) and the operator merge routine
        (:meth:`_run_merge_flow`). ``finalize`` is the publish node's hook; ``None`` for a flow with
        no PR-publishing node (the merge flow's ``policy: none`` terminal never calls it).
        ``tool_registry`` is the frozen-bundle registry on the task path; ``None`` falls
        back to the shared live-``.worc/tools`` registry (the ephemeral merge flow, not frozen).
        """
        return build_node_services(
            router=self._router,
            check_runner=self._checks,
            store=self._store,
            repo_dir=self._config.repo.local_path,
            artifacts_root=str(self._artifacts_root),
            exchange_root=str(self._exchange_root),
            clock=self._clock,
            git=self._git,
            notifier=self._notifier,
            snapshot_hook=self._git,
            ask_timeout_s=self._config.telegram.ask_timeout_s,
            ask_heartbeat_seconds=self._heartbeat_seconds,
            # Claude-only max-turns gate: resolved once from the claude provider block
            # (absent in a codex-only setup → off). Preflight guarantees telegram when it is on.
            max_turns_gate=self._max_turns_gate_enabled(),
            prompt_audit=self._prompt_audit_on(p.task),
            prompt_secrets=self._prompt_secrets(),
            register_artifact=self._register_artifact,
            finalize=finalize,
            # The frozen task-packet digest the publish node's audit commit verifies the
            # staged lifecycle ``<id>.md`` against (``None`` for the not-frozen merge flow).
            task_packet_digest=self._task_packet_digest(p),
            # The dependency_scan checker launches its argv scanners through the same safe runner
            # and policy-built env the Check Runner uses (a test's fake runner drives both).
            run_process=self._checks.run_process,
            process_env=build_child_env(self._config.security),
            scan_timeout_s=self._config.checks.timeout_seconds,
            # Per-task override wins outright; otherwise the global default (config_writer ships
            # "auto"). protected_paths is global-only (no per-task override).
            trust_level=(p.task.trust_level or self._config.security.trust_level),
            protected_paths=self._config.security.protected_paths,
            # Operator-only, like protected_paths: a task cannot turn the git-evidence grant on, and
            # neither can a flow — a node's declaration is honored only while this is true.
            allow_git_evidence=self._config.security.allow_git_evidence,
            # Defense-in-depth: the Core-owned advisory security contract, resolved once from
            # config; the neutral seam prepends it to every agent/evaluator prompt.
            security_preamble=self._security_preamble(),
            packet_builder=self._packet_builder(),
            # Custom tool nodes: the per-task frozen registry when given, else the
            # shared live one; plus the flow-wide default timeout.
            tool_registry=tool_registry if tool_registry is not None else self._tool_registry,
            tools_default_timeout_seconds=self._config.tools.default_timeout_seconds,
        )

    def _instruction_bundle_dir(self, task_id: str) -> Path:
        """The private per-task frozen-instruction-bundle dir (a provider deny target)."""
        return instruction_bundle_dir(self._layout.private_home, task_id)

    def _task_packet_digest(self, p: _Pipeline) -> str | None:
        """The frozen task-packet sha256 the audit commit verifies its lifecycle file
        against; ``None`` before the packet is frozen or when the run has none."""
        return next(
            (digest for key, digest in p.instruction_entries if key == TASK_PACKET_KEY), None
        )

    def _freeze_task_and_repo_instructions(
        self, p: _Pipeline, inputs: NodeInputs, *, resume: bool
    ) -> None:
        """Freeze the task packet + root repository instructions into the bundle.

        The source task file and ``AGENTS.md``/``CLAUDE.md`` stay ordinary repository content; the
        provider only ever reads the redacted exchange copies published here from the immutable
        frozen canonical (never live) — immutability comes from the canonical digest, not from the
        exchange copy (which is the redacted projection).

        Fresh/restart re-freezes from live and gates each required input against a known secret
        (fail-closed). Continue verifies the persisted composite digest first, then
        republishes the verified frozen copies to the restored exchange.
        """
        bundle_dir = self._instruction_bundle_dir(p.task.id)
        if resume:
            expected = self._store.get_instruction_manifest_digest(p.task.id)
            if expected is None:
                raise InstructionBundleError(
                    "no persisted instruction-manifest digest to resume against; rerun fresh"
                )
            loaded = load_instruction_bundle(bundle_dir, expected)  # fail-closed verify
            # Repopulate the frozen (key, digest) entries from the verified manifest so
            # ``_task_packet_digest`` is not ``None`` on resume — otherwise the audit commit would
            # silently skip ``_assert_lifecycle_matches_packet`` and a task file rewritten while the
            # task was parked could be committed unchecked (the fresh path always records the digest
            # via ``freeze_task_packet``).
            p.instruction_entries.extend(loaded.entries)
            self._publish_frozen_task_packet(p, inputs, bundle_dir)
            return
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        secrets = self._memory_extra_secrets()
        # Freeze the task packet only when there is a real source file. A flow with no task packet
        # (merge flow) has ``task_path is None``; a restart with no source file on disk leaves it
        # absent — both skip gracefully, so the frozen
        # bundle simply carries no task entry rather than failing on a phantom file. A real task
        # always has its lifecycle file, so its packet is always frozen.
        if inputs.task_path and Path(inputs.task_path).is_file():
            canonical, entry = freeze_task_packet(bundle_dir, Path(inputs.task_path))
            assert_no_required_secret(
                canonical.read_text(encoding="utf-8", errors="replace"),
                extra_secrets=secrets,
                label="task packet",
            )
            p.instruction_entries.append(entry)
        repo_root = Path(self._config.repo.local_path)
        tracked = frozenset(self._git.list_tracked_files(*REPO_INSTRUCTION_NAMES))
        files = discover_repository_instructions(repo_root, tracked)
        # The per-source files are still frozen + hashed into the manifest digest (audit /
        # reproducibility), but no payload is built or injected — the agent reads the live
        # (write-denied, immutable) root files itself. No exchange projection remains, so there is
        # no repository-instruction secret gate (the agent could read the live file regardless).
        p.instruction_entries.extend(freeze_repository_instructions(bundle_dir, files))
        self._publish_frozen_task_packet(p, inputs, bundle_dir)

    def _publish_frozen_task_packet(
        self, p: _Pipeline, inputs: NodeInputs, bundle_dir: Path
    ) -> None:
        """Re-point ``task_path`` at the redacted exchange copy of the frozen task packet.

        Published from the frozen canonical file (verified on resume), so the exchange copy is a
        redaction of the immutable snapshot — never a re-read of the live file. Repository
        instructions are neither published nor injected: the agent reads the repo's root instruction
        files itself, and those files are write-denied for the run (immutable).
        """
        secrets = self._memory_extra_secrets()
        if (bundle_dir / TASK_PACKET_KEY).is_file():
            inputs.task_path = publish_file(
                str(self._exchange_root),
                p.task.id,
                "task.md",
                str(bundle_dir / TASK_PACKET_KEY),
                extra_secrets=secrets,
            )

    def _finalize_instruction_bundle(
        self, p: _Pipeline, *, control_digest: str, resume: bool, adopt: bool = False
    ) -> None:
        """Write the composite manifest + persist the ``instruction_manifest_digest``.

        Fresh/restart folds the task/instruction entries and the control digest into one composite
        digest and persists it (the parent-held identity a later continue verifies).
        A plain continue is a no-op: the digest was already verified in stage 1 against the
        persisted value. An operator ``--continue`` that adopted a re-frozen control plane
        (``adopt``) re-writes the manifest so its embedded control digest re-binds to the NEW one,
        keeping the composite identity consistent; the already-frozen task/repo entries (restored
        into ``p.instruction_entries`` on resume) are preserved, not re-frozen from live.
        """
        if resume and not adopt:
            return
        from wastech_orchestrator import __version__

        bundle_dir = self._instruction_bundle_dir(p.task.id)
        digest = write_instruction_manifest(
            bundle_dir,
            entries=p.instruction_entries,
            control_digest=control_digest,
            metadata={"orchestrator_version": __version__},
        )
        self._store.update_task(p.task.id, instruction_manifest_digest=digest)
        label = "agent inputs re-bound (adopt)" if adopt else "agent inputs frozen"
        self._log(p.task.id).info(label, extra={"instruction_digest": digest[:12]})

    def _resolve_merge_flow(self) -> FlowSnapshot:
        """Resolve the configured ``git.merge_flow`` to a validated snapshot (operator-editable).

        Raises :class:`PipelineFailed` (→ a clear operator error) when the flow file is missing or
        fails graph/config validation, exactly as task-flow resolution does. The seam is a single
        flow name today; a future path/area-based collection resolves a name here the same way.
        """
        try:
            return self._flow_registry.resolve(self._config.git.merge_flow)
        except (FlowResolutionError, FlowValidationError) as exc:
            raise PipelineFailed(
                f"merge flow {self._config.git.merge_flow!r} could not be resolved: {exc}"
            ) from exc

    def _run_merge_flow(self, p: _Pipeline, snapshot: FlowSnapshot) -> bool:
        """Run the merge flow on the already-merged, conflict-marked working tree; True iff it ends
        clean and green.

        Transactional + ephemeral: a fresh ``FlowRunState`` and a no-op recorder (no checkpoint
        written to the task row, so no clash with ``rerun --continue``), no supervisor, no post-node
        hook, no publish. The flow only resolves markers and runs the operator's checks; the
        orchestrator commits the merge and merges the PR afterward. Returns ``False`` when a bounded
        loop is exhausted (markers/checks unresolved) so the caller aborts the merge."""
        assert snapshot.source_path is not None
        # Deliberate: the merge flow is ephemeral and NOT frozen; it publishes no
        # task packet and injects no repository instructions. The conflict agent reads the
        # repo's live root `AGENTS.md`/`CLAUDE.md` itself (Codex via native discovery, Claude via
        # its Read tool) — fine for a mechanical marker resolution + checks pass. A future merge
        # agent needing richer repository conventions would wire them here; see follow_ups.
        inputs = build_node_inputs(
            p,
            flow_dir=snapshot.source_path.parent,
            check_sets=self._check_sets(p),  # the operator's command_sets; () = no gate
            commit_message=f"merge({p.task.id}): resolve base-merge conflicts",
            summary_body_path=self._fallback_summary_path(p),
        )
        services = self._build_engine_services(p, finalize=None)
        run_state = FlowRunState(flow_fingerprint=snapshot.flow_fingerprint)
        result = drive_flow(
            snapshot=snapshot,
            run_state=run_state,
            recorder=_EphemeralRunRecorder(),
            services=services,
            inputs=inputs,
            facts=lambda _fact: False,  # merge.yaml has no `when` predicates
            agents=self._config.agents,
            task_id=p.task.id,
            is_cancelled=self._is_cancelled,
        )
        return result.status is Status.DONE

    def _announce_environment_patterns(self, p: _Pipeline) -> None:
        """Announce what each ``allowed_environment`` prefix pattern resolved to — once, up front.

        Once per fresh/resumed engine entry, before that entry launches git or a provider. The
        allowlist feeds every child under strict isolation, but only orchestrator git/gh in advanced
        mode; the scope is stated explicitly so a secret-name drop is never misread as an agent-side
        guarantee. ``worc preflight`` prints the same expansion in its report.

        A dropped name is a warning — the operator wrote a pattern that reaches a credential and the
        filter refused it — while a clean expansion is informational. A config with no pattern says
        nothing at all.
        """
        _, expansions = expand_allowed_environment(self._config.security.allowed_environment)
        described = describe_expansions(expansions)
        if not described:
            return
        log = self._log(p.task.id)
        scope = (
            "applies to orchestrator git/gh and strict-mode agent children; agent children also "
            "withhold env-file names matched only by a prefix pattern"
            if self._config.security.strict_isolation
            else "gates orchestrator git/gh only; advanced-mode agent/check/tool children receive "
            "the parent environment whole except variables loaded from the env-file"
        )
        message = (
            "allowed_environment prefix patterns resolved (" + scope + "): " + "; ".join(described)
        )
        extra = {"patterns": [item.pattern for item in expansions]}
        if any(item.dropped for item in expansions):
            log.warning(message, extra=extra)
        else:
            log.info(message, extra=extra)

    def _announce_security_posture(self, p: _Pipeline) -> None:
        """Record the effective environment/isolation posture for a fresh or resumed entry."""
        self._announce_environment_patterns(p)
        if self._config.security.read_isolation_off:
            # One line, and the structured fields beside it. What read-isolation off does and does
            # not open is `guide/config/security.md`'s job; repeating it into every run log is what
            # made the posture block longer than the run it introduces.
            self._log(p.task.id).warning(
                "read-isolation OFF (operator-sanctioned) — see guide/config/security.md",
                extra={
                    "disable_read_isolation": self._config.security.disable_read_isolation,
                    "strict_isolation": self._config.security.strict_isolation,
                },
            )
        if self._config.security.allow_git_evidence:
            inert = "" if self._config.security.strict_isolation else " (inert in advanced mode)"
            self._log(p.task.id).info(f"git-evidence ON{inert}")
        for mode_line in describe_advanced_mode(self._config):
            self._log(p.task.id).warning(mode_line)
        for floor_gap in describe_host_floor(self._config, self._host_floor_checks):
            self._log(p.task.id).warning(f"isolation floor NONE — {floor_gap}")

    def _drive_via_engine(self, p: _Pipeline, completeness: Completeness) -> PipelineResult:
        """Drive the task through the :class:`FlowEngine`.

        Keeps the orchestrator-owned preamble (isolation + check preflight, branch prep) and the
        terminal handling (auto-merge + cleanup); the refinement→…→publish body is expressed as the
        validated flow graph and executed by the engine. Per-node post-processing (artifact slots)
        runs in the post-node hook; the publish node finalizes the task file + opens the PR.
        Infra failure → ``failed``; a node needing human action → ``manual_action_required``.
        """
        self._resolve_flow(p)  # fail closed on an unknown/invalid flow before any side-effect
        if p.operator_decomposition is not None:
            # Operator-authored split: materialize the manifest-built decision now, before any
            # branch, so it is in place whether or not the planning ``proposed_by`` node runs (a
            # disabled planning node never fires the post-hook). Validated already at preflight.
            self._persist_decomposition(p, p.operator_decomposition, gate_on=True)
        self._announce_security_posture(p)
        # Ungated on `strict_isolation`, and it has to be: since the host half moved out to
        # `describe_host_floor`, what is left is "is this provider configuration legal?" — a pure,
        # host-independent verdict about extra_args and permission profiles that no configuration
        # value earns an exemption from. Skipping it under `strict_isolation: false` skipped it in
        # exactly the mode where the generated profile carries the whole local floor, and it also
        # disagreed with `worc preflight`, which has always run this check unconditionally: the same
        # config file reported `isolation: FAIL` there and started anyway here.
        reasons = check_isolation(self._config, self._isolation_checks)
        if reasons:
            joined = "; ".join(reasons)
            self._log(p.task.id).warning("isolation preflight failed", extra={"reasons": joined})
            raise PipelineFailed(f"isolation: {joined}")
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
        # Pre-launch invariant (fresh + resume): the exchange root may hold at most this task's
        # directory. Leftover entries from a run that died before its seal are cleared and named —
        # the directory is private and rebuilt from durable facts, so refusing to start over it
        # stopped a task for a state one delete fixes. A root or task entry *substituted* by a
        # symlink is a different finding and still fails closed: route it to a clean
        # ``manual_action_required`` (naming the offending entry) instead of letting the bare
        # ``ExchangeError`` escape uncaught and crash-loop the daemon (the border already holds — no
        # provider has launched). Mirrors the adjacent ``ExchangeSealError`` handling below.
        try:
            cleared = clear_foreign_exchange_entries(self._exchange_root, p.task.id)
        except (ExchangeError, OSError) as exc:
            return self._go_terminal(
                p, Status.MANUAL_ACTION_REQUIRED, manual_reason=f"exchange not clean: {exc}"
            )
        if cleared:
            self._log(p.task.id).warning(
                "cleared leftover exchange entries from an earlier run",
                extra={"removed": ", ".join(cleared)},
            )
        # Operator ``rerun --continue`` (``continue_task``) sets this marker; automatic
        # crash-recovery never does. It is what separates "a person read the diagnosis and decided"
        # from "the daemon came back up", and both the exchange guard below and the control-plane
        # freeze further down key their trust on it.
        adopt = resume and p.task.id in self._continue_adopt
        # On a continue, establish the verified current-task exchange before any node runs:
        # restore + verify the latest sealed snapshot (terminal continue), verify-and-reuse the
        # still-active exchange (parked/crashed continue), or refuse an unsafe task. A fresh run
        # starts from the clean exchange the rerun/restart path already cleared.
        if resume:
            try:
                contaminated, active_unsafe = self._store.get_exchange_guard(p.task.id)
                if contaminated and adopt:
                    # An explicit operator `--continue` clears the contamination flag, on the same
                    # trust the control plane is adopted on. Detection still parks the run and still
                    # quarantines the tree as evidence — that is the agent-side guard — but without
                    # this an operator who has read the diagnosis could not continue at all, only
                    # re-pay for a fresh run. The daemon's own crash recovery sets no such marker
                    # and still refuses.
                    self._store.update_task(p.task.id, exchange_contaminated=0)
                    contaminated = False
                    self._log(p.task.id).warning(
                        "continuing over an exchange flagged contaminated by mutation detection — "
                        "the operator asked for this resume; the quarantined evidence is kept"
                    )
                ensure_current_exchange(
                    self._exchange_root,
                    self._artifacts_root,
                    p.task.id,
                    contaminated=contaminated,
                    active_unsafe=active_unsafe,
                )
            except ExchangeSealError as exc:
                return self._go_terminal(
                    p, Status.MANUAL_ACTION_REQUIRED, manual_reason=f"exchange restore: {exc}"
                )
        # Freeze (fresh/restart) or load+verify (continue) the control plane before any
        # node runs, then bind every flow/supervisor/tool consumer to the frozen bundle instead of
        # live ``.worc``. A freeze/verify/parked-conflict failure is a fail-closed manual condition.
        # Operator ``rerun --continue`` adopts the live control plane; automatic crash-recovery
        # never sets the marker, so it keeps the fail-closed parked-conflict refuse.
        try:
            snapshot, bundle, live_flow_dir = self._prepare_control_bundle(
                p, resume=resume, adopt=adopt
            )
        except ControlBundleError as exc:
            return self._go_terminal(
                p, Status.MANUAL_ACTION_REQUIRED, manual_reason=f"control plane: {exc}"
            )
        assert snapshot.source_path is not None
        node_overrides = self._resolve_node_overrides(p, snapshot)
        if run_state is None:
            run_state = FlowRunState(flow_fingerprint=snapshot.flow_fingerprint)
        elif snapshot.flow_fingerprint != run_state.flow_fingerprint:
            # The frozen graph must match the checkpoint it is resumed against (defense in depth on
            # top of the load-time digest verify) — otherwise the bundle and checkpoint are from
            # different freezes.
            return self._go_terminal(
                p,
                Status.MANUAL_ACTION_REQUIRED,
                manual_reason="control plane: frozen flow does not match checkpoint; rerun fresh",
            )
        p.flow_name = snapshot.doc.name
        # The supervisor layer starts at task start and lives the whole cycle; it carries this
        # task's own resume_own_lineage session. It reads the frozen prompts. Switched off, it is
        # simply not built: all four consumers already tolerate its absence, so "do not construct
        # the object" is the whole mechanism. The assignment stays unconditional (not an `if` that
        # skips
        # it) so no layer from a previous task in a `watch` loop can survive into this one.
        self._supervisor = (
            self._build_supervisor(p, snapshot, flow_dir=bundle.flow_dir)
            if self._config.supervisor.enabled
            else None
        )
        inputs = build_node_inputs(
            p,
            flow_dir=bundle.flow_dir,
            check_sets=self._check_sets(p),  # normalized command_sets; () = no gate
            pull_request_title=p.task.title,
            commit_message=task_commit_subject(p.task.id, p.task.title, p.task.commit_type),
            summary_body_path=self._fallback_summary_path(p),
            branch_mode=self._branch_mode(p.task),
            publish_scope=p.task.publish,
        )
        # Freeze the agent inputs into a private, immutable bundle and expose only redacted
        # exchange copies. Fresh/restart freezes + records the composite
        # ``instruction_manifest_digest``; continue loads+verifies it and refuses to resume a
        # session whose digest differs. A freeze/verify/secret-gate failure is a fail-closed
        # manual condition (Core-detected, never fallback).
        try:
            self._freeze_task_and_repo_instructions(p, inputs, resume=resume)
            if resume:
                self._restore_engine_inputs(p, inputs)  # diff/checks/review/plan paths from disk
            self._finalize_instruction_bundle(
                p, control_digest=bundle.bundle_digest, resume=resume, adopt=adopt
            )
        except InstructionBundleError as exc:
            return self._go_terminal(
                p, Status.MANUAL_ACTION_REQUIRED, manual_reason=f"agent inputs: {exc}"
            )
        # Tool nodes launch the FROZEN executables: a per-task registry rooted at the
        # bundle, not the shared live ``.worc/tools`` one.
        services = self._build_engine_services(
            p,
            finalize=lambda: self._engine_finalize(p, inputs),
            tool_registry=ToolRegistry(bundle.tools_dir),
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
                live_flow_dir=live_flow_dir,
                control_digest=bundle.bundle_digest,
                control_bundle_dir=bundle.root,
            )
        except NodeManualRequired as exc:
            self._sync_counters_from_run_state(p, run_state)
            # A detected agent-side exchange mutation flags the tree contaminated
            # so the terminal seam quarantines it as evidence instead of sealing it, and continue is
            # refused. The flag is persisted (survives a restart between detection and teardown).
            mutation = exc if isinstance(exc, ExchangeMutationManual) else None
            if mutation is not None:
                self._store.update_task(p.task.id, exchange_contaminated=1)
            return self._go_terminal(
                p, Status.MANUAL_ACTION_REQUIRED, manual_reason=str(exc), mutation=mutation
            )
        except FlowCancelled as exc:
            self._sync_counters_from_run_state(p, run_state)
            return self._park(
                p,
                run_state,
                NodeInfraError(str(exc), error_class=ErrorClass.CANCELLED),
            )
        except EvaluatorInfraError as exc:
            self._sync_counters_from_run_state(p, run_state)
            # An evaluator that could not *run* (infra/misconfig) must not discard an already-green
            # diff: its terminal preserves the branch for an operator to review/publish rather than
            # failing the task.
            return self._dispatch_infra_exhaustion(
                p,
                exc,
                run_state,
                terminal_status=Status.MANUAL_ACTION_REQUIRED,
                terminal_reason=self._evaluator_degrade_reason(p, exc),
            )
        except NodeInfraError as exc:
            self._sync_counters_from_run_state(p, run_state)
            # An agent node's exhaustion leaves no usable result to ship, so its terminal is failed.
            return self._dispatch_infra_exhaustion(
                p,
                exc,
                run_state,
                terminal_status=Status.FAILED,
                terminal_reason=str(exc),
            )
        self._sync_counters_from_run_state(p, run_state)
        return self._finish_engine_run(p, result)

    def _dispatch_infra_exhaustion(
        self,
        p: _Pipeline,
        exc: NodeInfraError,
        run_state: FlowRunState,
        *,
        terminal_status: Status,
        terminal_reason: str,
    ) -> PipelineResult:
        """Route a node whose provider stage was exhausted, by the aggregate class disposition.

        Shared by every node kind so the containment-before-park precedence is applied exactly once
        and cannot diverge between them: an evaluator's green diff does not make an unproven process
        tree or a missing isolation capability shippable, and a security condition on any attempt
        outranks a resumable one on another. The caller supplies only what its own terminal means,
        because that — not the precedence — is what differs by node kind.
        """
        disposition = classify_exhaustion(exc.error_classes, representative=exc.error_class)
        if disposition is InfraDisposition.MANUAL:
            return self._terminal_infra_manual(p, exc, run_state)
        if disposition is InfraDisposition.PARK:
            # Resumable, not discarded: every allowed provider hit a transient limit or outage
            # (retries and fallback done), or an operator stop cancelled the agent. A subscription
            # limit resets on its own window, so the task waits it out. The checkpoint is already
            # persisted; a later watch tick / process start resumes from current_node, or fails it
            # once total parked time passes agents.retry.max_blocked_s.
            return self._park(p, run_state, exc)
        return self._fail(
            p,
            terminal_reason,
            status=terminal_status,
            node_id=run_state.current_node,
            run_state=run_state,
        )

    def _evaluator_degrade_reason(self, p: _Pipeline, exc: EvaluatorInfraError) -> str:
        """The terminal reason for an evaluator that could not run, preserving its green diff.

        ``str(exc)`` already carries the real cause; when the branch has no diff, say so plainly so
        the manual terminal never implies a change to review that does not exist.
        """
        reason = str(exc)
        # EXPERIMENTAL(no-work-infra): empty-diff annotation on the degrade-to-manual reason.
        if not read_final_diff(self._artifacts_root, p.task.id).strip():
            reason = f"{reason} (no changes were produced to review)"
        return reason

    def _terminal_infra_manual(
        self, p: _Pipeline, exc: NodeInfraError, run_state: FlowRunState
    ) -> PipelineResult:
        """Route a containment/capability infra error to a fail-closed manual terminal.

        Shared by the evaluator and non-evaluator infra handlers so the security dispatch is
        identical regardless of node kind. ``CONTAINMENT_UNVERIFIED`` flags the active exchange
        unsafe FIRST: the provider tree is not proven quiescent, so an unknown descendant
        may still be writing — the terminal seam must not seal it, and the terminal Git/cleanup is
        withheld (``_fail`` / ``_go_terminal`` honor the flag), holding the tree until an operator
        resolves. ``CAPABILITY_UNAVAILABLE`` has no live writer, so only the manual
        terminal applies.

        The unsafe flag is decided from *every* class the stage raised, never the settled one: an
        operator stop landing on the same attempt replaces the settled class with a cancel while the
        unproven tree stays unproven, and missing the flag there would let the terminal seam seal a
        tree an unknown descendant may still be writing.
        """
        if ErrorClass.CONTAINMENT_UNVERIFIED in exc.error_classes:
            self._store.update_task(p.task.id, exchange_active_unsafe=1)
        return self._fail(
            p,
            str(exc),
            status=Status.MANUAL_ACTION_REQUIRED,
            node_id=run_state.current_node,
            run_state=run_state,
        )

    def _park(self, p: _Pipeline, run_state: FlowRunState, exc: NodeInfraError) -> PipelineResult:
        """Soft, resumable pause on transient infra exhaustion or an operator stop (B-lite). NOT a
        terminal transition.

        The task stays ``RUNNING`` (active) so :meth:`resume` picks it up via the reconciler next
        tick / next start; the flow checkpoint is already saved (``current_node``). Records the
        first park instant in ``tasks.blocked_since`` (kept across re-parks so the ceiling measures
        total parked wall-clock); the ceiling is checked on resume in :meth:`_resume_via_engine`. No
        commit/push and no failure report — the partial work is preserved by the checkpoint.

        ``tasks.blocked_until`` records when a provider said its own window reopens, so the wait is
        precise instead of one blind re-entry per poll interval. Unlike ``blocked_since`` it is
        rewritten on EVERY park: a later exhaustion reporting no instant must not inherit an earlier
        provider's window and go on deferring a task that could already run."""
        existing = self._store.get_task(p.task.id)
        prior = existing.blocked_since if existing is not None else None
        parked_since = prior if prior is not None else self._clock()
        blocked_until = self._park_wake_instant(exc, parked_since=parked_since)
        self._store.update_task(p.task.id, blocked_since=parked_since, blocked_until=blocked_until)
        log = bind(_LOG, task_id=p.task.id)
        log.info(
            "task parked (resumable)",  # transient-infra exhaustion or an operator-stop cancel
            extra={
                "node_id": run_state.current_node,
                "error_class": exc.error_class.value if exc.error_class else None,
                "blocked_until": blocked_until,
            },
        )
        return PipelineResult(task_id=p.task.id, final_status=Status.RUNNING)

    def _park_wake_instant(self, exc: NodeInfraError, *, parked_since: str) -> str | None:
        """The earliest instant a parked task may attempt a provider again, else ``None`` for any
        tick.

        The provider's reported reset is untrusted input. It is ignored — leaving the blind
        next-tick behavior, never worse — when it is absent, unparseable, or not actually in the
        future; and it is clamped to the park ceiling so a provider claiming a window next week can
        never outlive the ceiling that would fail the task anyway. No new configuration key: the
        clamp is the existing ceiling.

        An operator stop is excluded outright: a cancelled run resumes when the operator says so,
        not when some provider's window reopens. Every comparison goes through the injected
        clock, and a clock that yields no comparable instant is treated as no instant rather than
        breaking a park — note that a naive clock compared against a provider's timezone-aware
        instant raises ``TypeError``, not ``ValueError``.
        """
        if exc.error_class is ErrorClass.CANCELLED or exc.resets_at is None:
            return None
        try:
            wake = datetime.fromisoformat(exc.resets_at)
            if wake <= datetime.fromisoformat(self._clock()):
                return None
            ceiling = datetime.fromisoformat(parked_since) + timedelta(
                seconds=self._config.agents.retry.max_blocked_s
            )
            return min(wake, ceiling).isoformat()
        except (ValueError, TypeError):
            return None

    def _still_blocked(self, blocked_until: str | None) -> bool:
        """Whether a parked task's provider-reported window has not reopened yet.

        Fails **open** on anything unparseable or incomparable: a junk instant must cost one wasted
        re-entry, never wedge a task that nothing else would release.
        """
        if blocked_until is None:
            return False
        try:
            return datetime.fromisoformat(blocked_until) > datetime.fromisoformat(self._clock())
        except (ValueError, TypeError):
            return False

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
        reading 0 after the engine ran fix loops.
        """
        p.counters = LoopCounters.from_run_state(run_state)

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
        live_flow_dir: Path | None = None,
        control_digest: str | None = None,
        control_bundle_dir: Path | None = None,
    ) -> FlowRunResult:
        """Drive the flow in phases. Fresh: a flow with no decomposition runs in one pass; a
        decomposed one runs pre (entry…proposed_by) once, the sub_flow region once per subtask
        (commit between), then post once. Resume: continue from the hydrated ``current_node`` — a
        run still in ``pre`` re-runs pre (planning re-decides), a single-unit run continues from
        ``current_node``, and a decomposed run re-enters the active uncommitted subtask at the
        region entry (committed subtasks are skipped, never re-committed)."""
        post_node = self._engine_post_node(
            p,
            inputs,
            snapshot,
            live_flow_dir=live_flow_dir,
            control_digest=control_digest,
            control_bundle_dir=control_bundle_dir,
        )
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
                is_cancelled=self._is_cancelled,
                post_node=post_node,
                # EXPERIMENTAL(no-work-infra): feeds the engine's no-effective-work stall guard — an
                # opaque fingerprint of the working tree read from the last-written ``current.diff``
                # (refreshed by every agent edit-node). The engine only compares it for equality
                # across rework charges — stays domain-free. Drop this kwarg to disable the guard.
                diff_fingerprint=lambda: read_final_diff(self._artifacts_root, p.task.id),
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
        return self._fan_out_subtasks(p, run_state, regions, phase, inputs, recorder)

    def _fan_out_subtasks(
        self,
        p: _Pipeline,
        run_state: FlowRunState,
        regions: DecompositionRegions,
        phase: Callable[..., FlowRunResult],
        inputs: NodeInputs,
        recorder: StateStoreRunRecorder,
    ) -> FlowRunResult:
        """Run the sub_flow region once per subtask (commit each, reset per-subtask counters), then
        the post-region phase. A subtask with a verified commit is never re-run (recovery).

        Before each subtask's region runs, the active immutable spec is injected as
        ``inputs.subtask_spec_path`` so ``{subtask_spec_path}`` (plus ``{subtask_order}`` /
        ``{subtask_count}``) scopes the region's nodes to that one subtask. **Every** node kind in
        the region reads it, not only the ones that edit: an evaluator in the region judges one
        subtask's diff, and while it saw only the root task file and the shared plan it charged the
        task's unfinished parts against whichever subtask was under review. The post-region phase is
        whole-task again, so the spec path is cleared first."""
        units = list(p.decomposition.subtasks)
        # Decomposition is decided during planning (after `inputs` was built), so the count was None
        # at build time; surface it now for the edit nodes' "subtask N of M" context.
        inputs.subtask_count = p.decomposition.n
        committed = {s.order for s in self._store.get_subtasks(p.task.id) if s.commit_sha}
        for index, unit in enumerate(units):
            if unit.order in committed:
                continue
            # Private spec stays the audit/immutable record; the redacted exchange copy is the
            # {subtask_spec_path} the region's nodes read.
            private_spec = subtask_spec_path(self._artifacts_root, p.task.id, unit.order, unit.slug)
            inputs.subtask_spec_path = publish_file(
                str(self._exchange_root),
                p.task.id,
                f"subtasks/{private_spec.name}",
                str(private_spec),
                extra_secrets=self._memory_extra_secrets(),
            )
            # Two-layer handoff brief over every subtask already committed on this branch
            # (``None`` for the first one, which has no predecessors).
            inputs.predecessor_context_path = self._assemble_predecessor_context(p, unit)
            sub = phase(regions.region_entry, regions.region, subtask=unit.order)
            if sub.status is not Status.DONE:
                return sub
            if index != len(units) - 1:
                # A region exits by a forward edge LEAVING it, so the engine's last checkpoint
                # names the post-region node — for a subtask that is not what runs next, and the
                # gap is not instantaneous: the commit below, then the spec publish and the
                # supervisor's handoff turn, run before the next region phase re-seeds it. That
                # window reported ``node=documentation`` beside ``subtask=3/5``, telling an
                # operator a five-subtask task had reached its last stage while it was starting
                # its third. Point it at what actually runs next. Resume is unaffected: the
                # fan-out re-enters from the committed subtask rows and never reads this value
                # (only the ``pre`` region's exit checkpoint is load-bearing there) — and the last
                # subtask deliberately keeps the engine's value, because for it the post-region
                # node IS next.
                run_state.current_node = regions.region_entry
                recorder.save_checkpoint(run_state)
            self._commit_subtask(p, unit)
            if index != len(units) - 1:
                run_state.reset_consecutive_fix_budget()  # fresh per-loop budgets; global accrues
                self._store.update_task(p.task.id, active_subtask=unit.order + 1)
        inputs.subtask_spec_path = None  # post-region phase is whole-task, not subtask-scoped
        inputs.predecessor_context_path = None
        return phase(regions.post_entry, None)

    def _assemble_predecessor_context(self, p: _Pipeline, unit: SubtaskSpec) -> str | None:
        """Assemble the subtask handoff brief for *unit* and return its path (or ``None``).

        Two layers: a **deterministic factual floor** (always, zero
        LLM) — each predecessor's changed files, commit, acceptance criteria, and spec pointer,
        from artifacts that already exist — plus an **interpretive supervisor brief**
        when the supervisor is available (it resumes its warm session; no new turn budget). The
        combined content is redaction-scrubbed and written to ``logs/<task-id>/subtasks/
        NN-slug.handoff.md`` (local, uncommitted, never in the memory tiers). Best-effort: the
        first subtask (nothing committed yet) gets ``None``; a failed/empty brief still ships the
        floor.

        The floor is built from **what landed on the branch** — every subtask before this one
        carrying a commit, oldest first — and not from ``depends_on``. Subtasks run sequentially on
        one branch, so an earlier commit is a predecessor in fact whether or not it was declared:
        reading ``depends_on`` instead hid two committed subtasks from a successor that declared
        only one, and gave a subtask with no declared dependency no brief at all next to three
        committed siblings. ``depends_on`` stays the author's emphasis signal — the predecessors it
        names are marked as declared — never the source of the facts.
        """
        specs = {s.order: s for s in p.decomposition.subtasks}
        landed = sorted(
            (
                row
                for row in self._store.get_subtasks(p.task.id)
                if row.commit_sha and row.order < unit.order
            ),
            key=lambda row: row.order,
        )
        floors: list[str] = []
        for row in landed:
            spec = specs.get(row.order)
            if spec is None or row.commit_sha is None:
                continue  # not part of this run's accepted decomposition (should not happen)
            spec_path = subtask_spec_path(
                self._artifacts_root, p.task.id, row.order, spec.slug
            ).as_posix()
            files = self._git.files_in_commit(row.commit_sha) if self._git is not None else []
            floors.append(
                _format_predecessor_floor(
                    spec,
                    row.commit_sha,
                    files,
                    spec_path,
                    declared=row.order in unit.depends_on,
                )
            )
        if not floors:
            return None
        floor = "\n\n".join(floors)
        brief = ""
        if self._supervisor is not None:
            brief_text = self._supervisor.handoff(
                task_id=p.task.id, subtask_order=unit.order, floor_context=floor
            )
            if brief_text:
                brief = "\n\n" + brief_text
        header = f"# Predecessor context for subtask {unit.order:02d}: {unit.title}\n\n"
        content = redact_text(header + floor + brief, extra_secrets=self._memory_extra_secrets())
        path = subtask_handoff_path(self._artifacts_root, p.task.id, unit.order, unit.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._register_artifact(p.task.id, "subtask_handoff", str(path))
        # Private brief stays the audit record; the redacted exchange copy is {predecessor_context}.
        return publish_artifact(
            str(self._exchange_root),
            p.task.id,
            f"subtasks/{path.name}",
            content,
            extra_secrets=self._memory_extra_secrets(),
            private_path=path.as_posix(),
        )

    def _commit_subtask(self, p: _Pipeline, unit: SubtaskSpec) -> None:
        """Commit one completed subtask + persist its SHA."""
        message = task_commit_subject(
            p.task.id, f"subtask {unit.order:02d} {unit.title}", p.task.commit_type
        )
        sha = self._git.commit_subtask(p.task.id, unit.order, unit.slug, message)
        update_subtask_index(
            self._artifacts_root, p.task.id, unit.order, status="committed", commit_sha=sha
        )
        self._store.set_subtask_commit(p.task.id, unit.order, sha, "committed")
        self._store.update_task(p.task.id, subtasks_completed=unit.order)

    def _build_supervisor(
        self, p: _Pipeline, snapshot: FlowSnapshot, *, flow_dir: Path
    ) -> Supervisor:
        """Construct the per-task supervisor layer from ``config.yaml: supervisor``.

        It runs read-only on the global primary; ``role_file`` is resolved inside ``flow_dir`` (same
        containment as a node ``role_file``). ``flow_dir`` is the frozen control bundle's
        flow dir, so supervisor prompts are read from the frozen bytes, not live ``.worc``. Built
        fresh per task so its own session does not leak across tasks.
        """
        assert snapshot.source_path is not None
        return Supervisor(
            settings=self._config.supervisor,
            router=self._router,
            store=self._store,
            repo_dir=self._config.repo.local_path,
            # The layer's own provider turn is bracketed like a graph node's: it is read-only by
            # mandate, but on Codex — and on every provider in the advanced mode — it still gets a
            # shell, so it carries the write guard and its Git-control drift is reported.
            git=self._git,
            artifacts_root=str(self._artifacts_root),
            exchange_root=str(self._exchange_root),
            flow_dir=flow_dir,
            # Flow-local supervisor prompts + the follow-ups opt-in;
            # ``None`` when the flow declares no ``supervisor:`` block (global config + built-ins).
            flow_supervisor=snapshot.doc.supervisor,
            # Header facts for the finalize packet — the shape of work being closed out.
            flow_name=snapshot.doc.name,
            task_type=p.task.task_type,
            register_artifact=self._register_artifact,
            # Same per-task gate/secrets the engine's own NodeServices uses
            # (_build_engine_services), so a supervisor turn's audit artifacts honor the same
            # prompt_audit opt-in and redaction.
            prompt_audit=self._prompt_audit_on(p.task),
            prompt_secrets=self._prompt_secrets(),
            # Defense-in-depth: the same Core-owned advisory contract the graph-node
            # NodeServices carries, so the supervisor's own read-only turn gets it too.
            security_preamble=self._security_preamble(),
            # Share the orchestrator clock so the supervisor's ``provider_attempts``
            # timestamps come from the same source as the rest of the run's audit.
            clock=self._clock,
        )

    def _engine_finalize(self, p: _Pipeline, inputs: NodeInputs) -> str | None:
        """The publish node's finalize hook: write the supervisor summary, move the task file, and
        write the committed summary.

        The constant supervisor layer synthesizes ``summary.{md,json}`` at whole-task close (before
        publish, so the ``summary.md`` is the PR body); ``_finalize_task_artifacts`` then falls back
        to the deterministic minimal summary when the advisory synthesis could not run — so a
        summary is *always* written, one way or the other (``config.summary_enabled`` is gone).

        The transitions below are logged (with elapsed time on the supervisor synthesis) so the
        end-of-run tail is never a silent window: the whole-task summary can take minutes of
        context assembly + one LLM call, and without a heartbeat that looked like a hang."""
        log = self._log(p.task.id)
        log.info("task finalize: starting (whole-task summary, then publish prep)")
        degraded = False
        if self._supervisor is not None:
            started = time.monotonic()
            memory_on = self._config.memory.enabled
            finalized = self._supervisor.finalize(
                task_id=p.task.id,
                task_title=p.task.title,
                task_path=inputs.task_path,
                emit_delta=memory_on,
            )
            # Already merged with the evaluator findings a gate let past (the layer does that
            # itself), so this is the whole list `.worc/follow-ups.md` receives for the task.
            p.follow_ups = finalized.follow_ups
            if memory_on:
                self._write_memory(p, finalized.candidate_delta, WriteSource.SUCCESS)
            log.info(
                "task finalize: supervisor summary written",
                extra={"elapsed_seconds": round(time.monotonic() - started, 1)},
            )
            # A provider-authored synthesis was expected here. If no summary.md exists after
            # finalize (the turn produced nothing and no prior good summary was preserved), the
            # deterministic minimal summary will silently replace it — make that degradation loud
            # (WARNING + a visible callout in the fallback body) instead of shipping a stub as if
            # it were the full synthesis. This is now the ONLY fallback: finalize runs fresh from
            # the packet, so there is no warm-session path left to degrade to.
            summary_md_path = (
                task_artifact_dir(self._artifacts_root, p.task.id) / SUMMARY_MD_FILENAME
            )
            degraded = not summary_md_path.exists()
            if degraded:
                log.warning(
                    "task finalize: summary degraded to deterministic fallback "
                    "(no provider-authored synthesis)"
                )
        self._append_skip_section(p)  # note skipped nodes on the supervisor summary (idempotent)
        log.info("task finalize: publish prep (committed summary + task-file move)")
        summary_md = self._finalize_task_artifacts(p, Status.DONE, degraded=degraded)
        return str(summary_md) if summary_md is not None else None

    # --- memory write path (best-effort; never blocks publish or a terminal) --------------

    def _memory_service(self) -> MemoryService | None:
        """Build a ``MemoryService`` for this run, or ``None`` when memory is disabled.

        The service is given a live-repo ``DerivedIndex`` (same construction as the cleanup hook) so
        the write funnel validates entity-card paths against the current tree: an
        unverifiable card is downgraded off ``repo-observed`` and quarantined, not kept durable.
        """
        if not self._config.memory.enabled:
            return None
        mem_layout = MemoryLayout(self._layout.private_home)
        ensure_store(mem_layout, created_at=self._clock())
        index = DerivedIndex(self._config.repo.local_path, derived_dir=mem_layout.derived)
        return MemoryService(
            mem_layout,
            config=self._config.memory,
            marker=self._memory_marker,
            index=index,
            extra_secrets=self._memory_extra_secrets(),
        )

    @property
    def _advanced_mode(self) -> bool:
        """Whether this run is in the operator's advanced mode (``strict_isolation: false``).

        Named rather than spelled out at each use, because it is asked in five places that must all
        agree: the loud run-log line, the frozen bundle's metadata, and three of the ledger paths.
        """
        return not self._config.security.strict_isolation

    def _memory_extra_secrets(self) -> tuple[str, ...]:
        """Known secret literals to scrub from every memory write, beyond the structural
        patterns — a structural-only scrub let a known literal through.

        The same sources the provider adapters scrub from artifacts, by the same rule: the values of
        secret-named parent env vars (the allowlist excuses one only while it is also the gate on
        what a child receives — in advanced mode it is not) + the contents of the repo's denied-read
        files (`.env` / `secrets/**`). Best-effort and read-only (missing files are skipped); the
        values are only ever used as redaction literals and are never themselves written anywhere.
        """
        security = self._config.security
        return secret_env_values(
            security.allowed_environment, exempt_allowlisted=security.strict_isolation
        ) + read_denied_secrets(self._config.repo.local_path, security.denied_read_paths)

    def _packet_builder(self) -> PacketBuilder | None:
        """Build the read-path ``PacketBuilder`` for this run, or ``None`` when memory is disabled.

        Read-only: it never mutates the store and writes no audit rows, so it needs no marker and
        does not seed the tree (a missing store reads as empty → an empty packet → no file).
        The per-node packet is built lazily by the node runner only when the role prompt references
        ``{memory_path}`` (node-driven), so a disabled config touches nothing."""
        if not self._config.memory.enabled:
            return None
        mem_layout = MemoryLayout(self._layout.private_home)
        return PacketBuilder(
            MemoryService(mem_layout, config=self._config.memory), self._config.memory
        )

    def _memory_marker(self, row: Mapping[str, Any]) -> None:
        """Mirror one memory audit row into the existing ``evaluations`` decision trail."""
        timestamp = row.get("timestamp")
        self._store.record_evaluation(
            EvaluationRow(
                task_id=str(row.get("task_id") or ""),
                kind="memory_write",
                verdict="advisory",
                findings_json=json.dumps(
                    {key: row.get(key) for key in ("action", "affected_ids", "post_hash")},
                    ensure_ascii=False,
                ),
                created_at=timestamp if isinstance(timestamp, str) else None,
            )
        )

    def _record_failure_memory(self, p: _Pipeline) -> None:
        """Deterministic short-term failure episode (no LLM); never long-term."""
        self._write_memory(p, None, WriteSource.FAILURE)

    def _write_memory(
        self,
        p: _Pipeline,
        delta: CandidateDelta | None,
        source: WriteSource,
    ) -> None:
        """Write the per-task episode (+ a SUCCESS candidate delta) through ``apply_delta``.

        Best-effort: a memory write must never block publish or a terminal transition, so every
        failure is logged and swallowed. The store is built lazily, so a disabled config touches
        nothing. The episode is write-only — never injected into an agent's
        context — so it carries no rotting pointer: the log-dir
        ``artifact_paths`` and the terminal-status ``stage_outcomes`` are dropped; only ``task_id``
        and this task's changed ``touched_paths`` (real, non-rotting signal for a future consumer)
        remain.
        """
        service = self._memory_service()
        if service is None:
            return
        now = self._clock()
        # This task's changed paths (per-task chain base). Best-effort: a git hiccup must never
        # block the episode.
        try:
            touched = tuple(self._git.changed_code_paths_since_task_base())
        except GitCommandError:
            touched = ()
        episode = EpisodeRecord(
            id=f"ep_{p.task.id}",
            task_id=p.task.id,
            created_at=now,
            trust_level=TrustLevel.ARTIFACT_BACKED,
            touched_paths=touched,
        )
        audit = AuditContext(timestamp=now, actor=AuditActor.FINALIZER, task_id=p.task.id)
        try:
            service.apply_delta(delta, episode=episode, source=source, audit=audit)
        except Exception as exc:
            self._log(p.task.id).warning(
                "memory write failed (best-effort, ignored)",
                extra={"error_type": type(exc).__name__, "source": source.value},
            )

    def _engine_facts(
        self, completeness: Completeness, snapshot: FlowSnapshot
    ) -> Callable[[str], bool]:
        """Resolve a flow ``when`` fact (``derived.*`` / ``config.*``) to a boolean.

        Per-task node-disable does not flow through a ``config.*_enabled`` fact — it is handed to
        the engine directly as ``disabled_nodes`` (keyed by node id), so the only facts here are the
        deterministic refinement-skip and the flow-capability ``config.external_research``.
        """
        # Refinement-skip is deterministic — driven purely by completeness classification, never a
        # task flag: a ``complete`` task skips refinement, anything else runs it.
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

    def _observes_step(
        self, mode: ObserveMode, node: FlowNode, outcome: NodeOutcome, node_run_id: int
    ) -> bool:
        """Whether the cadence in force spends an observation turn on this completed step.

        The ``events`` mode is the only one that needs facts beyond the node id, so it is the
        only one that reads the step's ``node_runs`` row — a single primary-key lookup, and none at
        all under ``all`` / ``selected`` / ``none``. ``include_nodes`` and ``triggers`` are
        global-only (a flow narrows the *mode*, nothing else), so they come straight from config.

        The fallback fact comes from the flow recorder, the same derivation the finalize packet's
        step record uses, so this gate and the summary cannot disagree on whether a step deviated.
        """
        observe = self._config.supervisor.observe
        triggers: frozenset[str] = frozenset()
        if mode is ObserveMode.EVENTS:
            row = self._store.get_node_run(node_run_id)
            triggers = observe_cadence.triggers_for(
                outcome_kind=outcome.kind,
                rework_exhausted=outcome.rework_exhausted,
                status=row.status if row is not None else None,
                fell_back=row is not None and fell_back_from(row) is not None,
            )
        return observe_cadence.should_observe(
            mode=mode,
            node_id=node.id,
            include_nodes=observe.include_nodes,
            enabled_triggers=observe.triggers,
            triggers=triggers,
        )

    def _announce_observe_cadence(
        self, p: _Pipeline, configured: ObserveMode, in_force: ObserveMode
    ) -> None:
        """Say it once per run when the flow observes less often than the operator asked for.

        The narrowing itself is legitimate — a flow is the narrower authority, and the validator
        refuses the other direction — but it must not happen in silence. An operator who configures
        ``events`` with ``triggers: [rework, failure, fallback]`` and then runs a packaged content
        flow (they all ship ``none``) would otherwise get run after run with a real provider
        fallback in them and not one observation, with nothing anywhere saying why. So the line
        names both modes and the triggers that stop applying, which is the part that is actually
        surprising: the triggers are configured globally and discarded per flow.

        Whether a ``failure`` / ``fallback`` trigger *should* survive a narrowing is a question for
        the operator, not for this method. It makes the loss visible; it does not decide it.
        """
        if in_force is configured:
            return
        extra: dict[str, object] = {
            "configured_observe_mode": configured.value,
            "observe_mode": in_force.value,
        }
        if in_force is ObserveMode.NONE and self._config.supervisor.observe.triggers:
            extra["dropped_triggers"] = ",".join(self._config.supervisor.observe.triggers)
        self._log(p.task.id).info(
            "observation cadence narrowed by the flow; the whole-task summary is unaffected",
            extra=extra,
        )

    def _engine_post_node(
        self,
        p: _Pipeline,
        inputs: NodeInputs,
        snapshot: FlowSnapshot,
        *,
        live_flow_dir: Path | None = None,
        control_digest: str | None = None,
        control_bundle_dir: Path | None = None,
    ) -> Callable[[FlowNode, NodeOutcome, int], None]:
        """Engine post-node hook: verify the live control plane is unchanged, let the
        supervisor layer observe the completed step (when the kind is observable at all and the
        cadence in force selects it), persist a node's output_artifact slot + its
        generic ``<node_id>.out.md``, and — for the decomposition ``proposed_by`` node — decide +
        materialize the decomposition."""
        decomp = snapshot.doc.decomposition
        # The observation cadence for this run: the flow's own mode when it declares one, else the
        # operator's global mode (the validator has already refused a flow that widens it). Resolved
        # once per run from data — the engine never maps a flow name or a node id to a mode.
        flow_supervisor = snapshot.doc.supervisor
        flow_observe = flow_supervisor.observe if flow_supervisor is not None else None
        configured_mode = self._config.supervisor.observe.mode
        observe_mode = observe_cadence.resolve_mode(
            configured_mode,
            flow_observe.mode if flow_observe is not None else None,
        )
        self._announce_observe_cadence(p, configured_mode, observe_mode)
        # Redaction literals for the node-output writer, harvested once per run (same set the memory
        # write path uses): raw structured output is not adapter-redacted, so scrub it at write.
        node_output_secrets = self._memory_extra_secrets()
        # The flow's private report dir (or None) for a `report` output_artifact slot: the migrated
        # security_audit report node returns the report as structured output, which the orchestrator
        # writes here privately instead of the agent writing into .worc/ itself.
        report_dir = resolve_output_policy(snapshot.doc.output_policy, p.task.id).report_dir(
            self._config.repo.local_path
        )
        # Live control-plane digests already warned about in this run — see the verify method.
        reported_control_drift: set[str] = set()

        def post_node(node: FlowNode, outcome: NodeOutcome, node_run_id: int) -> None:
            # Compare the live control plane (flow / role / tool) against the freeze before any
            # downstream consumer runs. The quiescence barrier has proven the provider tree
            # quiescent for this node's attempt(s), so a diff here means a control file moved under
            # the running task — but the fingerprint cannot say whose hand moved it, and on a
            # repository whose operator edits flows daily it is usually theirs. So a content change
            # warns and the run carries on over the frozen bytes, while a file *substituted* by a
            # symlink or hard-link is still fail-closed. See the method.
            if live_flow_dir is not None and control_digest is not None:
                self._verify_control_plane_unchanged(
                    p,
                    snapshot,
                    live_flow_dir,
                    control_digest,
                    control_bundle_dir,
                    reported_control_drift,
                )
            # The constant supervisor layer observes the completed step read-only (advisory),
            # subject
            # to two independent gates. First the kind: the three in `_UNOBSERVED_NODE_KINDS` are
            # never observable — the terminal `publish` node (its finalize hook already wrote the
            # summary) and the deterministic `tool` / `checks` nodes, whose result is already a
            # durable fact the finalize packet carries. Then the operator's cadence: `all` observes
            # every observable step, `selected` the listed ids, `events` only a deviation, `none`
            # nothing at all. The whole-task summary is unaffected by any of it — finalize is seeded
            # by the deterministic packet, not by these notes.
            if (
                self._supervisor is not None
                and node.kind not in _UNOBSERVED_NODE_KINDS
                and self._observes_step(observe_mode, node, outcome, node_run_id)
            ):
                self._supervisor.observe(
                    task_id=p.task.id,
                    node_id=node.id,
                    node_run_id=node_run_id,
                    outcome_kind=outcome.kind,
                    final_message=outcome.final_message,
                    # An evaluator's findings are the substance of the step it just observed.
                    # Passing only the outcome label had the supervisor acknowledge `accept` for a
                    # node that had filed a substantive finding, and then describe the gate as
                    # having passed in the whole-task summary.
                    findings=outcome.findings,
                )
            # A non-blocking evaluator that spent its whole `max_rework_per_stage` budget and
            # accepted with findings still open: warn the operator (console, always — independent of
            # Telegram) so a human knows the stage moved on and may need follow-up.
            rework_exhausted = False
            if isinstance(node, EvaluatorNode) and outcome.rework_exhausted:
                rework_exhausted = True
                self._log(p.task.id).warning(
                    "evaluator accepted after exhausting its rework budget — continuing; "
                    "stage may need follow-up",
                    extra={
                        "stage": node.id,
                        "max_rework_per_stage": node.max_rework_per_stage,
                        "findings": len(outcome.findings),
                    },
                )
            # A node that has a shell but no write access changed the working tree: its sandbox
            # write-denies the whole clone (an operator tool has no business writing either), so
            # this means that enforcement did not hold. Warn and keep going — the node's own outcome
            # stays `done` and the task is never parked over it, because such a node exists to read
            # and a stray file is not worth trading that for. The change is not consumed by
            # anything downstream.
            if outcome.unexpected_write:
                self._log(p.task.id).warning(
                    "a node with no write access changed the working tree — continuing; inspect "
                    "the tree, this should have been denied",
                    extra={"stage": node.id},
                )
            # The sharper half of the same never-park rule, on every node class including the
            # writing one: git control state moved — a moved `HEAD`, the index, a hook,
            # `.git/config`. Most of that is the operator working in their own repository, which is
            # why it does not stop the task; the part that is not is why this stays a warning rather
            # than an info, because continuing means the orchestrator's own next git command
            # (commit / branch switch / push) runs in that clone. So it names the drifted aspect and
            # says to stop the run rather than merely "inspect" — the operator is the one who can
            # tell their own commit from a planted hook.
            if outcome.git_control_drift is not None:
                self._log(p.task.id).warning(
                    "git control state changed during this node — continuing per policy; if you "
                    "did not do this yourself, stop the run and discard the clone before it is "
                    "committed or pushed",
                    extra={"stage": node.id, "drift": outcome.git_control_drift},
                )
            # Publishing had to merge in commits this orchestrator did not make. The run is a
            # success — the combination was re-checked BEFORE anything went out — but the task's
            # reported diff is measured from the base, so it now covers someone else's work too. A
            # pull request carries the same sentence in its body; with `publish: push`/`commit`
            # there is no body, and this warning plus the ⚠️ trace below is where it is said.
            if outcome.adopted_commits:
                self._log(p.task.id).warning(
                    "publishing merged in %d commit(s) this run did not record pushing (%s) — the "
                    "task's reported diff is measured from the base and now covers them too",
                    len(outcome.adopted_commits),
                    ", ".join(outcome.adopted_commits[:5]),
                    extra={"stage": node.id},
                )
            # A gating verdict none of whose gating findings names a source path. The rework edge
            # leads to `fixing`, whose job is to open a named location and change it, so this round
            # can only end in a refusal — which is what an evaluator reporting it *could not
            # review* produces, and the findings contract (nullable `path` by design) has no way to
            # say that instead. Deliberately a warning and not a park: the loop keeps its named
            # budget, and a verdict that gates for a real reason but was written without a path
            # must not be discarded. This line is what lets an operator tell a wasted round from a
            # productive one while it is still running, instead of reading it out of the ledger
            # afterwards.
            findings_without_a_path = False
            if outcome.gating_findings_name_no_path:
                findings_without_a_path = True
                self._log(p.task.id).warning(
                    "the gating findings name no source path, so the fix step has nothing to open "
                    "— continuing per policy; if the evaluator was reporting that it could not "
                    "review rather than a defect, this round will change nothing",
                    extra={
                        "stage": node.id,
                        "findings": len(outcome.findings),
                        "first_finding": _first_finding_reason(outcome.findings),
                    },
                )
            # Best-effort live progress trace: one message per executed node finish (never on a
            # skip). Gated on the flag alone — when Telegram is off the notifier is a NullNotifier
            # and this is a no-op. Carries only node id + outcome (no secrets); never raises. A
            # budget-exhausted accept traces as the ⚠️ TRACE_REWORK_EXHAUSTED label, not a clean ✅;
            # so does a write-less node that wrote (TRACE_UNEXPECTED_WRITE) or drifted git control
            # state (TRACE_GIT_CONTROL_DRIFT — checked first of the two, it is the one that
            # needs a human now).
            if self._config.telegram.trace:
                trace_outcome = outcome.kind
                if rework_exhausted:
                    trace_outcome = TRACE_REWORK_EXHAUSTED
                elif outcome.git_control_drift is not None:
                    trace_outcome = TRACE_GIT_CONTROL_DRIFT
                elif outcome.unexpected_write:
                    trace_outcome = TRACE_UNEXPECTED_WRITE
                elif outcome.adopted_commits:
                    trace_outcome = TRACE_ADOPTED_COMMITS
                elif findings_without_a_path:
                    # Last of the five: a wasted rework round is worth saying, but never at the
                    # cost of the single label slot when git control state also moved.
                    trace_outcome = TRACE_FINDINGS_WITHOUT_A_PATH
                self._notifier.send_trace(task_id=p.task.id, node_id=node.id, outcome=trace_outcome)
            # Chronological per-run index: one line per executed node run of every kind, so an
            # operator can read a re-running node's sequence without listing run-*/ dirs. Runs that
            # raise before returning (evaluator schema-fail, checks/tool manual) are absent — they
            # produce no complete payload to index and are recorded in node_runs anyway. The dir
            # relpath is computed purely (no mkdir), so a payload-less node leaves no empty run dir.
            run_rel = (
                node_run_dir(self._artifacts_root, p.task.id, node.id, node_run_id)
                .relative_to(task_artifact_dir(self._artifacts_root, p.task.id))
                .as_posix()
            )
            append_node_history(
                self._artifacts_root,
                p.task.id,
                node.id,
                {
                    "run_id": node_run_id,
                    "node_id": node.id,
                    "kind": node.kind,
                    "outcome": outcome.kind,
                    "findings": len(outcome.findings),
                    "dir": run_rel,
                },
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
                exchange_root=str(self._exchange_root),
                extra_secrets=node_output_secrets,
                report_dir=report_dir,
            )
            # Generic node-output channel: persist every agent node's output as {<node_id>_path}
            # (redaction-scrubbed, local/uncommitted). A node filling a special slot above writes no
            # duplicate — write_node_output is a no-op when output_artifact is set. A node that
            # declares `output_file` publishes that file's content instead of its closing message,
            # read from the only directory it was allowed to write into.
            write_node_output(
                node,
                outcome,
                artifacts_root=self._artifacts_root,
                task_id=p.task.id,
                node_run_id=node_run_id,
                register=self._register_artifact,
                extra_secrets=node_output_secrets,
                exchange_root=str(self._exchange_root),
                produced_dir=report_dir or Path(self._config.repo.local_path),
                warn=lambda message: self._log(p.task.id).warning(
                    message, extra={"stage": node.id}
                ),
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
        """Complete the persisted ``preparing`` checkpoint and attach the task branch (per mode)."""
        # Guarantee the `.worc/` runtime home is gitignored in this clone, regardless of how it was
        # scaffolded, so it never leaks into the operator's git status (no branch exists yet). The
        # same guarantee for an assigned toolchain cache inside the clone: `worc preflight` also
        # repairs those rules, but an operator who edits the path afterwards would otherwise
        # carry an unignored cache into this task's diff.
        self._git.ensure_runtime_excludes()
        self._git.ensure_assigned_cache_excludes()
        p.slug = slugify(p.task.title)
        epoch = int(time.time())  # makes a fresh attempt's branch unique (re-run never collides)
        mode = self._branch_mode(p.task)
        p.branch = self._observe(
            p,
            "branch preparation",
            lambda: self._git.prepare_branch(
                p.task.id,
                p.slug,
                epoch=epoch,
                branch_name=p.task.branch_name,
                mode=mode,
                branch_ref=p.task.branch_ref,
            ),
        )
        self._store.update_task(p.task.id, branch=p.branch, slug=p.slug)

    def _append_skip_section(self, p: _Pipeline) -> None:
        """Append the skipped-nodes section to ``summary.md`` (idempotent within a run).

        Only a provider-authored body needs this: the deterministic report renders the same section
        from the same renderer, so the heading — which is also the idempotency key — exists once.
        """
        if not p.skip:
            return
        md_path = task_artifact_dir(self._artifacts_root, p.task.id) / SUMMARY_MD_FILENAME
        if not md_path.exists():
            return
        existing = md_path.read_text(encoding="utf-8")
        if SKIPPED_NODES_HEADING in existing:
            return
        md_path.write_text(
            existing.rstrip("\n") + "\n\n" + render_skipped_nodes_section(p.skip),
            encoding="utf-8",
            newline="",
        )

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
                    subject=merge_commit_subject(
                        p.task.id, p.task.title, pr_url, p.task.commit_type
                    ),
                    body="",  # see merge_task: the branch's own commits are not a merge body
                ),
            )
        except GitCommandError as exc:
            raise ManualActionRequired(f"auto-merge blocked: {exc}") from exc
        return self._go_terminal(
            p, Status.DONE, pr_url=pr_url, already_moved=True, merge_outcome=outcome
        )

    def _fallback_summary_path(self, p: _Pipeline) -> str:
        """The logs/ working copy of summary.md — PR body fallback when no task file is on disk."""
        return str(task_artifact_dir(self._artifacts_root, p.task.id) / SUMMARY_MD_FILENAME)

    def _summary_md_body(self, p: _Pipeline, *, degraded: bool = False) -> str:
        """The human-readable summary text; falls back to the deterministic report.

        ``degraded`` marks the DONE-path case where a provider-authored synthesis was expected but
        failed (see ``_engine_finalize``); it reaches the report as a visible callout.
        """
        md_path = task_artifact_dir(self._artifacts_root, p.task.id) / SUMMARY_MD_FILENAME
        if not md_path.exists():
            self._write_deterministic_summary(p, degraded=degraded)
        return md_path.read_text(encoding="utf-8") if md_path.exists() else (p.task.title + "\n")

    def _write_deterministic_summary(self, p: _Pipeline, *, degraded: bool) -> None:
        """Write the deterministic ``summary.{md,json}`` report from the run's recorded facts.

        Reads the same durable facts the oversight layer's own close-out is grounded in, so the two
        bodies cannot disagree about what the run did, and the evaluator findings a gate let past
        reach the pull-request body on every path rather than only the local metadata.
        """
        evaluations = self._store.get_evaluations(p.task.id)
        # Merged, not assigned: on a degraded DONE the supervisor already computed its own list
        # (and merged the same findings into it) but produced no prose, so this writer runs second.
        # A bare assignment would drop the layer's own debt notes from the body AND from the
        # accumulating file — and the merge is a no-op on every path where nothing set them.
        follow_ups = merge_follow_ups(p.follow_ups, evaluator_finding_follow_ups(evaluations))
        p.follow_ups = follow_ups
        write_summary_report(
            self._artifacts_root,
            build_packet_facts(
                self._store,
                task_id=p.task.id,
                task_title=p.task.title,
                task_type=p.task.task_type,
                flow_name=p.flow_name,
                evaluations=evaluations,
                artifacts_root=self._artifacts_root,
                exchange_root=self._exchange_root,
                repo_dir=self._config.repo.local_path,
            ),
            follow_ups=follow_ups,
            gates=render_gate_digest(evaluations),
            skipped_nodes=p.skip,
            task_ref=self._task_ref(p),
            degraded=degraded,
            # Present exactly when the layer made calls, so an operator can tell "the layer never
            # ran" from "it ran and could not finish" without a second marker.
            supervisor_usage=summarize_spend(self._store.get_provider_attempts_for_task(p.task.id)),
        )

    def _task_ref(self, p: _Pipeline) -> str | None:
        """A short sibling-relative pointer to the task file for the committed summary.

        The committed ``<id>.summary.md`` lives next to the moved ``<id>.md`` task file, so the
        basename is the correct, move-independent reference. ``None`` for a synthetic ``run`` path.
        """
        return Path(p.task_file).name if p.task_file else None

    def _capture_governance_changed(self, p: _Pipeline) -> None:
        """Record which governance/instruction files this run changed and warn the operator.

        Called at finalize, before terminal cleanup restores the working tree, so the base-anchored
        diff still reflects the task's net change (including files earlier decomposed subtasks
        committed). Governance/instruction files are ordinary, editable content — a change is a
        non-blocking notice (this console/log WARNING, plus the PR summary, ledger, and Telegram),
        never a block. Empty on ordinary tasks, so there is no noise there.
        """
        p.governance_changed = governance_changed_paths(
            self._git.changed_code_paths_since_task_base()
        )
        if p.governance_changed:
            self._log(p.task.id).warning(
                "governance/instruction files changed by this task (notice, not a block): %s",
                ", ".join(p.governance_changed),
                extra={"governance_changed": list(p.governance_changed)},
            )

    def _finalize_task_artifacts(
        self, p: _Pipeline, final: Status, *, degraded: bool = False
    ) -> Path | None:
        """Move the task into its lifecycle folder; write the committed `<id>.summary.md` alongside.

        Runs **before** the commit so both land in the task (audit) commit. Returns the
        path to the committed `summary.md`, or ``None`` when there is no on-disk task file (e.g. a
        synthetic ``run`` path). ``summary.json`` and the rest of ``logs/`` are never committed.

        ``degraded`` (DONE path only) flows into the deterministic fallback body as a visible
        "fallback summary" callout when the supervisor synthesis was expected but failed.
        """
        self._capture_governance_changed(p)
        dest = self._move_task_file(p, final)
        body = self._summary_md_body(p, degraded=degraded)
        if p.governance_changed:
            body += _render_governance_section(p.governance_changed)
        # After `_summary_md_body`, because that call is what runs the deterministic producer of
        # `p.follow_ups`; and before the `dest is None` return, because a task with no on-disk file
        # (a synthetic `run`) still leaves debt worth accumulating.
        self._append_follow_ups_file(p)
        if dest is None:
            return None
        summary_path = dest.with_name(f"{p.task.id}.summary.md")
        try:
            # ``newline=""``: this copy is committed into the operator's repository, so the host's
            # line separator must not decide what lands in their history.
            summary_path.write_text(body, encoding="utf-8", newline="")
        except OSError:
            return None
        self._register_artifact(p.task.id, "summary_md", str(summary_path))
        return summary_path

    def _append_follow_ups_file(self, p: _Pipeline) -> None:
        """Accumulate this task's follow-ups into ``.worc/follow-ups.md``: append-only, best-effort.

        The one append site: it is reached on both terminal paths that finalize (the ``done``
        finalize and the infra-terminal publish), which is also why the tuple travels on
        ``_Pipeline`` instead of being re-derived here. The paths that bypass finalize need
        nothing — ``_resume_cleanup`` closes a task whose finalize already ran in the previous
        process, and the manual ``finalize`` command closes one that never produced a summary or a
        follow-up.

        Best-effort by contract: an unwritable control home is one WARNING, never a change to the
        task's terminal status. The file is the operator's to curate, so nothing here reads it back.
        A task with no follow-ups writes nothing — the writer owns that guard, so this call is
        unconditional.
        """
        path = self._layout.control_home / FOLLOW_UPS_FILENAME
        try:
            append_task_follow_ups(
                path,
                task_id=p.task.id,
                task_title=p.task.title,
                finished_at=self._clock(),
                follow_ups=p.follow_ups,
            )
        except OSError as exc:
            self._log(p.task.id).warning(
                "follow-ups file not updated (%d item(s) reach summary.json and the PR body only)",
                len(p.follow_ups),
                extra={"path": path.as_posix(), "error": str(exc)},
            )

    # --- terminal handling ----------------------------------------------------------------

    def _exchange_active_unsafe(self, task_id: str) -> bool:
        """True when the task's active exchange is flagged unsafe by the quiescence barrier.

        The provider tree could not be proven quiescent, so no Git action, cleanup checkout, or
        exchange seal may run against a tree an unknown descendant might still be writing — no
        manifest, check, Git action, seal, or next task happens before quiescence is proven.

        A task with no row yet (never registered) reads as safe."""
        try:
            _contaminated, active_unsafe = self._store.get_exchange_guard(task_id)
        except KeyError:
            return False
        return active_unsafe

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
        if self._exchange_active_unsafe(p.task.id):
            # Quiescence barrier: the provider tree is not proven quiescent, so no Git
            # action may run against a tree an unknown descendant might still be writing. Withhold
            # the terminal commit/push (the failure report is already written); ``_go_terminal``
            # likewise withholds the cleanup checkout and the seal, and the flag blocks the next
            # launch until an operator resolves.
            self._log(p.task.id).warning(
                "infra-terminal publish withheld: provider tree not proven quiescent "
                "(exchange unsafe); no commit/push"
            )
            return self._go_terminal(p, status, manual_reason=error)
        moved = False
        try:
            moved = self._finalize_task_artifacts(p, status) is not None
            verb = "failed attempt" if status is Status.FAILED else "manual action required"
            self._git.commit_code(p.task.id, f"chore({p.task.id}): {verb} — {p.task.title}")
            self._git.commit_audit(p.task.id, task_packet_digest=self._task_packet_digest(p))
            self._git.push(p.task.id, p.branch)
        except (GitCommandError, OSError) as exc:
            self._log(p.task.id).warning(
                "infra-terminal publish incomplete", extra={"error": str(exc)}
            )
        return self._go_terminal(p, status, manual_reason=error, already_moved=moved)

    def _explain_terminal(self, p: _Pipeline, final: Status, reason: str | None) -> bool:
        """Say why a non-``done`` terminal happened, and leave the artifacts needed to act on it.

        Three things, in the order a human reads them: the reason as a ``warning`` beside the
        status change, ``failure_report.json``/``stuck.md`` for the failing node's evidence, and
        the run summary for what did happen before the stop. Each is produced only when it is not
        already there — ``_fail`` and the engine's fix-budget recorder write the report on their
        own paths, the publish node's finalize hook writes the summary on its own — so this fills
        the gap rather than doing anyone's work twice.

        The summary degrades to the deterministic minimum (the packet's durable facts; no LLM, no
        supervisor turn), which on this path is the only option: the finalize hook lives inside the
        publish node, and a task that stopped at an earlier node never reaches it. Returns whether
        the task file was moved, which the caller folds into its ``already_moved``.

        Best-effort by construction. The terminal status is already computed and must stay stable,
        so a filesystem or git problem here is logged rather than raised.
        """
        log = self._log(p.task.id)
        if reason:
            log.warning("task stopped", extra={"final_status": final.value, "reason": reason})
        row = self._store.get_task(p.task.id)
        if row is not None and not row.failure_report_path:
            self._write_infra_failure_report(
                p,
                node_id=self._store.get_flow_checkpoint(p.task.id)[0],
                error=reason or f"terminal transition to {final.value}",
                run_state=None,
            )
        if (task_artifact_dir(self._artifacts_root, p.task.id) / SUMMARY_MD_FILENAME).exists():
            return False
        try:
            return self._finalize_task_artifacts(p, final) is not None
        except (GitCommandError, OSError) as exc:
            log.warning("terminal summary not written", extra={"error": str(exc)})
            return False

    def _write_infra_failure_report(
        self,
        p: _Pipeline,
        *,
        node_id: str | None,
        error: str,
        run_state: FlowRunState | None,
    ) -> None:
        """Write ``failure_report.json`` + ``stuck.md`` for an infra terminal (best-effort).

        Reuses the flow-neutral ledger writer. No fix-loop budget was spent here, so the report is
        marked as such and ``limit_name`` carries the infra error. Every provider attempt of the
        failing node is named, because an artifact reporting only the class the Router settled on
        hides both the real cause and the fact that a fallback was tried at all. A write or read
        failure must never mask the terminal outcome, so it is logged, not raised.
        """
        try:
            report_path, _stuck = write_failure_report(
                self._artifacts_root,
                p.task.id,
                loop=INFRA_LOOP,
                limit_name=error,
                counters=dict(run_state.loop_counters) if run_state is not None else {},
                last_check_log=None,
                last_review_findings=read_last_findings(self._store, p.task.id),
                final_diff=read_final_diff(self._artifacts_root, p.task.id),
                failing_node=NodeFailureEvidence(
                    node_id=node_id,
                    provider_attempts=self._provider_attempt_evidence(p.task.id, node_id),
                ),
            )
            self._store.update_task(p.task.id, failure_report_path=report_path)
        except (OSError, sqlite3.Error) as exc:
            self._log(p.task.id).warning("failure report not written", extra={"error": str(exc)})

    def _provider_attempt_evidence(
        self, task_id: str, node_id: str | None
    ) -> tuple[Mapping[str, Any], ...]:
        """The failing node run's provider attempts, projected to secret-free report fields.

        Read from the store rather than threaded through the exception: both node runners record the
        attempts *before* they raise, so every row is already durable by the time a terminal is
        decided — the exception carries the decision input, the store carries the evidence.

        ``()`` when there is no node to attribute the attempts to, because a whole-task dump would
        mix in nodes that already succeeded and the supervisor layer's own provider calls.
        """
        if node_id is None:
            return ()
        runs = [run for run in self._store.get_node_runs(task_id) if run.node_id == node_id]
        # Ascending by id, so the last match is the run that just failed — a fix loop or a subtask
        # region legitimately runs the same node id several times within one task.
        run_id = runs[-1].id if runs else None
        if run_id is None:
            return ()
        # An explicit whitelist, never the whole row: the attempt directory is a path into the
        # private artifact tree and the usage columns are not part of an operator artifact.
        return tuple(
            {
                "provider": row.provider,
                "attempt": row.attempt,
                "error_class": row.error_class,
                "exit_code": row.exit_code,
                "started_at": row.started_at,
            }
            for row in self._store.get_provider_attempts(run_id)
        )

    def _reconcile_open_node_runs(self, task_id: str, *, reason: str) -> None:
        """Close node runs left ``running`` by a hard stop, at a terminal transition.

        A ``--force-full`` SIGKILL kills the daemon mid-node, so the node's own
        ``complete_node_run`` and ``record_provider_attempts`` never run: the ``node_runs`` row is
        stranded ``running`` and the killed provider attempt is unbilled. Every terminal producer
        (``_go_terminal`` and the hand-finish ``finalize_task``) runs this so the orphan is closed
        to ``aborted`` and each
        killed provider node earns a ``provider_attempts`` row — ``usage_delta_status='unknown'``,
        because the partial run's real token usage is not recoverable — so an aborted run is not
        free in the cost roll-up. A no-op on a clean terminal (no orphan rows exist), and it emits
        ``WARNING`` naming the reconciled nodes, since an operator abort is exactly the event a
        operator needs to see and the SIGKILLed daemon logged nothing itself.
        """
        finished_at = self._clock()
        closed = self._store.reconcile_open_node_runs(
            task_id,
            finished_at=finished_at,
            error_class=ErrorClass.CANCELLED.value,
            skip_reason=reason,
        )
        if not closed:
            return
        for row in closed:
            if row.id is None or row.route_primary is None:
                continue  # a non-provider node (checks/publish) has no billable attempt to record
            self._store.record_provider_attempt(
                ProviderAttemptRow(
                    task_id=task_id,
                    node_run_id=row.id,
                    provider=row.route_primary,
                    attempt=1,
                    status="aborted",
                    error_class=ErrorClass.CANCELLED.value,
                    started_at=row.started_at,
                    finished_at=finished_at,
                    usage_delta_status="unknown",
                )
            )
        self._log(task_id).warning(
            "reconciled orphan node runs after termination",
            extra={
                "reconciled": len(closed),
                "nodes": ",".join(r.node_id for r in closed),
                "reason": reason,
            },
        )

    def _go_terminal(
        self,
        p: _Pipeline,
        status: Status,
        *,
        pr_url: str | None = None,
        manual_reason: str | None = None,
        already_moved: bool = False,
        merge_outcome: str | None = None,
        mutation: ExchangeMutationManual | None = None,
    ) -> PipelineResult:
        """Run terminal cleanup, set the final status, append exactly one ledger record.

        ``already_moved`` is set when the task file was moved + committed during finalize; the
        move is then complete on the task branch, so this must not re-move it on ``base_branch``
        after the cleanup checkout.
        """
        final = status
        if self._exchange_active_unsafe(p.task.id):
            # Quiescence barrier: do not run a checkout/cleanup Git action while an unknown
            # descendant may still be writing the working tree. Leave HEAD as-is and report unsafe;
            # the seal is withheld downstream and the flag holds the next task (the pre-launch
            # ``assert_exchange_current_task_only`` refuses to start over an un-cleared exchange)
            # until an operator resolves.
            cleanup = CleanupOutcome(
                safe=False,
                target_branch=self._config.repo.base_branch,
                error="terminal cleanup withheld: provider tree not proven quiescent",
            )
            self._log(p.task.id).warning(
                "terminal cleanup withheld: provider tree not proven quiescent; HEAD left as-is"
            )
        else:
            # A resumable manual park carrying the task's own WIP keeps that WIP (its resume
            # input): terminal cleanup preserves it and leaves HEAD on the branch rather than
            # failing "unaccounted changes". DONE/FAILED still fail-close on a dirty tree.
            preserve_own_wip = (
                status is Status.MANUAL_ACTION_REQUIRED and self._worktree_is_task_output(p.task.id)
            )
            cleanup = self._observe(
                p,
                "terminal cleanup",
                lambda: self._git.terminal_cleanup(
                    p.task.id, mode=self._branch_mode(p.task), preserve_own_wip=preserve_own_wip
                ),
            )
        if not cleanup.safe and status is Status.DONE:
            # Publishing finished but the working copy could not be safely restored → manual.
            final = Status.MANUAL_ACTION_REQUIRED
        if status is not Status.DONE:
            # Deterministic short-term failure episode (no LLM); never long-term.
            self._record_failure_memory(p)
        # Record the terminal-cleanup outcome and the reason this task stopped (when applicable).
        # Surface the true stop reason first; a cleanup problem is secondary context, never a
        # replacement for it — a cleanup error must not mask the node's manual reason.
        last_error: str | None
        if manual_reason and cleanup.error and not cleanup.safe:
            last_error = f"{manual_reason}; terminal cleanup: {cleanup.error}"
        else:
            last_error = manual_reason or cleanup.error
        self._store.update_task(
            p.task.id,
            cleanup_target_branch=cleanup.target_branch,
            cleanup_completed=cleanup.safe,
            cleanup_completed_at=self._clock() if cleanup.safe else None,
            cleanup_last_error=last_error,
            # A terminal task is not parked, and a surviving wake instant would defer a later
            # rerun that nobody is waiting on.
            blocked_since=None,
            blocked_until=None,
        )
        # Only ``done`` explains itself; every other terminal is explained here, from one place,
        # whichever path reached it. A publish failure logs its git stderr and already has a summary
        # (finalize runs inside the publish node), while a node raising ``NodeManualRequired`` comes
        # straight here — without this the operator would see "status changed to
        # manual_action_required" and have to open SQLite to learn why.
        if final is not Status.DONE:
            already_moved = self._explain_terminal(p, final, last_error) or already_moved
        # Close any node run left ``running`` by a hard stop before recording the terminal
        # state. A no-op on the normal path (the engine finalizes every node it runs); it catches a
        # node stranded by an interrupt that still reached ``_go_terminal``.
        self._reconcile_open_node_runs(
            p.task.id, reason=manual_reason or f"terminal transition to {final.value}"
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
            governance_changed=p.governance_changed,
        )
        self._log(p.task.id).info(
            "terminal",
            extra={"final_status": final.value, "pr_url": pr_url, "cleanup_safe": cleanup.safe},
        )
        # Terminal exchange handling: seal a checksum-verified snapshot into the private
        # audit and remove the active in-repo exchange (or quarantine a contaminated tree). It runs
        # after the quiescence barrier has proven the provider tree empty (an unproven tree already
        # set ``exchange_active_unsafe`` and blocks the seal). Never raises — the terminal status is
        # already recorded and must stay stable.
        self._seal_terminal_exchange(p.task.id, final=final, mutation=mutation)
        self._evict_run_artifacts(p.task.id, final=final)
        return PipelineResult(task_id=p.task.id, final_status=final, pr_url=pr_url)

    def _seal_terminal_exchange(
        self, task_id: str, *, final: Status, mutation: ExchangeMutationManual | None = None
    ) -> None:
        """Seal / quarantine the task's active exchange at a terminal transition.

        Never raises: the terminal status is already committed, so any failure is logged and — when
        it leaves an unsealed/undeleted tree — recorded as ``exchange_active_unsafe`` to block
        every later provider launch until an operator resolves it. Idempotent: a task whose exchange
        was already sealed/removed (e.g. an operator ``finalize`` after the pipeline terminal) is a
        no-op. ``mutation`` carries the before/after manifests for a detected-mutation
        terminal, so the contaminated tree is quarantined as evidence and never sealed.
        """
        log = self._log(task_id)
        try:
            contaminated, active_unsafe = self._store.get_exchange_guard(task_id)
        except KeyError:
            return
        if active_unsafe:
            # Quiescence was unproven (``CONTAINMENT_UNVERIFIED``): an unknown descendant may
            # still be writing, so we must not build a manifest or touch the tree. Keep it in place;
            # the flag already blocks every later launch until an operator resolves it.
            log.warning(
                "terminal exchange not sealed: active exchange unsafe (provider tree not proven "
                "quiescent); later launches blocked until resolved"
            )
            return
        if contaminated or mutation is not None:
            self._store.update_task(task_id, exchange_contaminated=1)
            expected = mutation.before if mutation is not None else None
            observed: tuple[str, ...] = ()
            if mutation is not None and mutation.before is not None and mutation.after is not None:
                observed = diff_exchange_manifests(mutation.before, mutation.after)
            try:
                evidence = quarantine_contaminated(
                    self._exchange_root,
                    self._artifacts_root,
                    task_id,
                    expected=expected,
                    observed_changes=observed,
                )
                log.warning(
                    "contaminated exchange quarantined (never restore-eligible)",
                    extra={"evidence": evidence.as_posix()},
                )
            except ExchangeCleanupBlocked as exc:
                self._store.update_task(task_id, exchange_active_unsafe=1)
                log.error("contaminated exchange quarantine blocked", extra={"error": str(exc)})
            except OSError as exc:
                # The quarantine move (mkdir/copy2/rename) can raise a bare OSError (ENOSPC /
                # EACCES) that is not an ExchangeCleanupBlocked. Honor "Never raises": flag the tree
                # unsafe (blocks later launches) instead of crashing into the daemon-crash path
                # with no signal.
                self._store.update_task(task_id, exchange_active_unsafe=1)
                log.error(
                    "contaminated exchange quarantine failed (unexpected OS error)",
                    extra={"error": str(exc)},
                )
            return
        try:
            result = seal_exchange(
                self._exchange_root,
                self._artifacts_root,
                task_id,
                metadata={"final_status": final.value, "sealed_at": self._clock()},
            )
            if result is not None:
                log.info(
                    "terminal exchange sealed",
                    extra={"seal": result.seal_dir.as_posix(), "files": result.entry_count},
                )
        except ExchangeCleanupBlocked as exc:
            # The snapshot sealed but the active dir could not be removed (a lock). The seal is
            # kept (restore stays possible); block later launches until the lock is cleared.
            self._store.update_task(task_id, exchange_active_unsafe=1)
            log.error("terminal exchange cleanup blocked", extra={"error": str(exc)})
        except (ExchangeError, ExchangeSealError) as exc:
            # A path-safety violation surfaced by the seal walk (a planted symlink/hard-link/special
            # file): the tree is not a clean snapshot. Block later launches and surface it.
            self._store.update_task(task_id, exchange_active_unsafe=1)
            log.error("terminal exchange seal failed (unsafe surface)", extra={"error": str(exc)})
        except OSError as exc:
            # mkdir/copy2/write_text can raise a bare OSError (ENOSPC on a full disk, EACCES)
            # after the terminal status + ledger were already written — not an ExchangeError/
            # ExchangeSealError. Honor the "Never raises" contract: flag the tree unsafe (blocks
            # every later launch) and log the precise target, instead of silently crashing into the
            # daemon-crash path with no signal and no unsafe flag.
            self._store.update_task(task_id, exchange_active_unsafe=1)
            log.error(
                "terminal exchange seal failed (unexpected OS error)", extra={"error": str(exc)}
            )

    def _evict_run_artifacts(self, task_id: str, *, final: Status) -> None:
        """Drop a successful task's own per-task ``runs/`` subtree (opt-out via config).

        A finished task's frozen inputs and sealed exchanges are a rerun/analysis cache, and a
        ``done`` task can never be a ``rerun`` target — so nothing consumes them and one directory
        per task per root would otherwise accumulate forever. Restricted to a *successful* terminal
        in both modes: for any other outcome these directories are the evidence, and deleting them
        at the moment the operator needs them is the one failure this must never have. Quarantined
        exchange evidence is out of reach here by construction.

        Never raises: the terminal status and its ledger record are already written, so a cleanup
        that cannot finish is logged and left for the operator's own ``runs clean``.
        """
        if final is not Status.DONE or not self._config.logging.clean_runs_on_success:
            return
        log = self._log(task_id)
        try:
            contaminated, active_unsafe = self._store.get_exchange_guard(task_id)
        except KeyError:
            return
        if contaminated or active_unsafe:
            # The seal or the teardown did not complete cleanly. The seal may be the only verified
            # copy of a tree still sitting in the repo, and later launches are already blocked
            # pending an operator — so keep everything and say why.
            log.warning(
                "run artifacts kept: exchange not cleanly sealed at terminal",
                extra={"contaminated": bool(contaminated), "active_unsafe": bool(active_unsafe)},
            )
            return
        try:
            removed = remove_task_runs(self._artifacts_root, task_id)
        except (OSError, PathIdentityError) as exc:
            log.error("run artifact eviction failed", extra={"error": str(exc)})
            return
        if removed:
            log.info("run artifacts evicted", extra={"roots": len(removed)})

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

        A decomposition root travels **with the spec files it references**, mirroring the promote
        that brought them in together. That symmetry is load-bearing rather than tidy: the refs in
        a ``subtasks:`` manifest are relative to the root file's own directory, so a root that
        lands in ``done/`` while its specs stay in ``pending/subtasks/`` can no longer resolve its
        own manifest — every ref reads as missing, which is a hard reject, and a reject quarantines
        the very file this move just filed as finished.
        """
        dest = lifecycle_destination(task_file, final)
        if dest is None:
            return None
        src = Path(task_file or "")
        if src.resolve() == dest.resolve():
            return dest  # already in its lifecycle folder (idempotent restart)
        if not src.exists():
            return dest if dest.exists() else None  # already moved
        # Read the manifest while the root is still beside its specs — afterwards the refs no
        # longer resolve from anywhere, which is the defect this move exists to avoid creating.
        refs = read_subtask_refs(src)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dest)
            self._store.update_task(task_id, source_path=str(dest))
        except OSError:
            # Never let a file-move failure mask the terminal outcome; the ledger still records it.
            return None
        self._relocate_subtask_specs(refs, src.parent, dest.parent)
        return dest

    @staticmethod
    def _relocate_subtask_specs(refs: Sequence[str], src_dir: Path, dest_dir: Path) -> None:
        """Carry a decomposition root's spec files to the folder the root just moved into.

        Each destination is the ref re-anchored on the root's **new** directory, never a per-file
        ``lifecycle_destination``: a spec lives one level down, so asking that function where
        ``tasks/pending/subtasks/01-a.md`` belongs answers ``tasks/pending/subtasks/done/01-a.md``
        — a lifecycle folder nested inside the queue rather than beside it.

        Best-effort and idempotent, for the same reason the root's own move is: the task has
        already reached a terminal status and nothing here may mask it. A ref that has gone missing,
        one already at its destination, and an occupied destination are all skipped rather than
        forced, so a re-entered relocation finishes what an interrupted one started.
        """
        for ref in refs:
            source = src_dir / ref
            target = dest_dir / ref
            if not source.is_file() or target.exists():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
            except OSError:
                continue

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
                advanced_mode=self._advanced_mode,
            )
        )
        self._notify_terminal(
            task_id=task_id, final_status=Status.FAILED, pr_url=None, reason=reason
        )
        return PipelineResult(
            task_id=task_id,
            final_status=Status.FAILED,
            validation_reason=reason,
            validation_detail=result.detail or None,
        )

    def _quarantine(self, task_file: str) -> str | None:
        """Move the task file into ``.worc/tasks/rejected/`` (the quarantine) when it exists.

        A relative ``validation.quarantine_folder`` (the default ``./.worc/tasks/rejected``) is
        resolved against the repository root, not the process working directory: the operator runs
        ``worc`` from anywhere inside the clone, so a cwd-relative quarantine would scatter rejected
        task files into whichever subdirectory they happened to be standing in.
        """
        src = Path(task_file)
        if not src.exists():
            return None
        quarantine_dir = Path(self._config.validation.quarantine_folder)
        if not quarantine_dir.is_absolute():
            quarantine_dir = Path(self._config.repo.local_path) / quarantine_dir
        try:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            dest = quarantine_dir / src.name
            src.replace(dest)
            return str(dest)
        except OSError:
            return None

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

    def _security_preamble(self) -> str:
        """The Core-owned orchestrator security contract prepended to every provider prompt.

        Defense-in-depth / advisory only — never enforcement (the sandbox + deny projection are the
        enforcement). Resolved once here (config-derived, not per-node) and threaded to the agent/
        evaluator via ``NodeServices`` and to the supervisor directly: an always-on baseline, a
        read-restraint reinforcement when effective read-isolation is off, a paragraph naming the
        advanced mode, and — only where this host truly has no OS sandbox — a paragraph saying that
        the write floor is not enforced by anything but the agent's own compliance. The last one
        asks the same host question the loud floor line does, through the same injected table, so
        the prompt and the operator's report can never disagree about the machine.
        """
        return build_orchestrator_security_preamble(
            read_isolation_off=self._config.security.read_isolation_off,
            advanced_mode=self._advanced_mode,
            no_write_floor=bool(describe_host_floor(self._config, self._host_floor_checks)),
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
        """Whether decomposition is permitted at all: the task value overrides the global.

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

        The task value wins outright: an explicit per-task ``True``/``False`` is honored
        verbatim; absent (``None``) defers to the instance default ``git.auto_merge``. Auto-merge
        is a publishing-policy choice owned by the operator (the same trusted author as the config),
        not a sandbox/approvals ceiling, so there is no separate operator gate: skipping the human
        PR review is the operator's call to make, not the orchestrator's to police.
        """
        if task.auto_merge is not None:
            return task.auto_merge
        return self._config.git.auto_merge

    def _branch_mode(self, task: NormalizedTask) -> BranchMode:
        """The effective branch mode: the task value wins, else the instance ``repo.branch_mode``.

        Governs where the task's git operations point. A branch is
        orchestrator-owned — and destructive git ops (reset-to-base, force-checkout-away, delete)
        permitted — only in ``new`` mode.
        """
        return task.branch_mode or self._config.repo.branch_mode

    def _persisted_branch_mode(self, task_id: str) -> BranchMode:
        """The task's effective branch mode read from its persisted normalized manifest — the rerun
        path, where the live :class:`NormalizedTask` isn't in hand. Falls back to the instance
        default if the manifest can't be read (a terminal task reliably has one)."""
        try:
            task = load_normalized(self._artifacts_root, task_id)
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            return self._config.repo.branch_mode
        return self._branch_mode(task)

    def _persisted_commit_type(self, task_id: str) -> str | None:
        """The task's ``commit_type`` read from its persisted normalized manifest — the merge path,
        where the live :class:`NormalizedTask` isn't in hand (``merge-task`` runs against a stored
        row long after the run). ``None`` when the manifest can't be read, which
        :func:`task_commit_subject` renders as the default type: a merge subject must never fail to
        exist because a log directory was cleaned."""
        try:
            return load_normalized(self._artifacts_root, task_id).commit_type
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            return None

    def _restart_display_branch(self, task_id: str, mode: BranchMode) -> str | None:
        """Best-effort name of the operator-owned branch a restart-in-place will run on, for the
        confirm / dry-run view. ``current`` → the current git branch; ``existing`` → the task's
        ``branch_ref`` from its persisted manifest. Returns ``None`` if it can't be resolved (the
        caller falls back to the stored row branch)."""
        if mode is BranchMode.CURRENT:
            return self._git.current_branch()
        try:
            task = load_normalized(self._artifacts_root, task_id)
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            return None
        return task.branch_ref

    def _persisted_flow_snapshot(self, task_id: str) -> FlowSnapshot | None:
        """Resolve the task's flow from its persisted manifest — the rerun path.

        Used to validate a ``--from`` node, rebaseline the checkpoint's fingerprint on a compatible
        ``--from``, and (for any ``--continue``) evaluate live fix-loop budgets. Returns ``None`` if
        the manifest can't be read or the flow can't be resolved/validated (callers degrade
        gracefully: a ``--from`` refusal, or simply skipping the budget-exhaustion check)."""
        try:
            task = load_normalized(self._artifacts_root, task_id)
            return self._flow_registry.resolve(task.task_type)
        except (
            json.JSONDecodeError,
            OSError,
            KeyError,
            ValueError,
            FlowResolutionError,
            FlowValidationError,
        ):
            return None

    def _resume_node_refusals(
        self, resume_node: str, *, live: FlowSnapshot | None, is_from: bool
    ) -> list[str]:
        """Validate the resume target against the flow currently on disk (empty list => valid).

        An operator ``rerun --continue`` adopts the live control plane and resumes at
        ``resume_node`` — the explicit ``--from`` override, or the checkpoint's current node. Either
        way the node must still exist in the (possibly edited) live flow; refuse with an actionable
        message when it does not, or when the on-disk flow no longer resolves at all. The
        checkpoint's fingerprint is rebaselined to the live flow by ``_apply_continue_controls``
        once this passes, so the resume lands at ``resume_node`` instead of the engine's resume gate
        routing to manual.
        """
        if live is None:
            return [
                (
                    "could not resolve the task's flow (the on-disk flow may be invalid); "
                    "fix it or use a fresh rerun"
                )
            ]
        if resume_node not in live.nodes_by_id:
            known = ", ".join(sorted(live.nodes_by_id))
            if is_from:
                return [f"--from node '{resume_node}' is not in the current flow (nodes: {known})"]
            return [
                (
                    f"the checkpoint node '{resume_node}' no longer exists in the edited flow "
                    f"(nodes: {known}); pass --from <node> to pick a resume point, or a fresh rerun"
                )
            ]
        return []

    def _control_plane_drifted(
        self, task_id: str, live: FlowSnapshot, *, checkpoint_fingerprint: str | None
    ) -> bool:
        """True when the live control plane differs from the digest frozen at the checkpoint.

        Bundle-level (flow YAML + role prompts + tool executables), so a role/tool-only edit counts
        too — unlike ``flow_fingerprint`` (flow YAML only). Falls back to the flow_fingerprint when
        no control-bundle digest was persisted (defensive; every current task freezes one). Drives
        the ``--continue`` adopt/drift note; a broken/unreadable live input reads as drift.
        """
        persisted = self._store.get_control_bundle_digest(task_id)
        if persisted is None:
            return (
                checkpoint_fingerprint is not None
                and checkpoint_fingerprint != live.flow_fingerprint
            )
        assert live.source_path is not None
        try:
            live_digest = digest_live_control_inputs(
                live, live.source_path.parent, self._tool_registry
            )
        except ControlBundleError:
            return True
        return live_digest != persisted

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
        log.info("%s started", operation_name, extra=safe_fields)
        try:
            result = operation()
        except Exception as exc:
            log.error(
                "%s failed",
                operation_name,
                extra={
                    **safe_fields,
                    "duration_seconds": round(self._monotonic() - started, 3),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        log.info(
            "%s completed",
            operation_name,
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
        governance_changed: tuple[str, ...] = (),
    ) -> None:
        """Best-effort terminal notification. Never raises, never hangs, never alters the
        outcome. The "never hangs" half is the transport's: every notifier call carries its own
        wall-clock deadline, because this runs inside a watch tick and the stop ladder cannot
        reach a call in flight."""
        try:
            self._notifier.send_notification(
                task_id=task_id,
                final_status=final_status.value,
                pr_url=pr_url,
                reason=reason,
                contacts=contacts,
                governance_changed=governance_changed,
                details=self._terminal_details(task_id, final_status),
            )
        except Exception as exc:
            self._log(task_id).warning(
                "terminal notification failed", extra={"error_type": type(exc).__name__}
            )

    def _terminal_details(self, task_id: str, final_status: Status) -> TerminalDetails | None:
        """Assemble the operator-facing enrichment for a needs-attention terminal.

        Returns ``None`` for a clean ``done`` (kept terse) or when the task has no ``tasks`` row (a
        validation reject has none) — the notification degrades to the terse one-line message. The
        stop node, loop, and blocking finding are read from the on-disk ``failure_report.json`` the
        engine / infra terminal already wrote; the rest from the ``TaskRow``. Best-effort: a
        missing or unreadable report simply omits those fields. Never raises (the caller swallows).
        """
        if final_status not in (Status.MANUAL_ACTION_REQUIRED, Status.FAILED):
            return None
        row = self._store.get_task(task_id)
        if row is None:
            return None
        loop: str | None = None
        finding: TerminalFinding | None = None
        report = _read_failure_report(row.failure_report_path)
        if report is not None:
            raw_loop = report.get("loop")
            loop = raw_loop if isinstance(raw_loop, str) and raw_loop != "infra" else None
            finding = _top_blocking_finding(report.get("last_review_findings"))
        # The stop node is the persisted resume checkpoint (``tasks.current_node``), not a TaskRow
        # field — read it via the flow-checkpoint accessor (the row is known to exist here).
        stop_node = self._store.get_flow_checkpoint(task_id)[0]
        return TerminalDetails(
            title=row.title or None,
            branch=row.branch,
            stop_node=stop_node,
            loop=loop,
            fix_rounds=row.fix_iterations,
            finding=finding,
            report_path=_stuck_report_path(row.failure_report_path),
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
                governance_changed=p.governance_changed,
                advanced_mode=self._advanced_mode,
            )
        )
