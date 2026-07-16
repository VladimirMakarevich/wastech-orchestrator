"""The no-work guard (``_produced_no_work``) — fires on a per-run output delta of zero.

The guard must recognize a resumed Codex node that produced no NEW output, where the cumulative
``output_tokens`` is never 0. It subtracts the resumed session's previous cumulative (passed on the
request) only while ``session_id`` is set, so a run the router dropped to a fresh session reads its
own absolute output.
"""

from __future__ import annotations

from wastech_orchestrator.providers._adapter_base import ParsedEvents, _produced_no_work
from wastech_orchestrator.providers.base import (
    MAX_TURNS_SUBTYPE,
    AgentRunRequest,
    NormalizedUsage,
    UsageScope,
)


def _request(*, session_id: str | None = None, baseline: int | None = None) -> AgentRunRequest:
    return AgentRunRequest(
        task_id="t",
        node_id="n",
        working_directory=".",
        prompt="p",
        permission_profile="read-only",
        timeout_seconds=60,
        attempt=1,
        node_run_id=1,
        session_id=session_id,
        resume_baseline_output_tokens=baseline,
    )


def _parsed(
    *,
    output_total: int | None,
    succeeded: bool = False,
    structured_output: dict[str, object] | None = None,
    failure_subtype: str | None = None,
    with_usage: bool = True,
) -> ParsedEvents:
    usage = (
        NormalizedUsage(scope=UsageScope.SESSION_CUMULATIVE, output_total=output_total)
        if with_usage
        else None
    )
    return ParsedEvents(
        final_message=None,
        structured_output=structured_output,
        usage=None,
        session_id=None,
        succeeded=succeeded,
        normalized_usage=usage,
        failure_subtype=failure_subtype,
    )


def test_fresh_run_zero_output_fires() -> None:
    assert _produced_no_work(_parsed(output_total=0), _request()) is True


def test_fresh_run_with_output_does_not_fire() -> None:
    assert _produced_no_work(_parsed(output_total=12), _request()) is False


def test_resumed_run_with_zero_per_run_delta_fires() -> None:
    # Cumulative output equals the baseline → the resumed run produced no new output → fires,
    # even though the cumulative output_total is far from 0.
    parsed = _parsed(output_total=8329)
    assert _produced_no_work(parsed, _request(session_id="s", baseline=8329)) is True


def test_resumed_run_with_new_output_does_not_fire() -> None:
    parsed = _parsed(output_total=9364)
    assert _produced_no_work(parsed, _request(session_id="s", baseline=8329)) is False


def test_baseline_ignored_when_session_dropped() -> None:
    # A stale baseline on a request whose session_id was cleared is inert: the guard reads the
    # absolute output (here nonzero) and does not fire.
    parsed = _parsed(output_total=8329)
    assert _produced_no_work(parsed, _request(session_id=None, baseline=8329)) is False


def test_absent_usage_never_fires() -> None:
    assert _produced_no_work(_parsed(output_total=None, with_usage=False), _request()) is False


def test_absent_output_count_never_fires() -> None:
    assert _produced_no_work(_parsed(output_total=None), _request()) is False


def test_max_turns_stop_is_work() -> None:
    parsed = _parsed(output_total=0, failure_subtype=MAX_TURNS_SUBTYPE)
    assert _produced_no_work(parsed, _request()) is False


def test_structured_output_is_work() -> None:
    parsed = _parsed(output_total=0, structured_output={"summary": "done"})
    assert _produced_no_work(parsed, _request()) is False
