"""Task state machine.

The canonical task statuses and the allowed transitions between them. This module is pure: no IO,
no git, no SQLite. The orchestrator asserts a transition here and then persists the new status
atomically (State Store transaction).

The lifecycle is generic: ``new -> validated -> preparing -> running -> (done | failed |
manual_action_required)``. Progress *within* ``running`` — which flow node is executing, and the
per-subtask loop of a decomposed task — is the ``current_node`` in ``node_runs`` and the
``active_subtask`` counter, not a status. The orchestrator no longer carries per-stage statuses.
"""

from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    """The task statuses from spec (canonical names; do not invent new ones).

    ``pending`` is the queue waiting-state — a task that has been accepted but does not yet own
    the single processing slot. ``running`` is the single in-flight status: the FlowEngine drives
    the graph and progress is the ``current_node`` in ``node_runs``, not a granular status.
    """

    NEW = "new"
    VALIDATED = "validated"
    PREPARING = "preparing"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    MANUAL_ACTION_REQUIRED = "manual_action_required"
    # The queue waiting-state: accepted, awaiting the single processing slot.
    PENDING = "pending"


# Terminal statuses: no outgoing transitions, the slot is released after terminal cleanup.
TERMINAL: frozenset[Status] = frozenset({Status.DONE, Status.FAILED, Status.MANUAL_ACTION_REQUIRED})

# Statuses in which a task is actively occupying the processing slot. ``pending`` and the
# terminal statuses are excluded; ``new`` precedes slot acquisition (the gate runs first).
ACTIVE: frozenset[Status] = frozenset({Status.VALIDATED, Status.PREPARING, Status.RUNNING})

# The "happy path" edges . Every non-terminal status additionally allows ``-> failed`` and
# ``-> manual_action_required`` (added below), so those are not repeated here.
_BASE_TRANSITIONS: dict[Status, frozenset[Status]] = {
    # new -> validated normally; new -> failed when the gate rejects (quarantined, no branch).
    Status.NEW: frozenset({Status.VALIDATED}),
    Status.VALIDATED: frozenset({Status.PREPARING}),
    # preparing -> running when the orchestrator hands the validated flow graph to the engine
    # (isolation + check preflight + branch prep happen in preparing, in the orchestrator wrapper).
    Status.PREPARING: frozenset({Status.RUNNING}),
    # running -> done when the flow graph reaches its terminal node (the engine drives the rest).
    Status.RUNNING: frozenset({Status.DONE}),
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
    """Raised when a status transition is not permitted ."""

    def __init__(self, src: Status, dst: Status) -> None:
        super().__init__(f"illegal transition: {src.value} -> {dst.value}")
        self.src = src
        self.dst = dst


def can_transition(src: Status, dst: Status) -> bool:
    """Return True iff ``src -> dst`` is an allowed transition."""
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


def assert_transition(src: Status, dst: Status) -> None:
    """Raise :class:`InvalidTransition` unless ``src -> dst`` is allowed ."""
    if not can_transition(src, dst):
        raise InvalidTransition(src, dst)


def is_terminal(status: Status) -> bool:
    """Return True iff ``status`` is terminal (done / failed / manual_action_required)."""
    return status in TERMINAL


def is_active(status: Status) -> bool:
    """Return True iff ``status`` means the task currently owns the processing slot."""
    return status in ACTIVE
