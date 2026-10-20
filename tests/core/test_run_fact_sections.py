"""The fact blocks every pull-request body carries: provider fallbacks and the run's cost.

Both come from ``enrich-07b``. Its PR body never said that the critic pinned to Codex ran on Claude
in all three passes, and every surface reported "cost not accounted" beside no total at all — while
``state.db`` held $114.11 of recorded cost across 19 provider calls.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator import cli
from wastech_orchestrator.core.flow.nodes.base import provider_fallback_note
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.core.summary_report import (
    PROVIDER_FALLBACKS_HEADING,
    RUN_COST_HEADING,
    render_provider_fallbacks_section,
    render_run_cost_section,
    upsert_section,
)
from wastech_orchestrator.core.supervisor_usage import describe_task_cost, summarize_task_spend
from wastech_orchestrator.providers.base import ErrorClass, ProviderId
from wastech_orchestrator.routing.router import (
    ProviderAttempt,
    ResolvedRoute,
    RouteSource,
    StageOutcome,
)
from wastech_orchestrator.state_store import NodeRunRow, ProviderAttemptRow, StateStore, TaskRow


def _critic_run(run_id: int) -> NodeRunRow:
    return NodeRunRow(
        task_id="t",
        node_id="coverage_critic",
        node_kind="evaluator",
        route_primary="codex",
        route_source="flow_node",
        provider_used="claude",
        id=run_id,
    )


def _attempt(run_id: int | None, provider: str, *, cost: float | None, error: str | None = None):
    return ProviderAttemptRow(
        task_id="t",
        node_run_id=run_id,
        provider=provider,
        attempt=1,
        error_class=error,
        usage_cost=cost,
        usage_input_total=None if cost is None else 1000,
    )


def test_the_fallback_note_names_both_providers_why_and_the_pin() -> None:
    route = ResolvedRoute(
        node_id="coverage_critic",
        primary=ProviderId.CODEX,
        fallback=ProviderId.CLAUDE,
        source=RouteSource.FLOW_NODE,
    )
    outcome = StageOutcome(
        route=route,
        result=None,
        provider_used=ProviderId.CLAUDE,
        stage_attempts=2,
        terminal_error=None,
        attempts=(
            ProviderAttempt(
                provider=ProviderId.CODEX,
                attempt=1,
                status=None,
                error_class=ErrorClass.PROCESS_CRASHED,
                result=None,
            ),
        ),
    )
    note = provider_fallback_note(outcome)
    assert note is not None
    assert "routed to codex, ran on claude after codex failed (process_crashed)" in note
    assert "pinned" in note


def test_no_note_when_the_stage_ran_where_it_was_routed() -> None:
    route = ResolvedRoute(
        node_id="n", primary=ProviderId.CLAUDE, fallback=None, source=RouteSource.CONFIG
    )
    outcome = StageOutcome(
        route=route,
        result=None,
        provider_used=ProviderId.CLAUDE,
        stage_attempts=1,
        terminal_error=None,
        attempts=(),
    )
    assert provider_fallback_note(outcome) is None


def test_the_fallback_section_says_every_pass_ran_on_the_other_model() -> None:
    runs = [_critic_run(100), _critic_run(104), _critic_run(105)]
    attempts = [
        _attempt(run_id, "codex", cost=None, error="process_crashed") for run_id in (100, 104, 105)
    ]
    section = render_provider_fallbacks_section(runs, attempts)
    assert section is not None
    assert section.startswith(PROVIDER_FALLBACKS_HEADING)
    assert (
        "- `coverage_critic`: routed to `codex` (pinned by the flow), ran on `claude` in 3 of 3 "
        "passes — `codex` failed with `process_crashed`." in section
    )


def test_no_fallback_section_when_nothing_fell_back() -> None:
    row = NodeRunRow(
        task_id="t", node_id="a", node_kind="agent", route_primary="claude", provider_used="claude"
    )
    assert render_provider_fallbacks_section([row], []) is None


def test_the_cost_section_sums_what_was_priced_and_names_what_was_not() -> None:
    attempts = [
        _attempt(1, "claude", cost=100.0),
        _attempt(2, "claude", cost=14.11),
        _attempt(3, "codex", cost=None, error="process_crashed"),
    ]
    section = render_run_cost_section(attempts)
    assert section is not None
    assert section.startswith(RUN_COST_HEADING)
    assert "Recorded provider cost: $114.11 over 3 provider calls" in section
    assert "Claude's reported cost includes its subagents" in section
    assert "Codex cost not accounted" in section


def test_the_subagent_note_appears_only_when_claude_ran() -> None:
    section = render_run_cost_section([_attempt(1, "codex", cost=None)])
    assert section is not None
    assert "subagents" not in section
    assert "No provider cost was recorded for its 1 provider call." in section


def test_status_and_the_pull_request_share_one_cost_wording() -> None:
    attempts = [_attempt(1, "claude", cost=10.0), _attempt(2, "claude", cost=1.5)]
    described = describe_task_cost(summarize_task_spend(attempts))
    assert (
        described == "$11.50 over 2 provider calls (Claude's reported cost includes its subagents)"
    )
    section = render_run_cost_section(attempts)
    assert section is not None and described in section


def test_a_reused_body_gets_its_fact_sections_rewritten_not_duplicated() -> None:
    # A body written at an earlier park, reused after `rerun --continue`: its cost was the park's.
    body = "# T\n\nprose\n\n## Run cost\n\nRecorded provider cost: $40.00.\n\n## Tail\n\nend\n"
    fresh = "## Run cost\n\nRecorded provider cost: $114.11.\n"
    updated = upsert_section(body, fresh)
    assert updated.count("## Run cost") == 1
    assert "$114.11" in updated and "$40.00" not in updated
    assert updated.endswith("## Tail\n\nend\n")
    assert upsert_section(updated, fresh) == updated
    assert upsert_section("# T\n", fresh) == "# T\n\n" + fresh


def test_task_spend_totals_every_attempt_and_splits_by_provider() -> None:
    spend = summarize_task_spend(
        [_attempt(1, "claude", cost=10.0), _attempt(None, "claude", cost=1.5)]
    )
    assert spend is not None
    assert spend["total"]["cost"] == 11.5
    assert spend["total"]["calls"] == 2
    assert set(spend["by_provider"]) == {"claude"}
    assert summarize_task_spend([]) is None


def test_status_prints_the_task_cost(tmp_path: Path) -> None:
    store = StateStore.open(tmp_path / "state.db")
    store.insert_task(TaskRow(task_id="t", title="T", status=Status.DONE))
    store.record_provider_attempt(_attempt(None, "claude", cost=114.11))
    assert cli._task_cost_line(store, "t") == (
        "$114.11 over 1 provider call (Claude's reported cost includes its subagents)"
    )
    assert cli._task_cost_line(store, "missing") is None
