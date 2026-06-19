"""Unit tests for the §8 state machine (generic lifecycle)."""

from __future__ import annotations

import pytest

from wastech_orchestrator.core.state_machine import (
    ACTIVE,
    ALLOWED_TRANSITIONS,
    TERMINAL,
    InvalidTransition,
    Status,
    assert_transition,
    can_transition,
    is_active,
    is_terminal,
)


def test_status_set_is_the_generic_lifecycle() -> None:
    # The generic lifecycle: new -> validated -> preparing -> running -> terminal, plus the §8.2
    # ``pending`` queue waiting-state. Progress within ``running`` is the flow ``current_node``.
    assert {s.value for s in Status} == {
        "new",
        "validated",
        "preparing",
        "running",
        "done",
        "failed",
        "manual_action_required",
        "pending",
    }


def test_terminal_statuses() -> None:
    assert {Status.DONE, Status.FAILED, Status.MANUAL_ACTION_REQUIRED} == TERMINAL
    for status in TERMINAL:
        assert is_terminal(status)
        assert ALLOWED_TRANSITIONS[status] == frozenset()


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (Status.NEW, Status.VALIDATED),
        (Status.VALIDATED, Status.PREPARING),
        (Status.PREPARING, Status.RUNNING),
        (Status.RUNNING, Status.DONE),
        (Status.PENDING, Status.VALIDATED),
        (Status.PENDING, Status.PREPARING),
    ],
)
def test_allowed_happy_path_transitions(src: Status, dst: Status) -> None:
    assert can_transition(src, dst)
    assert_transition(src, dst)  # does not raise


def test_new_can_fail_at_validation_gate() -> None:
    # A §19 reject is terminal ``failed`` straight from ``new`` (no branch).
    assert can_transition(Status.NEW, Status.FAILED)


@pytest.mark.parametrize("status", sorted(ACTIVE, key=lambda s: s.value))
def test_every_active_status_can_bail_out(status: Status) -> None:
    assert can_transition(status, Status.FAILED)
    assert can_transition(status, Status.MANUAL_ACTION_REQUIRED)
    assert is_active(status)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (Status.NEW, Status.RUNNING),  # cannot skip validate/prepare
        (Status.VALIDATED, Status.RUNNING),  # must go through preparing
        (Status.PREPARING, Status.DONE),  # only the engine reaches done (from running)
        (Status.DONE, Status.RUNNING),  # terminal has no outgoing edges
        (Status.FAILED, Status.VALIDATED),
    ],
)
def test_disallowed_transitions_raise(src: Status, dst: Status) -> None:
    assert not can_transition(src, dst)
    with pytest.raises(InvalidTransition):
        assert_transition(src, dst)


def test_invalid_transition_carries_src_and_dst() -> None:
    with pytest.raises(InvalidTransition) as exc:
        assert_transition(Status.DONE, Status.NEW)
    assert exc.value.src is Status.DONE
    assert exc.value.dst is Status.NEW


def test_terminal_and_active_are_disjoint() -> None:
    assert TERMINAL.isdisjoint(ACTIVE)
    # ``new`` and ``pending`` precede slot ownership, so they are neither terminal nor active.
    assert not is_active(Status.NEW)
    assert not is_active(Status.PENDING)
    assert not is_terminal(Status.NEW)
