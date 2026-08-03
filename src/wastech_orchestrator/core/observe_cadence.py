"""The supervisor observation cadence: which completed steps are worth an LLM turn.

Pure functions over primitives — no store, no engine types, no config object beyond
:class:`~wastech_orchestrator.config.schema.ObserveMode`. The orchestrator's post-node hook owns the
facts (it has the outcome and the step's ``node_runs`` row); this module owns the *policy*, so the
policy is testable without a run and the hook stays a few lines.

Deliberately flow-agnostic: nothing here (or in the hook) may branch on a flow name, a node id
convention, or a path. A per-flow cadence is data — the flow's own ``supervisor.observe.mode`` — and
a user-authored flow gets the same treatment as a packaged one.
"""

from __future__ import annotations

from collections.abc import Iterable

from wastech_orchestrator.config.schema import ObserveMode

# Rank = how many LLM calls the mode can produce, so a *lower* rank is narrower: `none` never
# observes, `events` only on a deviation, `selected` on every listed node, `all` on every observable
# step. `selected` sits ABOVE `events` because a list of nodes is in the limit wider than "only when
# something went wrong" — which is what closes the "narrow to `selected` under a global `events`"
# loophole by construction, with no special case. Same shape as `security.profiles`.
_MODE_RANK: dict[str, int] = {
    ObserveMode.NONE.value: 0,
    ObserveMode.EVENTS.value: 1,
    ObserveMode.SELECTED.value: 2,
    ObserveMode.ALL.value: 3,
}


def is_same_or_narrower(candidate: str, reference: str) -> bool:
    """Return True iff ``candidate`` observes no more often than ``reference``.

    Fail-closed: an unrecognized mode on either side returns False, because a flow may never *widen*
    the operator's global cadence. With both modes known this is ``rank(candidate) <=
    rank(reference)`` (a lower rank observes less).
    """
    candidate_rank = _MODE_RANK.get(candidate)
    reference_rank = _MODE_RANK.get(reference)
    if candidate_rank is None or reference_rank is None:
        return False
    return candidate_rank <= reference_rank


def resolve_mode(global_mode: ObserveMode, flow_mode: ObserveMode | None) -> ObserveMode:
    """The cadence in force for a run: the flow's mode when it declares one, else the global one.

    A flow narrows, never widens — but that is the *validator's* rule
    (``validate_flow_against_config``), enforced before any node runs, so this resolution can trust
    the flow value outright. A flow with no ``supervisor:`` block (or no ``observe:`` in it)
    inherits the global mode, which is why the global default matters more than any packaged flow's.
    """
    return flow_mode if flow_mode is not None else global_mode


def triggers_for(
    *,
    outcome_kind: str,
    rework_exhausted: bool,
    status: str | None,
    fell_back: bool,
) -> frozenset[str]:
    """The deviations this completed step exhibits, as a subset of ``OBSERVE_TRIGGERS``.

    Everything here is already in the post-node hook's hands: ``outcome_kind`` /
    ``rework_exhausted`` come from the node outcome, ``status`` and ``fell_back`` from the step's
    own ``node_runs`` row. Nothing is inferred and no extra provider call is made.

    * ``rework`` — an evaluator sent the stage back, or accepted only after spending its whole
      rework budget (``rework_exhausted``): the run is looping, which is the case worth a note.
    * ``failure`` — the run row's ``status`` is ``failed``. Read from the row, not the outcome,
      because an agent node's outcome kind is unconditionally ``done`` even when its provider result
      failed on quality (a hard infra failure raises before this hook ever runs).
    * ``fallback`` — the attempt landed on a provider other than the resolved primary. Taken as an
      already-decided fact, because the step record answers it for the finalize packet too, and one
      deviation must not be able to read two ways.
    """
    triggers: set[str] = set()
    if outcome_kind == "rework" or rework_exhausted:
        triggers.add("rework")
    if status == "failed":
        triggers.add("failure")
    if fell_back:
        triggers.add("fallback")
    return frozenset(triggers)


def should_observe(
    *,
    mode: ObserveMode,
    node_id: str,
    include_nodes: Iterable[str],
    enabled_triggers: Iterable[str],
    triggers: frozenset[str],
) -> bool:
    """Decide whether this step gets an LLM observation under ``mode``.

    ``triggers`` is only consulted under ``events`` — the caller reads the ``node_runs`` row (and so
    calls :func:`triggers_for`) only in that mode, and passes an empty set otherwise, so the other
    three modes cost no database read. ``enabled_triggers`` is the operator's subset of
    ``OBSERVE_TRIGGERS``, letting a run narrow to (say) only ``failure``.

    Note what is *not* here: the whole-task ``finalize`` turn, the subtask ``handoff`` brief, and
    the skill proposal are all unaffected by the cadence. Finalize is seeded by the deterministic
    ``SupervisorPacket``, which is built from ``node_runs`` and each node's own output file — never
    from observations — so a summary stays complete at ``mode: none``.
    """
    match mode:
        case ObserveMode.NONE:
            return False
        case ObserveMode.ALL:
            return True
        case ObserveMode.SELECTED:
            return node_id in set(include_nodes)
        case ObserveMode.EVENTS:
            return bool(triggers & set(enabled_triggers))
