"""Flow runtime checkpoint — the per-run engine state (P1.1, persistence in P1.2).

:class:`FlowRunState` is the mutable checkpoint the engine threads through a single graph
traversal: where execution currently is (``current_node``), what has run (``completed_nodes``), and
the loop/budget counters (``loop_counters``). Per the resume model (``index.md``) the durable
checkpoint is ``{completed_nodes, current_node, loop_counters, publish_operations}``;
``publish_operations`` is read from the state store (idempotency lives there) and is **not**
duplicated here.

In P1.1 the state is in-memory only. P1.2 adds the persistence seam (hydrate from ``node_runs`` +
``tasks`` + ``publish_operations``, schema v4) without changing this shape.

``loop_counters`` is a single ``dict[str, int]`` keyed by (P1.1/P1.2 decision):

* a named loop's name (``loop: test_fix``) — the consecutive-cycle counter for that loop;
* the synthetic edge key ``"<from>-><to>:<outcome>"`` — for an inline ``budget: N`` rework edge;
* the reserved key :data:`FlowRunState.GLOBAL_FIX_KEY` (``"global_fix_iterations"``) — the single
  global per-task fix counter incremented on **every** rework/fail edge (the hard stop).

This one dict carries both the named-loop and inline-budget meanings without per-flow special
cases, so the engine stays free of domain knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class FlowRunState:
    """Mutable runtime checkpoint for one unit's traversal of a flow graph."""

    #: Reserved ``loop_counters`` key for the single global fix counter (mirrors the
    #: ``budgets.global_fix_iterations`` cap key). Synthetic edge keys contain ``"->"`` and named
    #: loops are operator-chosen, so this reserved name cannot collide with either.
    GLOBAL_FIX_KEY: ClassVar[str] = "global_fix_iterations"

    flow_fingerprint: str
    current_node: str | None = None
    #: Ordered trace of node executions (a node re-appears each time a loop re-enters it). P1.2
    #: refines how resume uses this; P1.1 only appends.
    completed_nodes: list[str] = field(default_factory=list)
    loop_counters: dict[str, int] = field(default_factory=dict)

    @property
    def fix_iterations(self) -> int:
        """The single global fix counter (0 when no rework/fail edge has been taken yet)."""
        return self.loop_counters.get(self.GLOBAL_FIX_KEY, 0)

    def counter(self, key: str) -> int:
        """Current value of a loop/budget counter (0 if never incremented)."""
        return self.loop_counters.get(key, 0)

    def bump(self, key: str) -> int:
        """Increment ``key`` by one and return the new value."""
        new = self.loop_counters.get(key, 0) + 1
        self.loop_counters[key] = new
        return new

    def reset(self, key: str) -> None:
        """Reset a loop/budget counter to zero (drop the key entirely)."""
        self.loop_counters.pop(key, None)

    def mark_completed(self, node_id: str) -> None:
        """Append ``node_id`` to the execution trace."""
        self.completed_nodes.append(node_id)

    def reset_for_next_subtask(self) -> None:
        """Drop every loop/inline-budget counter EXCEPT the global fix counter (decomposition).

        Each subtask gets fresh per-loop / per-edge budgets, but the global ``fix_iterations``
        accumulates across the whole decomposed task (the ``shared_budget`` hard stop). Generic —
        covers named loops + inline supervisor budgets without naming them.
        """
        glob = self.loop_counters.get(self.GLOBAL_FIX_KEY)
        self.loop_counters.clear()
        if glob is not None:
            self.loop_counters[self.GLOBAL_FIX_KEY] = glob
