"""Exhaustion disposition table: which class set parks, which fails closed to manual, which
goes terminal — and that the security precedence outranks everything, including an operator stop."""

from __future__ import annotations

import pytest

from wastech_orchestrator.core.flow.nodes.base import NodeInfraError
from wastech_orchestrator.core.infra_disposition import (
    InfraDisposition,
    classify_exhaustion,
)
from wastech_orchestrator.providers.base import PARK_ELIGIBLE, ErrorClass

# The two fail-closed security classes, iterated from the module under test so a class added to the
# set extends this table automatically instead of silently escaping it.
_MANUAL_CLASSES = sorted(
    {ErrorClass.CONTAINMENT_UNVERIFIED, ErrorClass.CAPABILITY_UNAVAILABLE}, key=lambda e: e.value
)


@pytest.mark.parametrize("security_class", _MANUAL_CLASSES)
@pytest.mark.parametrize(
    "classes_for,representative_for",
    [
        # Alone: the plain case.
        (lambda c: (c,), lambda c: c),
        # Behind a rate limit, and ahead of one: a park-eligible sibling must not win in either
        # order, because a set has no order and the predicate must not depend on one.
        (lambda c: (ErrorClass.RATE_LIMITED, c), lambda c: c),
        (lambda c: (c, ErrorClass.RATE_LIMITED), lambda _c: ErrorClass.RATE_LIMITED),
        # With a stop as the representative: an operator stop cancels an agent, it does not prove
        # the process tree empty, so security still wins.
        (lambda c: (c,), lambda _c: ErrorClass.CANCELLED),
    ],
)
def test_containment_class_anywhere_is_always_manual(
    security_class: ErrorClass, classes_for, representative_for
) -> None:
    assert (
        classify_exhaustion(
            classes_for(security_class), representative=representative_for(security_class)
        )
        is InfraDisposition.MANUAL
    )


@pytest.mark.parametrize("park_class", sorted(PARK_ELIGIBLE, key=lambda e: e.value))
@pytest.mark.parametrize("order", ["primary", "fallback"])
def test_park_eligible_class_parks_whatever_the_other_attempt_reported(
    park_class: ErrorClass, order: str
) -> None:
    # The incident's shape: one attempt reports something resumable, the other fails on a class that
    # is not. The representative is always the LAST attempt's class, which is what used to decide.
    other = ErrorClass.AUTHENTICATION_FAILED
    classes = (park_class, other) if order == "primary" else (other, park_class)
    assert classify_exhaustion(classes, representative=classes[-1]) is InfraDisposition.PARK


@pytest.mark.parametrize(
    "error_class",
    [
        ErrorClass.AUTHENTICATION_FAILED,
        ErrorClass.AUTHORIZATION_FAILED,
        ErrorClass.TIMEOUT,
        ErrorClass.PROCESS_CRASHED,
        ErrorClass.INVALID_OUTPUT,
        ErrorClass.BINARY_NOT_FOUND,
        ErrorClass.UNSUPPORTED_VERSION,
        ErrorClass.SESSION_UNAVAILABLE,
        ErrorClass.CONFIGURATION_ERROR,
        # Deliberately fallback-eligible but not park-eligible: a possibly-permanent no-work must
        # not hold the single processing slot for a whole park window on its own.
        ErrorClass.AGENT_NO_PROGRESS,
    ],
)
def test_non_park_classes_alone_are_terminal(error_class: ErrorClass) -> None:
    assert (
        classify_exhaustion((error_class,), representative=error_class) is InfraDisposition.TERMINAL
    )


@pytest.mark.parametrize(
    "classes,representative,expected",
    [
        # The incident: a rate-limited primary whose fallback died on expired credentials. The
        # representative is the masking class, and the task must still park.
        (
            (ErrorClass.RATE_LIMITED, ErrorClass.AUTHENTICATION_FAILED),
            ErrorClass.AUTHENTICATION_FAILED,
            InfraDisposition.PARK,
        ),
        # A stop as the Router actually shapes it: the killed attempt's row carries the crash class
        # while the representative is synthesized. Reads as stopped, not as waiting.
        ((ErrorClass.PROCESS_CRASHED,), ErrorClass.CANCELLED, InfraDisposition.PARK),
        # A stop that landed on a rate-limited stage is still a stop.
        ((ErrorClass.RATE_LIMITED,), ErrorClass.CANCELLED, InfraDisposition.PARK),
        # Security outranks the stop branch.
        ((ErrorClass.CONTAINMENT_UNVERIFIED,), ErrorClass.CANCELLED, InfraDisposition.MANUAL),
        # A missing isolation capability is not made shippable by a transient on the other provider.
        (
            (ErrorClass.CAPABILITY_UNAVAILABLE, ErrorClass.RATE_LIMITED),
            ErrorClass.RATE_LIMITED,
            InfraDisposition.MANUAL,
        ),
        # A no-work fallback does not cancel out a real limit on the primary.
        (
            (ErrorClass.RATE_LIMITED, ErrorClass.AGENT_NO_PROGRESS),
            ErrorClass.AGENT_NO_PROGRESS,
            InfraDisposition.PARK,
        ),
        # A same-provider retry run that drifted class: the first attempt's limit still decides.
        (
            (ErrorClass.RATE_LIMITED, ErrorClass.NETWORK_UNAVAILABLE),
            ErrorClass.NETWORK_UNAVAILABLE,
            InfraDisposition.PARK,
        ),
        # Nothing was raised at all — no evidence to justify holding the slot.
        ((), None, InfraDisposition.TERMINAL),
        # The set decides, so a caller that supplies a representative but no set fails closed rather
        # than parking on a class no attempt is recorded as having raised.
        ((), ErrorClass.RATE_LIMITED, InfraDisposition.TERMINAL),
    ],
)
def test_precedence_table(
    classes: tuple[ErrorClass, ...],
    representative: ErrorClass | None,
    expected: InfraDisposition,
) -> None:
    assert classify_exhaustion(classes, representative=representative) is expected


# --- the exception's derivation is half of the same contract ------------------------------------


def test_single_class_derives_the_class_set() -> None:
    # Every raise site that legitimately knows exactly one class passes only the representative, so
    # the derivation is what keeps those sites deciding on the class they have.
    exc = NodeInfraError("x", error_class=ErrorClass.RATE_LIMITED)
    assert exc.error_classes == (ErrorClass.RATE_LIMITED,)
    assert classify_exhaustion(exc.error_classes, representative=exc.error_class) is (
        InfraDisposition.PARK
    )


def test_no_class_at_all_derives_an_empty_set() -> None:
    exc = NodeInfraError("x")
    assert exc.error_class is None
    assert exc.error_classes == ()


def test_explicit_class_set_wins_over_the_representative() -> None:
    exc = NodeInfraError(
        "x",
        error_class=ErrorClass.AUTHENTICATION_FAILED,
        error_classes=(ErrorClass.RATE_LIMITED, ErrorClass.AUTHENTICATION_FAILED),
    )
    assert exc.error_classes == (ErrorClass.RATE_LIMITED, ErrorClass.AUTHENTICATION_FAILED)
    assert exc.error_class is ErrorClass.AUTHENTICATION_FAILED
