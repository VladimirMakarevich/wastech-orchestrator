"""Task state machine (spec §8).

The canonical task statuses and the allowed transitions between them. This module is pure: no IO,
no git, no SQLite. The orchestrator asserts a transition here and then persists the new status
atomically (State Store transaction). No status outside §8 is ever introduced — the decomposition
cycle reuses ``implementing -> testing -> reviewing`` per subtask and carries ``active_subtask``
(``k`` of ``n``) in the State Store rather than inventing new statuses.
"""

from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    """The task statuses from spec §8 (canonical names; do not invent new ones).

    ``pending`` is the §8.2 queue waiting-state — a task that has been accepted but does not yet own
    the single processing slot. It is tracked on the ``tasks`` row but is not one of the active
    pipeline statuses below, so it has no entry in :data:`ALLOWED_TRANSITIONS` as a source until it
    is picked up (``pending -> validated`` is not a pipeline edge: a task enters at ``new``).
    """

    NEW = "new"
    VALIDATED = "validated"
    PREPARING = "preparing"
    REFINING = "refining"
    PLANNING = "planning"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    SUMMARIZING = "summarizing"
    READY_TO_PUBLISH = "ready_to_publish"
    COMMITTING = "committing"
    PUSHING = "pushing"
    CREATING_PR = "creating_pr"
    DONE = "done"
    FAILED = "failed"
    MANUAL_ACTION_REQUIRED = "manual_action_required"
    # The §8.2 queue waiting-state: accepted, awaiting the single processing slot.
    PENDING = "pending"


# Terminal statuses: no outgoing transitions, the slot is released after terminal cleanup (§8.3).
TERMINAL: frozenset[Status] = frozenset({Status.DONE, Status.FAILED, Status.MANUAL_ACTION_REQUIRED})

# Statuses in which a task is actively occupying the processing slot (§8.2). ``pending`` and the
# terminal statuses are excluded; ``new`` precedes slot acquisition (the gate runs first).
ACTIVE: frozenset[Status] = frozenset(
    {
        Status.VALIDATED,
        Status.PREPARING,
        Status.REFINING,
        Status.PLANNING,
        Status.IMPLEMENTING,
        Status.TESTING,
        Status.REVIEWING,
        Status.FIXING,
        Status.SUMMARIZING,
        Status.READY_TO_PUBLISH,
        Status.COMMITTING,
        Status.PUSHING,
        Status.CREATING_PR,
    }
)

# The "happy path" edges from §8. Every non-terminal status additionally allows ``-> failed`` and
# ``-> manual_action_required`` (added below), so those are not repeated here.
_BASE_TRANSITIONS: dict[Status, frozenset[Status]] = {
    # new -> validated normally; new -> failed when the §19 gate rejects (quarantined, no branch).
    Status.NEW: frozenset({Status.VALIDATED}),
    Status.VALIDATED: frozenset({Status.PREPARING}),
    # preparing -> planning when refinement is deterministically skipped (§5).
    Status.PREPARING: frozenset({Status.REFINING, Status.PLANNING}),
    Status.REFINING: frozenset({Status.PLANNING}),
    Status.PLANNING: frozenset({Status.IMPLEMENTING}),
    Status.IMPLEMENTING: frozenset({Status.TESTING}),
    # testing: success -> reviewing; failure -> fixing (test-driven fix loop).
    Status.TESTING: frozenset({Status.REVIEWING, Status.FIXING}),
    # reviewing: pass -> summarizing; blocking findings -> fixing; decomposed subtask k<n -> the
    # next subtask's implementing.
    Status.REVIEWING: frozenset({Status.SUMMARIZING, Status.FIXING, Status.IMPLEMENTING}),
    Status.FIXING: frozenset({Status.TESTING}),
    Status.SUMMARIZING: frozenset({Status.READY_TO_PUBLISH}),
    Status.READY_TO_PUBLISH: frozenset({Status.COMMITTING}),
    Status.COMMITTING: frozenset({Status.PUSHING}),
    Status.PUSHING: frozenset({Status.CREATING_PR}),
    Status.CREATING_PR: frozenset({Status.DONE}),
    Status.PENDING: frozenset({Status.VALIDATED, Status.PREPARING}),
}


def _build_transitions() -> dict[Status, frozenset[Status]]:
    """Add the universal ``-> failed`` / ``-> manual_action_required`` edges to active statuses."""
    bailouts = {Status.FAILED, Status.MANUAL_ACTION_REQUIRED}
    table: dict[Status, frozenset[Status]] = {}
    for status, targets in _BASE_TRANSITIONS.items():
        if status in TERMINAL:
            table[status] = targets
        else:
            table[status] = targets | bailouts
    # Terminal statuses have no outgoing transitions.
    for terminal in TERMINAL:
        table[terminal] = frozenset()
    return table


ALLOWED_TRANSITIONS: dict[Status, frozenset[Status]] = _build_transitions()


class InvalidTransition(Exception):
    """Raised when a status transition is not permitted by §8."""

    def __init__(self, src: Status, dst: Status) -> None:
        super().__init__(f"illegal transition: {src.value} -> {dst.value}")
        self.src = src
        self.dst = dst


def can_transition(src: Status, dst: Status) -> bool:
    """Return True iff ``src -> dst`` is an allowed §8 transition."""
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def assert_transition(src: Status, dst: Status) -> None:
    """Raise :class:`InvalidTransition` unless ``src -> dst`` is allowed by §8."""
    if not can_transition(src, dst):
        raise InvalidTransition(src, dst)


def is_terminal(status: Status) -> bool:
    """Return True iff ``status`` is terminal (done / failed / manual_action_required)."""
    return status in TERMINAL


def is_active(status: Status) -> bool:
    """Return True iff ``status`` means the task currently owns the processing slot (§8.2)."""
    return status in ACTIVE
