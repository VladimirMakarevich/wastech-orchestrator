"""Persistence adapter for the flow engine (P1.2).

:class:`StateStoreRunRecorder` implements the engine's
:class:`~wastech_orchestrator.core.flow.engine.RunRecorder` seam against the SQLite state store and
the ledger: it records skipped nodes in ``node_runs``, checkpoints the
:class:`~wastech_orchestrator.core.flow.run_state.FlowRunState` on the ``tasks`` row after each
transition, and writes the flow-neutral failure report when a budget is exhausted.

:func:`hydrate_run_state` rebuilds the checkpoint on resume **from the persisted snapshot
fingerprint** — recovery trusts the stored flow and never re-resolves it from the live config
(``index.md`` §6, ``flow-contract.md`` §9). Side-effect idempotency lives in ``publish_operations``
(unchanged), so a resumed run never duplicates a commit/push/PR.
"""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import FlowNode
from wastech_orchestrator.ledger import DecomposedFailureInfo, write_failure_report
from wastech_orchestrator.state_store import StateStore


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
            last_review_findings=None,
            final_diff="",
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
