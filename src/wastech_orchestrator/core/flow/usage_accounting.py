"""Baseline subtraction for provider token usage (the orchestrator half of usage accounting).

Providers emit a :class:`NormalizedUsage` in their own scope: cumulative-per-session for Codex,
per-invocation for Claude. Turning that into a summation-safe PER-RUN figure — subtracting a
resumed session's previous cumulative snapshot — needs the state store, so it lives here on the core
side, never in a provider (a provider must not read the store). This module also owns the JSON
round-trip for the snapshot the lineage tables persist.
"""

from __future__ import annotations

import dataclasses
import json
import logging

from wastech_orchestrator.providers.base import NormalizedUsage, UsageScope

_LOG = logging.getLogger(__name__)

# The delta-status values persisted on ``provider_attempts.usage_delta_status``.
USAGE_DELTA_OK = "ok"
USAGE_DELTA_UNKNOWN = "unknown"

# The additive token fields subtracted field-by-field to form a per-run delta (``cost`` is deferred
# and ``scope`` is carried through, so neither participates).
_DELTA_FIELDS = (
    "input_total",
    "cache_read",
    "cache_write",
    "uncached_input",
    "output_total",
    "reasoning_output",
)


def serialize_usage(usage: NormalizedUsage) -> str:
    """The cumulative snapshot as compact JSON for the lineage ``usage_snapshot`` column."""
    return json.dumps(dataclasses.asdict(usage), separators=(",", ":"))


def deserialize_usage(raw: str | None) -> NormalizedUsage | None:
    """Reconstruct a :class:`NormalizedUsage` from a ``usage_snapshot`` blob, or ``None`` if absent.

    Coerces the ``scope`` string back to :class:`UsageScope` (a plain ``**json.loads`` would leave
    it a bare string and defeat the scope checks that decide whether to subtract).
    """
    if not raw:
        return None
    data = json.loads(raw)
    return NormalizedUsage(
        scope=UsageScope(data["scope"]),
        input_total=data.get("input_total"),
        cache_read=data.get("cache_read"),
        cache_write=data.get("cache_write"),
        uncached_input=data.get("uncached_input"),
        output_total=data.get("output_total"),
        reasoning_output=data.get("reasoning_output"),
        cost=data.get("cost"),
    )


def guard_output_baseline(baseline: NormalizedUsage | None) -> int | None:
    """The no-work guard's baseline output count — only for a cumulative session, else ``None``.

    A per-invocation provider (Claude) already reports each run's own output, so subtracting a
    previous figure would be wrong; the guard must compare against 0 there.
    """
    if baseline is None or baseline.scope is not UsageScope.SESSION_CUMULATIVE:
        return None
    return baseline.output_total


def snapshot_for_lineage(usage: NormalizedUsage | None) -> str | None:
    """The lineage ``usage_snapshot`` JSON for a finished run, or ``None`` for a per-invocation run.

    A per-invocation provider (Claude) has no cumulative to carry forward, so nothing is stored and
    a later resume of the same lineage has no baseline to subtract.
    """
    if usage is None or usage.scope is not UsageScope.SESSION_CUMULATIVE:
        return None
    return serialize_usage(usage)


def compute_usage_delta(
    current: NormalizedUsage | None,
    baseline: NormalizedUsage | None,
    *,
    current_session_id: str | None,
    baseline_session_id: str | None,
) -> tuple[NormalizedUsage | None, str | None]:
    """The summation-safe per-run usage for one attempt, with a persistable status.

    - No usage → ``(None, None)``.
    - Per-invocation provider → the record is already per-run, returned unchanged.
    - Cumulative provider → subtract the previous snapshot only when it belongs to the SAME session
      (``current_session_id == baseline_session_id``); a missing or different session means a fresh
      session whose cumulative is its own delta. A snapshot smaller than its baseline (session reset
      / compaction / version drift) cannot be a real delta, so it degrades to ``(None, "unknown")``
      with a warning — never a negative count.
    """
    if current is None:
        return None, None
    if current.scope is not UsageScope.SESSION_CUMULATIVE:
        return current, USAGE_DELTA_OK
    if baseline is None or current_session_id != baseline_session_id:
        return current, USAGE_DELTA_OK
    delta = _subtract(current, baseline)
    if delta is None:
        # The raw session id is a secret, so it is not logged; the attempt row still keeps the raw
        # payload — only the numeric delta is dropped.
        _LOG.warning("usage snapshot smaller than its baseline; recording raw usage, no delta")
        return None, USAGE_DELTA_UNKNOWN
    return delta, USAGE_DELTA_OK


def _subtract(current: NormalizedUsage, baseline: NormalizedUsage) -> NormalizedUsage | None:
    """Field-by-field ``current - baseline``, or ``None`` if any field would go negative.

    None-safe: an absent current field stays ``None``; an absent baseline field contributes 0 (all
    of current is new). Only fields present on both sides can trigger the negative guard.
    """
    values: dict[str, int | None] = {}
    for name in _DELTA_FIELDS:
        cur = getattr(current, name)
        base = getattr(baseline, name)
        if cur is None:
            values[name] = None
        elif base is None:
            values[name] = cur
        else:
            diff = cur - base
            if diff < 0:
                return None
            values[name] = diff
    return NormalizedUsage(scope=current.scope, cost=None, **values)
