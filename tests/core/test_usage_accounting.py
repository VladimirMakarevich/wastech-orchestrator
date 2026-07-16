"""Baseline subtraction + snapshot (de)serialization for normalized token usage."""

from __future__ import annotations

import logging

from wastech_orchestrator.core.flow.usage_accounting import (
    USAGE_DELTA_OK,
    USAGE_DELTA_UNKNOWN,
    compute_usage_delta,
    deserialize_usage,
    guard_output_baseline,
    serialize_usage,
    snapshot_for_lineage,
)
from wastech_orchestrator.providers.base import NormalizedUsage, UsageScope


def _codex(input_total: int, cache_read: int, output_total: int, reasoning: int) -> NormalizedUsage:
    """A Codex-shaped cumulative record (input inclusive of cache_read; no cache-creation)."""
    return NormalizedUsage(
        scope=UsageScope.SESSION_CUMULATIVE,
        input_total=input_total,
        cache_read=cache_read,
        cache_write=None,
        uncached_input=input_total - cache_read,
        output_total=output_total,
        reasoning_output=reasoning,
    )


def test_fresh_then_resume_delta_matches_analysis() -> None:
    # Reproduces the analyzed run: a fresh turn.completed followed by a resume turn.completed whose
    # cumulative usage includes the fresh turn. The per-run delta must be the resume's own work, and
    # the two per-node deltas must sum to the latest snapshot (282699), not the naive sum (424163).
    fresh = _codex(input_total=141464, cache_read=76288, output_total=8329, reasoning=5935)
    resume = _codex(input_total=282699, cache_read=187904, output_total=9364, reasoning=6066)

    fresh_delta, fresh_status = compute_usage_delta(
        fresh, None, current_session_id="sess-x", baseline_session_id=None
    )
    resume_delta, resume_status = compute_usage_delta(
        resume, fresh, current_session_id="sess-x", baseline_session_id="sess-x"
    )

    assert fresh_status == USAGE_DELTA_OK and resume_status == USAGE_DELTA_OK
    assert fresh_delta == fresh  # a fresh session's delta is its own cumulative (baseline 0)
    assert resume_delta is not None
    assert resume_delta.input_total == 141235
    assert resume_delta.cache_read == 111616
    assert resume_delta.output_total == 1035
    assert resume_delta.reasoning_output == 131
    assert resume_delta.uncached_input == 94795 - 65176
    # Per-node deltas sum to the latest cumulative snapshot, not the double-counted naive sum.
    assert fresh_delta is not None
    assert fresh_delta.input_total + resume_delta.input_total == 282699
    assert 141464 + 282699 == 424163  # the wrong number a naive per-node sum would report


def test_per_invocation_returned_unchanged() -> None:
    # Claude is per-invocation: each run's usage is already per-run, so the baseline is ignored.
    claude = NormalizedUsage(scope=UsageScope.PER_INVOCATION, input_total=683078, output_total=900)
    baseline = NormalizedUsage(scope=UsageScope.PER_INVOCATION, input_total=500, output_total=10)
    delta, status = compute_usage_delta(
        claude, baseline, current_session_id="s", baseline_session_id="s"
    )
    assert status == USAGE_DELTA_OK
    assert delta == claude


def test_different_session_is_not_subtracted() -> None:
    # A cumulative run whose session differs from the baseline's (the router silently ran fresh) is
    # its own session: its cumulative is its own delta, never a subtraction of a stale baseline.
    baseline = _codex(input_total=141464, cache_read=76288, output_total=8329, reasoning=5935)
    fresh_again = _codex(input_total=40000, cache_read=10000, output_total=2000, reasoning=100)
    delta, status = compute_usage_delta(
        fresh_again, baseline, current_session_id="new-sess", baseline_session_id="old-sess"
    )
    assert status == USAGE_DELTA_OK
    assert delta == fresh_again


def test_smaller_than_baseline_degrades_to_unknown(caplog) -> None:  # type: ignore[no-untyped-def]
    # A snapshot smaller than its baseline (reset / compaction) cannot be a real delta: keep raw,
    # mark unknown, warn — never emit a negative count.
    baseline = _codex(input_total=282699, cache_read=187904, output_total=9364, reasoning=6066)
    smaller = _codex(input_total=141464, cache_read=76288, output_total=8329, reasoning=5935)
    # Attach caplog's handler directly to the module logger: it runs with propagation off (its own
    # stderr handler), so the default root-attached caplog would never see the record.
    logger = logging.getLogger("wastech_orchestrator.core.flow.usage_accounting")
    logger.addHandler(caplog.handler)
    try:
        delta, status = compute_usage_delta(
            smaller, baseline, current_session_id="s", baseline_session_id="s"
        )
    finally:
        logger.removeHandler(caplog.handler)
    assert delta is None
    assert status == USAGE_DELTA_UNKNOWN
    assert "baseline" in caplog.text


def test_no_current_usage_is_none() -> None:
    delta, status = compute_usage_delta(
        None, None, current_session_id=None, baseline_session_id=None
    )
    assert delta is None and status is None


def test_serialize_deserialize_round_trip() -> None:
    usage = _codex(input_total=141464, cache_read=76288, output_total=8329, reasoning=5935)
    restored = deserialize_usage(serialize_usage(usage))
    assert restored == usage
    assert restored is not None and isinstance(restored.scope, UsageScope)  # coerced, not a str
    assert deserialize_usage(None) is None


def test_guard_output_baseline_only_for_cumulative() -> None:
    cumulative = _codex(input_total=100, cache_read=10, output_total=42, reasoning=5)
    per_invocation = NormalizedUsage(scope=UsageScope.PER_INVOCATION, output_total=42)
    assert guard_output_baseline(cumulative) == 42
    assert guard_output_baseline(per_invocation) is None
    assert guard_output_baseline(None) is None


def test_snapshot_for_lineage_only_for_cumulative() -> None:
    cumulative = _codex(input_total=100, cache_read=10, output_total=42, reasoning=5)
    per_invocation = NormalizedUsage(scope=UsageScope.PER_INVOCATION, output_total=42)
    assert snapshot_for_lineage(cumulative) is not None
    assert snapshot_for_lineage(per_invocation) is None
    assert snapshot_for_lineage(None) is None
