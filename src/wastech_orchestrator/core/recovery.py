"""Restart recovery + idempotency reconciliation (spec §13).

On startup the orchestrator reconciles SQLite ↔ working branch ↔ artifacts to find the single
unfinished operation and resume it idempotently. This module computes the **decision** (a
:class:`RecoveryPlan`); the orchestrator carries it out (re-running only the unfinished work, with
commit/push/PR idempotency enforced by the Git Manager's fingerprints).

Rules (§13, §8.2):

* exactly one task may be active; **more than one → ``manual_action_required``** (ambiguous);
* a decomposed subtask is "done" only when its ``commit_sha`` is set **and** that commit is on the
  branch — a recorded SHA missing from the branch, or more committed SHAs than
  ``subtasks_completed``, is inconsistent → ``manual_action_required`` (never re-commit a SHA);
* a terminal task whose cleanup never completed → perform the base-branch checkout once when safe;
* anything ambiguous → ``manual_action_required``; never auto-republish an unknown commit, delete
  partial changes, or continue past a detected inconsistency.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.git_manager import GitManager
from wastech_orchestrator.state_store import StateStore, TaskRow


class RecoveryAction(StrEnum):
    """What the orchestrator should do after reconciliation."""

    NONE = "none"  # no active task — the slot is free (watch may pick a pending task)
    RESUME = "resume"  # one active task — resume it idempotently
    CLEANUP = "cleanup"  # a terminal task whose cleanup was interrupted
    MANUAL = "manual"  # an ambiguous/inconsistent state — stop for manual action


@dataclass(frozen=True)
class RecoveryPlan:
    """The reconciliation decision (§13)."""

    action: RecoveryAction
    task_id: str | None = None
    resume_subtask: int | None = None
    manual_reason: str | None = None
    manual_task_ids: tuple[str, ...] = ()


class RecoveryReconciler:
    """Computes the :class:`RecoveryPlan` from the persisted state on startup (§13)."""

    def __init__(self, config: OrchestratorConfig, store: StateStore, git: GitManager) -> None:
        self._config = config
        self._store = store
        self._git = git

    def reconcile(self) -> RecoveryPlan:
        active = self._store.find_active_tasks()
        if len(active) > 1:
            return RecoveryPlan(
                action=RecoveryAction.MANUAL,
                manual_reason="more than one task is active (§8.2)",
                manual_task_ids=tuple(t.task_id for t in active),
            )
        if len(active) == 1:
            task = active[0]
            if task.subtask_count and task.decomposition_accepted:
                return self.reconcile_decomposed(task)
            return RecoveryPlan(action=RecoveryAction.RESUME, task_id=task.task_id)

        uncleaned = self._store.find_incomplete_cleanup()
        if uncleaned:
            return RecoveryPlan(action=RecoveryAction.CLEANUP, task_id=uncleaned[0].task_id)
        return RecoveryPlan(action=RecoveryAction.NONE)

    def reconcile_decomposed(self, task: TaskRow) -> RecoveryPlan:
        """Verify each recorded subtask commit against the branch; find the resume point (§5.1)."""
        branch = task.branch or ""
        subtasks = self._store.get_subtasks(task.task_id)
        committed = 0
        for sub in subtasks:
            if sub.commit_sha:
                if branch and not self._git.commit_on_branch(sub.commit_sha, branch):
                    return RecoveryPlan(
                        action=RecoveryAction.MANUAL,
                        task_id=task.task_id,
                        manual_reason=(
                            f"subtask {sub.order} records commit {sub.commit_sha} "
                            f"absent from {branch} (inconsistent)"
                        ),
                        manual_task_ids=(task.task_id,),
                    )
                committed += 1
        if committed > task.subtasks_completed:
            return RecoveryPlan(
                action=RecoveryAction.MANUAL,
                task_id=task.task_id,
                manual_reason=(
                    f"{committed} subtasks committed but only {task.subtasks_completed} recorded"
                ),
                manual_task_ids=(task.task_id,),
            )
        # The first subtask without a verified commit is the one to re-run (§5.1, §7.4).
        resume_subtask = committed + 1
        return RecoveryPlan(
            action=RecoveryAction.RESUME, task_id=task.task_id, resume_subtask=resume_subtask
        )
