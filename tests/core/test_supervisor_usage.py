"""The supervisor spend roll-up: bucketing by phase, and the honesty of its absent values."""

from __future__ import annotations

from typing import Any

from wastech_orchestrator.core.supervisor_usage import SupervisorFunction, summarize_spend
from wastech_orchestrator.state_store import ProviderAttemptRow


def _attempt(function: str | None = "observe", **overrides: Any) -> ProviderAttemptRow:
    fields: dict[str, Any] = {
        "task_id": "t1",
        "node_run_id": None if function else 7,
        "supervisor_function": function,
        "provider": "claude",
        "attempt": 1,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:30+00:00",
        "usage_input_total": 1_000,
        "usage_cache_read": 900,
        "usage_cache_write": 0,
        "usage_output_total": 100,
        "usage_cost": 0.01,
    }
    fields.update(overrides)
    return ProviderAttemptRow(**fields)


def test_no_supervisor_calls_yields_no_report() -> None:
    # Distinct from a report of zeros: "the layer never ran" (observations off and a finalize that
    # could not start) is not "it ran and cost nothing".
    assert summarize_spend([]) is None
    assert summarize_spend([_attempt(function=None)]) is None


def test_graph_node_attempts_are_excluded_from_the_layer_report() -> None:
    report = summarize_spend([_attempt(function=None, usage_input_total=500_000), _attempt()])
    assert report is not None
    assert report["total"]["calls"] == 1
    assert report["total"]["input"] == 1_000


def test_buckets_sum_to_the_total_and_are_ordered_by_phase() -> None:
    rows = [
        _attempt(function="finalize", usage_input_total=20_000),
        _attempt(usage_input_total=30_000),
        _attempt(function="skill", usage_input_total=5_000),
        _attempt(usage_input_total=40_000),
    ]
    report = summarize_spend(rows)
    assert report is not None
    # Phases read in the order a run reaches them, not first-seen or alphabetical, so two runs'
    # reports line up side by side.
    assert list(report["by_function"]) == ["observe", "finalize", "skill"]
    assert report["by_function"]["observe"]["calls"] == 2
    assert report["by_function"]["observe"]["input"] == 70_000
    assert report["total"]["calls"] == 4
    assert report["total"]["input"] == 95_000
    assert sum(b["input"] for b in report["by_function"].values()) == report["total"]["input"]


def test_every_field_the_report_promises_is_present() -> None:
    report = summarize_spend([_attempt()])
    assert report is not None
    assert set(report["total"]) == {
        "calls",
        "input",
        "cache_read",
        "cache_write",
        "output",
        "cost",
        "duration_seconds",
    }
    assert report["total"]["cache_read"] == 900
    assert report["total"]["output"] == 100
    assert report["total"]["duration_seconds"] == 30.0


def test_a_provider_that_reports_no_cost_reports_null_not_zero() -> None:
    # Codex reports no cost at all, and 0.0 there would read as "measured, and it was free".
    report = summarize_spend([_attempt(usage_cost=None), _attempt(usage_cost=None)])
    assert report is not None
    assert report["total"]["cost"] is None
    assert report["total"]["input"] == 2_000  # tokens are still known


def test_a_mixed_bucket_sums_the_costs_it_knows() -> None:
    report = summarize_spend([_attempt(usage_cost=None), _attempt(usage_cost=0.25)])
    assert report is not None
    assert report["total"]["cost"] == 0.25


def test_unmeasured_calls_are_counted_so_the_totals_are_not_read_as_complete() -> None:
    # A turn that produced no result, or a session snapshot that yielded no delta, leaves the usage
    # columns NULL. Without this count the sums would look complete while under-reporting.
    report = summarize_spend([_attempt(), _attempt(usage_input_total=None, usage_cost=None)])
    assert report is not None
    assert report["total"]["calls"] == 2
    assert report["total"]["calls_without_usage"] == 1
    assert "calls_without_usage" not in summarize_spend([_attempt()])["total"]  # type: ignore[index]


def test_an_unreadable_interval_leaves_the_duration_unclaimed() -> None:
    report = summarize_spend([_attempt(started_at=None), _attempt(finished_at="not-a-timestamp")])
    assert report is not None
    assert report["total"]["duration_seconds"] is None
    # One readable interval is enough to report what is known.
    mixed = summarize_spend([_attempt(started_at=None), _attempt()])
    assert mixed is not None
    assert mixed["total"]["duration_seconds"] == 30.0


def test_an_unrecognized_label_is_reported_last_rather_than_dropped() -> None:
    # Dropping a bucket would break the identity that the phases sum to the layer's total.
    report = summarize_spend([_attempt(function="mystery"), _attempt()])
    assert report is not None
    assert list(report["by_function"]) == ["observe", "mystery"]
    assert report["total"]["calls"] == 2


def test_the_four_phase_labels_are_the_values_persisted() -> None:
    assert [f.value for f in SupervisorFunction] == ["observe", "finalize", "handoff", "skill"]
