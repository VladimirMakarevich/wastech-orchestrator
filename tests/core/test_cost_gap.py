"""The stated cost gap (P1.7): what the recorded total does not cover, named and never estimated."""

from __future__ import annotations

from wastech_orchestrator.core.cost_gap import cost_gaps, gap_json, render_cost_gap
from wastech_orchestrator.state_store import ProviderAttemptRow


def _attempt(
    provider: str, *, cost: float | None = None, tokens: int | None = None, attempt: int = 1
) -> ProviderAttemptRow:
    return ProviderAttemptRow(
        task_id="task-001",
        node_run_id=1,
        provider=provider,
        attempt=attempt,
        usage_cost=cost,
        usage_input_total=tokens,
    )


def test_a_fully_priced_run_says_nothing() -> None:
    # A Claude-only run reports a complete total, so there is no gap and no line.
    rows = [_attempt("claude", cost=1.5, tokens=100), _attempt("claude", cost=2.0, tokens=200)]
    assert cost_gaps(rows) == ()
    assert render_cost_gap(cost_gaps(rows)) is None


def test_unpriced_attempts_are_grouped_and_counted_per_provider() -> None:
    rows = [
        _attempt("claude", cost=15.81, tokens=28_800_000),
        *[_attempt("codex", tokens=646_000, attempt=n) for n in range(1, 14)],
    ]
    gaps = cost_gaps(rows)
    assert [(g.provider, g.attempts, g.input_tokens) for g in gaps] == [("codex", 13, 13 * 646_000)]
    assert render_cost_gap(gaps) == "Codex cost not accounted (13 attempts, 8.4 M input tokens)"


def test_an_unmeasured_attempt_is_counted_but_not_summed_as_zero() -> None:
    # A result-less attempt reported no tokens either. Saying "0 input tokens" would read as
    # "measured, and it was nothing" — the same mistake as the silent zero cost this line exists
    # to prevent.
    gaps = cost_gaps([_attempt("codex")])
    assert gaps[0].input_tokens is None
    assert render_cost_gap(gaps) == (
        "Codex cost not accounted (1 attempt, input tokens not measured)"
    )


def test_several_unpriced_providers_are_joined_in_name_order() -> None:
    rows = [_attempt("codex", tokens=2_000), _attempt("claude", tokens=500)]
    assert render_cost_gap(cost_gaps(rows)) == (
        "Claude cost not accounted (1 attempt, 500 input tokens); "
        "Codex cost not accounted (1 attempt, 2.0 k input tokens)"
    )


def test_gap_json_is_plain_data_for_summary_json() -> None:
    gaps = cost_gaps([_attempt("codex", tokens=8_400_000)])
    assert gap_json(gaps) == [{"provider": "codex", "attempts": 1, "input_tokens": 8_400_000}]
