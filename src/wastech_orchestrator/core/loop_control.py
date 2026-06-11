"""Loop control for the two fix loops (spec §8.1).

Three persisted counters drive termination deterministically (no supervisor agent in v1):

* ``stage_attempts`` — attempts of a single stage run, including provider fallback within that
  stage; bounded by ``agents.max_stage_attempts``. This counter is owned and enforced by the Router
  (``StageOutcome.stage_attempts``); we only mirror its last value here for persistence.
* ``test_fix_cycles`` / ``review_fix_cycles`` — the length of the *current consecutive* fix loop,
  counted **separately** for the test-driven and review-driven loops; each bounded by
  ``agents.max_fix_cycles``.
* ``fix_iterations`` — a **single global per-task** counter, incremented on **every** entry into
  ``fixing`` regardless of which loop triggered it; bounded by ``agents.max_total_fix_iterations``.
  This is the hard stop guaranteeing termination (no infinite reviewing<->fixing ping-pong).

When the task is decomposed (§5.1), ``stage_attempts`` and both ``fix_cycles`` are scoped to the
active subtask and reset when the Core advances to the next subtask; the global ``fix_iterations``
is **not** reset and accumulates across all subtasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from wastech_orchestrator.config.schema import AgentsConfig


class FixLoop(StrEnum):
    """Which fix loop triggered an entry into ``fixing`` (spec §8.1)."""

    TEST = "test"
    REVIEW = "review"


@dataclass
class LoopCounters:
    """The mutable per-task loop counters persisted on the ``tasks`` row (§9)."""

    stage_attempts: int = 0
    test_fix_cycles: int = 0
    review_fix_cycles: int = 0
    fix_iterations: int = 0


@dataclass(frozen=True)
class LoopDecision:
    """The outcome of entering ``fixing``: whether the task is stuck and which limit bound it."""

    stuck: bool
    loop: FixLoop | None = None
    # The exhausted limit name, for the failure report (§10): one of
    # ``"max_fix_cycles"`` or ``"max_total_fix_iterations"``.
    limit_name: str | None = None


class LoopController:
    """Applies the §8.1 counter rules. Pure given a :class:`LoopCounters` instance it mutates."""

    def __init__(self, limits: AgentsConfig) -> None:
        self._max_fix_cycles = limits.max_fix_cycles
        self._max_total_fix_iterations = limits.max_total_fix_iterations

    def enter_fixing(self, counters: LoopCounters, loop: FixLoop) -> LoopDecision:
        """Record an entry into ``fixing`` and decide whether the task is now stuck.

        Increments the triggering loop's ``fix_cycles`` and the global ``fix_iterations``, then
        checks both caps. The task is stuck as soon as *either* the single fix loop reaches
        ``max_fix_cycles`` *or* the global ``fix_iterations`` reaches ``max_total_fix_iterations``.
        The per-loop cap is reported first when both trip on the same entry.
        """
        counters.fix_iterations += 1
        if loop is FixLoop.TEST:
            counters.test_fix_cycles += 1
            loop_cycles = counters.test_fix_cycles
        else:
            counters.review_fix_cycles += 1
            loop_cycles = counters.review_fix_cycles

        if loop_cycles >= self._max_fix_cycles:
            return LoopDecision(stuck=True, loop=loop, limit_name="max_fix_cycles")
        if counters.fix_iterations >= self._max_total_fix_iterations:
            return LoopDecision(stuck=True, loop=loop, limit_name="max_total_fix_iterations")
        return LoopDecision(stuck=False)

    def on_check_pass(self, counters: LoopCounters) -> None:
        """Tests passed: the test-driven fix loop is resolved, so reset its cycle counter."""
        counters.test_fix_cycles = 0

    def on_review_pass(self, counters: LoopCounters) -> None:
        """Review passed with no blocking findings: reset both fix-loop cycle counters."""
        counters.test_fix_cycles = 0
        counters.review_fix_cycles = 0

    def reset_for_next_subtask(self, counters: LoopCounters) -> None:
        """Advance to the next subtask (§5.1): reset per-subtask counters, keep the global one.

        ``stage_attempts`` and both ``fix_cycles`` reset; ``fix_iterations`` is **not** reset and
        keeps accumulating across all subtasks so a decomposed task cannot evade the hard stop.
        """
        counters.stage_attempts = 0
        counters.test_fix_cycles = 0
        counters.review_fix_cycles = 0
