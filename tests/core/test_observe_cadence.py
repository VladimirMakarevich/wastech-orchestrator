"""The supervisor observation cadence policy: the rank comparison, resolution, and the triggers."""

import pytest

from wastech_orchestrator.config.schema import OBSERVE_TRIGGERS, ObserveMode
from wastech_orchestrator.core.observe_cadence import (
    is_same_or_narrower,
    resolve_mode,
    should_observe,
    triggers_for,
)

# Narrowest first. The order IS the policy: rank = how many LLM calls the mode can produce.
_ORDER = [ObserveMode.NONE, ObserveMode.EVENTS, ObserveMode.SELECTED, ObserveMode.ALL]


# -- the rank comparison (a flow may narrow the global cadence, never widen it) ----------------


def test_every_mode_is_same_or_narrower_than_itself_and_than_any_wider_one() -> None:
    for i, candidate in enumerate(_ORDER):
        for j, reference in enumerate(_ORDER):
            # narrower-or-equal (i <= j) is allowed; strictly wider (i > j) is not.
            assert is_same_or_narrower(candidate.value, reference.value) is (i <= j)


def test_selected_is_not_allowed_under_a_global_events() -> None:
    # The loophole worth naming: `selected` enumerates nodes and is in the limit wider than "only
    # when something went wrong", so it ranks ABOVE `events` and is refused under it — no special
    # case, just where it sits in the table.
    assert is_same_or_narrower(ObserveMode.SELECTED.value, ObserveMode.EVENTS.value) is False
    assert is_same_or_narrower(ObserveMode.EVENTS.value, ObserveMode.SELECTED.value) is True
    assert is_same_or_narrower(ObserveMode.NONE.value, ObserveMode.EVENTS.value) is True


@pytest.mark.parametrize(
    ("candidate", "reference"),
    [("sometimes", "all"), ("none", "sometimes"), ("", "all"), ("None", "all")],
)
def test_unknown_mode_fails_closed(candidate: str, reference: str) -> None:
    # Fail-closed on either side: an unrecognized mode must never be read as permission to widen.
    assert is_same_or_narrower(candidate, reference) is False


# -- resolution (flow wins when it declares one; the validator has proven it is not wider) -----


def test_flow_mode_wins_and_absence_inherits_the_global() -> None:
    assert resolve_mode(ObserveMode.ALL, ObserveMode.NONE) is ObserveMode.NONE
    assert resolve_mode(ObserveMode.ALL, None) is ObserveMode.ALL
    assert resolve_mode(ObserveMode.EVENTS, None) is ObserveMode.EVENTS


# -- trigger detection (all facts already in the post-node hook's hands) ------------------------


def _triggers(**overrides: object) -> frozenset[str]:
    facts: dict = {
        "outcome_kind": "done",
        "rework_exhausted": False,
        "status": "succeeded",
        "fell_back": False,
    }
    facts.update(overrides)
    return triggers_for(**facts)  # type: ignore[arg-type]


def test_an_ordinary_step_has_no_triggers() -> None:
    for kind in ("done", "accept", "pass", "route:approve"):
        assert _triggers(outcome_kind=kind) == frozenset()


def test_rework_fires_on_the_outcome_and_on_an_exhausted_budget() -> None:
    assert _triggers(outcome_kind="rework") == {"rework"}
    # The give-up accept: the kind is `accept`, the deviation is the exhausted budget — which is the
    # case most worth a note, since the stage moved on with findings still open.
    assert _triggers(outcome_kind="accept", rework_exhausted=True) == {"rework"}


def test_failure_is_read_from_the_run_row_not_the_outcome_kind() -> None:
    # An agent node's outcome kind is unconditionally `done` even when its provider result failed on
    # quality, so the row's status is the only place the failure is visible at this point.
    assert _triggers(outcome_kind="done", status="failed") == {"failure"}
    assert _triggers(status="succeeded") == frozenset()
    assert _triggers(status=None) == frozenset()  # a row that could not be read is not a failure


def test_fallback_is_taken_as_a_decided_fact() -> None:
    # Which route columns amount to a fallback is the step record's call (`fell_back_from`), so that
    # the finalize packet and this gate cannot disagree; this policy only names the answer.
    assert _triggers(fell_back=True) == {"fallback"}
    assert _triggers(fell_back=False) == frozenset()


def test_a_step_can_exhibit_several_deviations_at_once() -> None:
    assert _triggers(outcome_kind="rework", status="failed", fell_back=True) == {
        "rework",
        "failure",
        "fallback",
    }


def test_detected_triggers_stay_within_the_closed_set() -> None:
    # The set is closed by decision: a new trigger arrives with the facts that detect it.
    assert _triggers(outcome_kind="rework", status="failed", fell_back=True) <= OBSERVE_TRIGGERS


# -- the per-mode decision ---------------------------------------------------------------------


def _observes(mode: ObserveMode, **overrides: object) -> bool:
    kwargs: dict = {
        "mode": mode,
        "node_id": "review",
        "include_nodes": (),
        "enabled_triggers": sorted(OBSERVE_TRIGGERS),
        "triggers": frozenset(),
    }
    kwargs.update(overrides)
    return should_observe(**kwargs)  # type: ignore[arg-type]


def test_none_never_observes_and_all_always_does() -> None:
    assert _observes(ObserveMode.NONE) is False
    assert _observes(ObserveMode.NONE, triggers=frozenset({"rework"})) is False
    assert _observes(ObserveMode.ALL) is True


def test_selected_observes_exactly_the_listed_nodes() -> None:
    assert _observes(ObserveMode.SELECTED, include_nodes=("review",)) is True
    assert _observes(ObserveMode.SELECTED, include_nodes=("implementation",)) is False
    assert _observes(ObserveMode.SELECTED) is False  # an empty list observes nothing


def test_events_observes_a_deviation_and_only_an_enabled_one() -> None:
    assert _observes(ObserveMode.EVENTS, triggers=frozenset({"rework"})) is True
    assert _observes(ObserveMode.EVENTS) is False
    # An operator can narrow further: with only `failure` enabled, a rework no longer qualifies.
    assert (
        _observes(ObserveMode.EVENTS, triggers=frozenset({"rework"}), enabled_triggers=("failure",))
        is False
    )
    assert (
        _observes(
            ObserveMode.EVENTS, triggers=frozenset({"failure"}), enabled_triggers=("failure",)
        )
        is True
    )
