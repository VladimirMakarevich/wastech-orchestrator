"""Persisted per-task loop counters (spec §8.1).

The counters are stored on the ``tasks`` row and surface fix-loop progress; the ``FlowEngine`` owns
the termination rules now (it charges each ``rework``/``fail`` edge against a generic global counter
plus its named loop / inline budget — the ``min(flow_budget, config_cap)`` ceiling), so this module
is just the data shape:

(``stage_attempts`` is **not** here: it is an inherently per-node quantity owned by the Router
(``StageOutcome.stage_attempts``) and persisted on ``node_runs``, never collapsed to a task-level
integer — audit remediation #17.)

* ``test_fix_cycles`` / ``review_fix_cycles`` — the length of the *current consecutive* fix loop,
  counted separately for the test-driven and review-driven loops; each bounded by
  ``agents.max_fix_cycles``.
* ``fix_iterations`` — a single global per-task counter, incremented on every entry into ``fixing``;
  bounded by ``agents.max_total_fix_iterations`` (the hard stop guaranteeing termination).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wastech_orchestrator.core.flow.run_state import FlowRunState


@dataclass
class LoopCounters:
    """The mutable per-task loop counters persisted on the ``tasks`` row (§9)."""

    test_fix_cycles: int = 0
    review_fix_cycles: int = 0
    fix_iterations: int = 0


def record_rework(run_state: FlowRunState) -> int:
    """The single rework-accounting path: increment the global ``fix_iterations`` once (P2.1).

    Every in-flow rework/fail edge the engine takes — the test-driven loop (``test_fix``) and the
    review-driven loop (``review_fix``) alike — charges its global cost here and **only** here, so a
    rework is counted exactly once and never double-incremented (the failure mode the old
    ``supervise_fix → fixing`` edge risked, now structurally impossible: the supervisor is an
    advisory layer that never reworks). Returns the new global counter value.

    The named-loop / inline-edge budgets stay with the engine's edge bookkeeping; this owns only the
    one global counter. The immutable per-verdict audit lives in the ``evaluations`` table, written
    by the evaluator node — recording a verdict never touches this counter.
    """
    return run_state.bump(run_state.GLOBAL_FIX_KEY)
