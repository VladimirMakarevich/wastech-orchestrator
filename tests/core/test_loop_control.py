"""Unit tests for §8.1 loop control."""

from __future__ import annotations

from wastech_orchestrator.config.schema import AgentsConfig, DecompositionConfig
from wastech_orchestrator.core.loop_control import (
    FixLoop,
    LoopController,
    LoopCounters,
)
from wastech_orchestrator.providers.base import ProviderId


def _agents(*, max_fix_cycles: int, max_total: int) -> AgentsConfig:
    """A minimal AgentsConfig carrying just the loop limits the controller reads."""
    return AgentsConfig(
        allowed=(ProviderId.CLAUDE, ProviderId.CODEX),
        max_stage_attempts=3,
        max_fix_cycles=max_fix_cycles,
        max_total_fix_iterations=max_total,
        decomposition=DecompositionConfig(
            enabled=False, max_subtasks=8, min_size_signal="large", commit_per_subtask=True
        ),
        routing={},
        providers={},
    )


def test_single_fixing_entry_is_not_stuck() -> None:
    ctrl = LoopController(_agents(max_fix_cycles=3, max_total=5))
    counters = LoopCounters()
    decision = ctrl.enter_fixing(counters, FixLoop.TEST)
    assert decision.stuck is False
    assert counters.test_fix_cycles == 1
    assert counters.fix_iterations == 1
    assert counters.review_fix_cycles == 0


def test_test_loop_hits_max_fix_cycles() -> None:
    ctrl = LoopController(_agents(max_fix_cycles=3, max_total=99))
    counters = LoopCounters()
    assert ctrl.enter_fixing(counters, FixLoop.TEST).stuck is False
    assert ctrl.enter_fixing(counters, FixLoop.TEST).stuck is False
    decision = ctrl.enter_fixing(counters, FixLoop.TEST)
    assert decision.stuck is True
    assert decision.loop is FixLoop.TEST
    assert decision.limit_name == "max_fix_cycles"


def test_review_loop_hits_max_fix_cycles_independently() -> None:
    ctrl = LoopController(_agents(max_fix_cycles=2, max_total=99))
    counters = LoopCounters()
    # Two test-loop entries do not advance the review loop.
    ctrl.enter_fixing(counters, FixLoop.TEST)
    decision = ctrl.enter_fixing(counters, FixLoop.REVIEW)
    assert decision.stuck is False
    assert counters.test_fix_cycles == 1
    assert counters.review_fix_cycles == 1
    decision = ctrl.enter_fixing(counters, FixLoop.REVIEW)
    assert decision.stuck is True
    assert decision.loop is FixLoop.REVIEW
    assert decision.limit_name == "max_fix_cycles"


def test_global_cap_is_the_hard_stop() -> None:
    # Large per-loop budget but a small global cap: alternating loops still terminate.
    ctrl = LoopController(_agents(max_fix_cycles=99, max_total=3))
    counters = LoopCounters()
    assert ctrl.enter_fixing(counters, FixLoop.TEST).stuck is False
    assert ctrl.enter_fixing(counters, FixLoop.REVIEW).stuck is False
    decision = ctrl.enter_fixing(counters, FixLoop.TEST)
    assert decision.stuck is True
    assert decision.limit_name == "max_total_fix_iterations"
    assert counters.fix_iterations == 3


def test_per_loop_cap_reported_before_global_when_both_trip() -> None:
    ctrl = LoopController(_agents(max_fix_cycles=1, max_total=1))
    counters = LoopCounters()
    decision = ctrl.enter_fixing(counters, FixLoop.TEST)
    assert decision.stuck is True
    assert decision.limit_name == "max_fix_cycles"


def test_on_check_pass_resets_only_test_loop() -> None:
    ctrl = LoopController(_agents(max_fix_cycles=3, max_total=99))
    counters = LoopCounters(test_fix_cycles=2, review_fix_cycles=1, fix_iterations=3)
    ctrl.on_check_pass(counters)
    assert counters.test_fix_cycles == 0
    assert counters.review_fix_cycles == 1
    assert counters.fix_iterations == 3


def test_on_review_pass_resets_both_fix_loops() -> None:
    ctrl = LoopController(_agents(max_fix_cycles=3, max_total=99))
    counters = LoopCounters(test_fix_cycles=2, review_fix_cycles=2, fix_iterations=4)
    ctrl.on_review_pass(counters)
    assert counters.test_fix_cycles == 0
    assert counters.review_fix_cycles == 0
    assert counters.fix_iterations == 4


def test_reset_for_next_subtask_keeps_global_iterations() -> None:
    ctrl = LoopController(_agents(max_fix_cycles=3, max_total=99))
    counters = LoopCounters(
        stage_attempts=2, test_fix_cycles=2, review_fix_cycles=1, fix_iterations=4
    )
    ctrl.reset_for_next_subtask(counters)
    assert counters.stage_attempts == 0
    assert counters.test_fix_cycles == 0
    assert counters.review_fix_cycles == 0
    # The global counter accumulates across subtasks (§8.1).
    assert counters.fix_iterations == 4


def test_global_cap_spans_subtasks() -> None:
    ctrl = LoopController(_agents(max_fix_cycles=99, max_total=2))
    counters = LoopCounters()
    assert ctrl.enter_fixing(counters, FixLoop.TEST).stuck is False
    ctrl.reset_for_next_subtask(counters)  # advance to the next subtask
    # The very next fixing entry on the new subtask trips the global cap.
    decision = ctrl.enter_fixing(counters, FixLoop.TEST)
    assert decision.stuck is True
    assert decision.limit_name == "max_total_fix_iterations"
