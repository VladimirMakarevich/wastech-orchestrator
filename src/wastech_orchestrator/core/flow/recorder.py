"""What a flow run durably recorded: the engine's persistence adapter and the step record.

:class:`StateStoreRunRecorder` implements the engine's
:class:`~wastech_orchestrator.core.flow.engine.RunRecorder` seam against the SQLite state store and
the ledger: it records skipped nodes in ``node_runs``, checkpoints the
:class:`~wastech_orchestrator.core.flow.run_state.FlowRunState` on the ``tasks`` row after each
transition, and writes the flow-neutral failure report when a budget is exhausted.

:class:`StepFacts` is the read side of the same subject: the deterministic, LLM-free record of what
each executed node did. It lives here, next to the writer, so there is exactly one place that states
a fact about a node run — the finalize packet and the observation cadence both read it rather than
each deriving its own answer from the raw columns.

:func:`hydrate_run_state` rebuilds the checkpoint on resume **from the persisted snapshot
fingerprint** — recovery trusts the stored flow and never re-resolves it from the live config.
Side-effect idempotency lives in ``publish_operations``
(unchanged), so a resumed run never duplicates a commit/push/PR.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import FlowNode
from wastech_orchestrator.ledger import DecomposedFailureInfo, write_failure_report
from wastech_orchestrator.providers.artifacts import node_run_dir, task_artifact_dir
from wastech_orchestrator.state_store import NodeRunRow, StateStore


def read_last_findings(store: StateStore, task_id: str) -> list[Any] | None:
    """The most recent in-flow evaluator verdict's findings for the failure report.

    Returns ``None`` when the run recorded no in-flow verdict (so the report reads "(none)" only
    when that is the truth) or the stored JSON is unusable.
    """
    verdicts = [e for e in store.get_evaluations(task_id) if e.kind == "in_flow_verdict"]
    if not verdicts:
        return None
    try:
        findings = json.loads(verdicts[-1].findings_json)
    except json.JSONDecodeError:
        return None
    return findings if isinstance(findings, list) else None


def read_final_diff(artifacts_root: str | Path, task_id: str) -> str:
    """The task's working-tree diff artifact (already written + redacted) for the report.

    Reads back ``<task-dir>/current.diff`` written by ``GitManager.write_current_diff`` during the
    agent nodes; ``""`` when absent (e.g. no edit node ran) — an honest empty, not a masked one.
    """
    try:
        return (task_artifact_dir(artifacts_root, task_id) / "current.diff").read_text(
            encoding="utf-8"
        )
    except OSError:
        return ""


@dataclass(frozen=True)
class StepFacts:
    """What one executed flow node did, as durable fact — no LLM, no live inputs.

    Assembled from the node's own ``node_runs`` row plus the closing message it wrote, never from an
    observation: the record must be complete on a run that spent no observation turns, and identical
    after a revive that re-executed nothing.

    Every field is required, none defaulted. A defaulted field silently becomes ``None`` when a new
    construction site forgets it, and a missing fact is invisible in every consumer — a required
    argument is the only thing that makes the type checker say so instead.

    ``message`` is the node's closing text verbatim. Bounding it is the job of whoever renders it
    into a size-limited surface, not of the fact: a truncated record would make the truncation
    permanent for every later reader.
    """

    node_id: str
    node_kind: str
    status: str | None
    outcome: str | None
    stage_attempts: int
    subtask_order: int | None
    provider_used: str | None
    fallback_from: str | None
    error_class: str | None
    skipped: bool
    skip_reason: str | None
    started_at: str | None
    finished_at: str | None
    message: str | None


def fell_back_from(row: NodeRunRow) -> str | None:
    """The primary provider this run fell back from, or ``None`` when it ran on its primary.

    The single derivation of that fact from a run row. Both the step record and the observation
    cadence's deviation trigger ask it here, so "did this step land somewhere other than where it
    was routed" cannot come out differently in the two places that care. Guard order matters: a
    non-agent node kind leaves both route columns NULL, which is not a fallback.
    """
    if row.provider_used and row.route_primary and row.provider_used != row.route_primary:
        return row.route_primary
    return None


def step_facts(row: NodeRunRow, message: str | None) -> StepFacts:
    """One run row plus its closing message as the run's :class:`StepFacts`."""
    return StepFacts(
        node_id=row.node_id,
        node_kind=row.node_kind,
        status=row.status,
        outcome=row.outcome,
        stage_attempts=row.stage_attempts,
        subtask_order=row.subtask_order,
        provider_used=row.provider_used,
        fallback_from=fell_back_from(row),
        error_class=row.error_class,
        skipped=bool(row.skipped),
        skip_reason=row.skip_reason,
        started_at=row.started_at,
        finished_at=row.finished_at,
        message=message,
    )


def collect_step_facts(
    node_runs: Sequence[NodeRunRow], artifacts_root: str | Path, task_id: str
) -> tuple[StepFacts, ...]:
    """The task's step record: one :class:`StepFacts` per run, in the order they executed.

    Takes the rows rather than a store handle, so the caller keeps ownership of the query — the
    supervisor layer reads them through its own narrow store port, which is not the concrete store
    this module's other helpers take.
    """
    return tuple(step_facts(row, _step_message(artifacts_root, task_id, row)) for row in node_runs)


def _step_message(artifacts_root: str | Path, task_id: str, row: NodeRunRow) -> str | None:
    """A run's closing message from the ``<node_id>.out.md`` the orchestrator wrote for it.

    ``None`` when the run wrote none — a slot node whose product is an artifact, a deterministic
    ``tool`` / ``checks`` node, a run that produced nothing. ``id`` is the run id the artifact
    directory is named after, so a row that has none has no readable output either; without that
    guard the directory name cannot even be formatted.
    """
    if row.id is None:
        return None
    path = node_run_dir(artifacts_root, task_id, row.node_id, row.id) / f"{row.node_id}.out.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


class StateStoreRunRecorder:
    """Back the engine's ``RunRecorder`` protocol with the state store + ledger."""

    def __init__(self, store: StateStore, task_id: str, *, artifacts_root: str | Path) -> None:
        self._store = store
        self._task_id = task_id
        self._artifacts_root = artifacts_root

    def record_skip(self, node: FlowNode, *, reason: str, subtask_order: int | None) -> None:
        self._store.record_node_skip(
            self._task_id, node.id, node.kind, reason=reason, subtask_order=subtask_order
        )

    def save_checkpoint(self, run_state: FlowRunState) -> None:
        self._store.save_flow_checkpoint(
            self._task_id,
            current_node=run_state.current_node,
            counters_json=json.dumps(run_state.loop_counters, sort_keys=True),
            flow_fingerprint=run_state.flow_fingerprint,
            fix_iterations=run_state.fix_iterations,
        )

    def write_failure_report(
        self,
        *,
        node_id: str,
        loop: str | None,
        limit_name: str,
        run_state: FlowRunState,
        subtask_order: int | None = None,
    ) -> str:
        report_path, _stuck_path = write_failure_report(
            self._artifacts_root,
            self._task_id,
            loop=loop or "unknown",
            limit_name=limit_name,
            counters=dict(run_state.loop_counters),
            last_check_log=None,
            last_review_findings=read_last_findings(self._store, self._task_id),
            final_diff=read_final_diff(self._artifacts_root, self._task_id),
            decomposed=self._decomposed_failure(subtask_order),
            node_id=node_id,
        )
        self._store.update_task(self._task_id, failure_report_path=report_path)
        return report_path

    def _decomposed_failure(self, subtask_order: int | None) -> DecomposedFailureInfo | None:
        """Build the decomposed-failure section when the stuck run was inside a subtask region."""
        if subtask_order is None:
            return None
        subtasks = self._store.get_subtasks(self._task_id)
        committed = tuple(s.commit_sha for s in subtasks if s.commit_sha)
        return DecomposedFailureInfo(
            subtask_count=len(subtasks),
            subtasks_completed=len(committed),
            failing_subtask=subtask_order,
            committed_shas=committed,
        )


def hydrate_run_state(store: StateStore, task_id: str) -> FlowRunState | None:
    """Rebuild the :class:`FlowRunState` checkpoint for a resumed task, or ``None`` if absent.

    Returns ``None`` when the flow-engine path never ran for this task (no persisted fingerprint).
    ``completed_nodes`` is the execution trace from ``node_runs``; ``loop_counters`` and
    ``current_node`` come from the ``tasks`` checkpoint columns. The stored fingerprint is taken
    as-is — recovery does not re-resolve the flow from config.
    """
    current_node, counters_json, fingerprint = store.get_flow_checkpoint(task_id)
    if fingerprint is None:
        return None
    counters: dict[str, int] = json.loads(counters_json) if counters_json else {}
    completed = [run.node_id for run in store.get_node_runs(task_id)]
    return FlowRunState(
        flow_fingerprint=fingerprint,
        current_node=current_node,
        completed_nodes=completed,
        loop_counters=counters,
    )
