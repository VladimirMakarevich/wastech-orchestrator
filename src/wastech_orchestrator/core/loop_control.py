"""Persisted per-task loop counters.

The counters are stored on the ``tasks`` row and surface fix-loop progress; the ``FlowEngine`` owns
the termination rules now (it charges each ``rework``/``fail`` edge against a generic global counter
plus its named loop / inline budget — the ``min(flow_budget, config_cap)`` ceiling), so this module
is just the data shape:

(``stage_attempts`` is **not** here: it is an inherently per-node quantity owned by the Router
(``StageOutcome.stage_attempts``) and persisted on ``node_runs``, never collapsed to a task-level
integer — audit remediation #17.)

* ``test_fix_cycles`` / ``review_fix_cycles`` — the length of the *current consecutive* fix loop,
  counted separately for the test-driven and review-driven loops; each bounded by
  ``agents.max_fix_cycles``. They reset to 0 when the loop converges (a forward edge is taken), so
  a task that *succeeded* after N reworks persists 0 here — a live figure, not an audit total.
* ``test_fix_total`` / ``review_fix_total`` — the *cumulative* per-loop rework total for the whole
  task, never reset on convergence. Use these, not the consecutive counters, to attribute how
  many reworks a completed task actually took to a given loop.
* ``fix_iterations`` — a single global per-task counter, incremented on every entry into ``fixing``;
  bounded by ``agents.max_total_fix_iterations`` (the hard stop guaranteeing termination). Equals
  ``test_fix_total + review_fix_total`` for the default flow (every rework belongs to one loop).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.flow.schema import REWORK_OUTCOMES
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot, reachable_nodes

#: Absent flow budget => only the config cap clamps (mirrors the engine's own ``_LARGE``).
_LARGE = 1 << 60


@dataclass
class LoopCounters:
    """The mutable per-task loop counters persisted on the ``tasks`` row."""

    test_fix_cycles: int = 0
    review_fix_cycles: int = 0
    #: Cumulative per-loop rework totals for the whole task (never reset on convergence).
    test_fix_total: int = 0
    review_fix_total: int = 0
    fix_iterations: int = 0

    @classmethod
    def from_run_state(cls, run_state: FlowRunState) -> LoopCounters:
        """Mirror the engine's authoritative ``FlowRunState`` counters into the operator-facing row.

        The engine owns counting in ``FlowRunState.loop_counters``; these columns back the operator
        surfaces (ledger, CLI ``status``, ``finalize``). The named-loop mirrors are the
        implementation flow's ``test_fix`` / ``review_fix`` loops. Used both at the terminal
        transition and when ``finalize`` reconciles a task the orchestrator never terminated itself
        (killed mid-flow), so the mirror reflects the real churn rather than the last clean sync.
        """
        return cls(
            test_fix_cycles=run_state.counter("test_fix"),
            review_fix_cycles=run_state.counter("review_fix"),
            # Cumulative totals for the audit trail — unlike the consecutive counters above, they
            # are not zeroed on convergence, so a task that succeeded after N reworks records N.
            test_fix_total=run_state.total("test_fix"),
            review_fix_total=run_state.total("review_fix"),
            fix_iterations=run_state.fix_iterations,
        )


def record_rework(run_state: FlowRunState) -> int:
    """The single rework-accounting path: increment the global ``fix_iterations`` once.

    Every in-flow rework/fail edge the engine takes — the test-driven loop (``test_fix``) and the
    review-driven loop (``review_fix``) alike — charges its global cost here and **only** here, so a
    rework is counted exactly once and never double-incremented — the supervisor is an advisory
    layer that never reworks, so no rework edge can double-count. Returns the new global counter
    value.

    The named-loop / inline-edge budgets stay with the engine's edge bookkeeping; this owns only the
    one global counter. The immutable per-verdict audit lives in the ``evaluations`` table, written
    by the evaluator node — recording a verdict never touches this counter.
    """
    return run_state.bump(run_state.GLOBAL_FIX_KEY)


def loop_cap(budgets: Mapping[str, int], max_fix_cycles: int, loop: str) -> int:
    """The effective cap for a named loop: ``min(flow-declared budget, config ceiling)``."""
    return min(budgets.get(loop, _LARGE), max_fix_cycles)


def global_cap(budgets: Mapping[str, int], max_total_fix_iterations: int) -> int:
    """The effective cap for the global fix counter: ``min(flow budget, config ceiling)``."""
    return min(budgets.get(FlowRunState.GLOBAL_FIX_KEY, _LARGE), max_total_fix_iterations)


@dataclass(frozen=True, slots=True)
class ExhaustedLoop:
    """A named fix loop whose consecutive-cycle counter is already at or over its cap."""

    loop: str
    node: str  # the node whose outgoing edge carries this loop
    counter: int
    cap: int


def exhausted_fix_loops(
    snapshot: FlowSnapshot,
    counters: Mapping[str, int],
    max_fix_cycles: int,
    start: str,
) -> list[ExhaustedLoop]:
    """Every named rework/fail loop reachable forward from ``start`` that is already at/over cap.

    Mirrors the engine's own ``_charge_rework``/``_loop_cap`` check (a park always persists the
    counter already at the cap value — the check bumps, then compares — so "already at or over cap"
    is a plain current-state comparison, no bump simulation needed) but runs *before* a resume, from
    outside the engine, over the whole path a resumed run could retake — not just the resume node's
    own edge — so a downstream-already-exhausted loop is caught even when ``--from`` re-enters
    upstream of it.
    """
    out = []
    for node_id in reachable_nodes(snapshot, start):
        for edge in snapshot.adjacency.get(node_id, ()):
            if edge.loop is None or edge.outcome not in REWORK_OUTCOMES:
                continue
            cap = loop_cap(snapshot.doc.budgets, max_fix_cycles, edge.loop)
            counter = counters.get(edge.loop, 0)
            if counter >= cap:
                out.append(ExhaustedLoop(loop=edge.loop, node=node_id, counter=counter, cap=cap))
    return out


def global_backstop_exhausted(
    budgets: Mapping[str, int], max_total_fix_iterations: int, counters: Mapping[str, int]
) -> bool:
    """Whether the hard global fix-iteration ceiling is already at/over cap."""
    counter = counters.get(FlowRunState.GLOBAL_FIX_KEY, 0)
    return counter >= global_cap(budgets, max_total_fix_iterations)
