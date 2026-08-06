"""What terminal an exhausted provider stage earns: fail-closed manual, resumable park, or terminal.

The decision is a pure function over the **set** of error classes the stage raised, never over the
last one alone: a fallback provider that fails worse than the primary must not be able to turn a
park-eligible primary failure into a terminal one, or whether a task survives comes to depend on a
provider that never ran a token of work.

Kept out of the exception handlers that consume it so the precedence — security first, always — is
stated exactly once and cannot drift between node kinds.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from wastech_orchestrator.providers.base import PARK_ELIGIBLE, ErrorClass

# Infra error classes that are a fail-closed SECURITY / manual-action condition for ANY node kind
# (agent or evaluator): a provider tree that could not be proven quiescent
# (``CONTAINMENT_UNVERIFIED``) or a missing host isolation capability with no qualifying fallback
# (``CAPABILITY_UNAVAILABLE``). Never a quality fail, never a park, never a shippable green diff.
_CONTAINMENT_MANUAL_CLASSES = frozenset(
    {ErrorClass.CONTAINMENT_UNVERIFIED, ErrorClass.CAPABILITY_UNAVAILABLE}
)


class InfraDisposition(StrEnum):
    """What the Core does with a node whose provider stage was exhausted."""

    #: Fail closed to manual action — an unproven process tree or a missing isolation capability.
    #: Never auto-resumed, whatever else the stage reported.
    MANUAL = "manual"
    #: Resumable soft pause: the task stays active on its checkpoint and a later tick retries it.
    PARK = "park"
    #: Terminal by the failing node kind's own rule. An agent node has no result left to ship; an
    #: evaluator still holds a reviewable diff. Which terminal that is belongs to the caller.
    TERMINAL = "terminal"


def classify_exhaustion(
    classes: Sequence[ErrorClass], *, representative: ErrorClass | None
) -> InfraDisposition:
    """Decide the disposition for a stage that exhausted every provider.

    ``classes`` is every class the stage *raised* — each attempt, including same-provider transient
    retries and the fallback. ``representative`` is the single class the Router settled on; it is
    consulted only for the operator-stop case, because a stop replaces the representative while the
    killed attempt's own row still reads as a process crash. Pure, so the precedence is directly
    table-tested rather than inferred from an end-to-end outcome.
    """
    if any(c in _CONTAINMENT_MANUAL_CLASSES for c in classes):
        # Security first and unconditionally. A rate-limited primary must never paper over an
        # unproven process tree on the fallback, and this outranks the stop branch below too: a stop
        # cancels an agent, it does not prove that no unknown descendant is still writing.
        return InfraDisposition.MANUAL
    if representative is ErrorClass.CANCELLED:
        # An operator stop means stopped, not waiting. Checked before the park-eligible scan because
        # the killed attempt's own row carries the crash class, so a stop that landed on a
        # rate-limited stage would otherwise read as a provider window that reopens on its own.
        return InfraDisposition.PARK
    if any(c in PARK_ELIGIBLE for c in classes):
        # One park-eligible attempt is enough. This does widen where a no-progress fallback can
        # hold the single processing slot — a rate-limited primary paired with one now parks — and
        # that is intended: the primary's limit is a real transient that resets on its own window.
        return InfraDisposition.PARK
    # Nothing resumable was reported, including a stage that raised no class at all, where there is
    # no evidence to justify holding the slot. Fail closed.
    return InfraDisposition.TERMINAL
