"""What the supervisor layer spent, per phase — the vocabulary and the roll-up.

The layer makes provider calls for three different reasons, and until they were labelled the only
question the audit table could answer was "how much did the supervisor cost in total" (its rows are
the ``node_run_id IS NULL`` ones). That is the wrong grain for the decision an operator actually
makes: whether the per-step notes are worth their share against the turn that writes the summary.

:class:`SupervisorFunction` is that label. It lives here rather than in the storage layer (which
stays free of domain vocabulary) and rather than in the supervisor itself (so the roll-up can name
the phases without importing the class that runs them).

:func:`summarize_spend` is the read side: a pure function over already-persisted attempt rows, so
the report is testable without a run and cannot disagree with the database it came from.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from wastech_orchestrator.state_store import ProviderAttemptRow


class SupervisorFunction(StrEnum):
    """Which of the supervisor layer's three jobs a provider call belongs to.

    Passed explicitly by the calling phase and persisted on the attempt row. It is deliberately not
    derived from the synthetic run ids or attempt-dir names that namespace the layer's artifacts:
    those are a naming scheme, and reading a phase out of one would make a path string carry meaning
    it was never meant to hold.
    """

    OBSERVE = "observe"
    FINALIZE = "finalize"
    HANDOFF = "handoff"


# Report the phases in the order a run reaches them, not in first-seen or alphabetical order, so two
# runs' reports line up. An unrecognized label (a hand-edited database) sorts last rather than being
# dropped — a bucket silently missing would break the identity that the phases sum to the total.
_PHASE_ORDER = {function.value: index for index, function in enumerate(SupervisorFunction)}

# The persisted usage columns, paired with the report key each is published under.
_TOKEN_FIELDS = (
    ("input", "usage_input_total"),
    ("cache_read", "usage_cache_read"),
    ("cache_write", "usage_cache_write"),
    ("output", "usage_output_total"),
)


def summarize_spend(attempts: Sequence[ProviderAttemptRow]) -> dict[str, Any] | None:
    """The supervisor layer's spend from a task's attempt rows, in total and per phase.

    Takes every attempt of the task and keeps the labelled ones, so the caller can hand over the
    task-wide read without pre-filtering. Returns ``None`` when the layer made no calls at all — a
    report of zeros would suggest it ran and cost nothing, which is a different fact from "it never
    ran" (a flow with observations off and a finalize that could not start).
    """
    labelled = [row for row in attempts if row.supervisor_function]
    if not labelled:
        return None
    buckets: dict[str, list[ProviderAttemptRow]] = {}
    for row in labelled:
        buckets.setdefault(str(row.supervisor_function), []).append(row)
    ordered = sorted(buckets, key=lambda label: (_PHASE_ORDER.get(label, len(_PHASE_ORDER)), label))
    return {
        "total": _bucket(labelled),
        "by_function": {label: _bucket(buckets[label]) for label in ordered},
    }


def _bucket(rows: Sequence[ProviderAttemptRow]) -> dict[str, Any]:
    """One group's call count, token totals, cost and provider wall time.

    ``calls_without_usage`` appears only when some attempt reported no usable numbers (a turn that
    never produced a result, or a session snapshot that could not yield a delta). Without it the
    totals would read as complete while under-counting, which is the accounting bug this whole
    measurement path exists to avoid.
    """
    bucket: dict[str, Any] = {"calls": len(rows)}
    for key, column in _TOKEN_FIELDS:
        bucket[key] = _total(getattr(row, column) for row in rows)
    bucket["cost"] = _total(row.usage_cost for row in rows)
    bucket["duration_seconds"] = _duration(rows)
    unmeasured = sum(1 for row in rows if row.usage_input_total is None)
    if unmeasured:
        bucket["calls_without_usage"] = unmeasured
    return bucket


def _total(values: Any) -> int | float | None:
    """The sum of the values that are present, or ``None`` when none of them is.

    ``None`` rather than ``0`` on purpose: a provider that does not report cost at all (Codex) and a
    turn that produced no result both leave the column NULL, and a zero there would be read as
    "measured, and it was free".
    """
    present = [value for value in values if value is not None]
    if not present:
        return None
    total = sum(present)
    return round(total, 6) if isinstance(total, float) else total


def _duration(rows: Sequence[ProviderAttemptRow]) -> float | None:
    """Summed provider wall time in seconds over the attempts whose interval is readable.

    This is time spent inside the provider, not the layer's share of the run: the attempt row is
    stamped from the result's own measured interval, which is the only duration the audit trail has.
    """
    total = 0.0
    measured = False
    for row in rows:
        start, finish = _instant(row.started_at), _instant(row.finished_at)
        if start is None or finish is None:
            continue
        measured = True
        total += (finish - start).total_seconds()
    return round(total, 3) if measured else None


def _instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
