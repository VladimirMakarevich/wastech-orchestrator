"""Persisted per-task loop counters (spec §8.1).

The counters are stored on the ``tasks`` row and surface fix-loop progress; the ``FlowEngine`` owns
the termination rules now (it charges each ``rework``/``fail`` edge against a generic global counter
plus its named loop / inline budget — the ``min(flow_budget, config_cap)`` ceiling), so this module
is just the data shape:

* ``stage_attempts`` — attempts of a single stage run, including provider fallback within that
  stage; bounded by ``agents.max_stage_attempts``. Owned and enforced by the Router
  (``StageOutcome.stage_attempts``); persisted here.
* ``test_fix_cycles`` / ``review_fix_cycles`` — the length of the *current consecutive* fix loop,
  counted separately for the test-driven and review-driven loops; each bounded by
  ``agents.max_fix_cycles``.
* ``fix_iterations`` — a single global per-task counter, incremented on every entry into ``fixing``;
  bounded by ``agents.max_total_fix_iterations`` (the hard stop guaranteeing termination).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LoopCounters:
    """The mutable per-task loop counters persisted on the ``tasks`` row (§9)."""

    stage_attempts: int = 0
    test_fix_cycles: int = 0
    review_fix_cycles: int = 0
    fix_iterations: int = 0
