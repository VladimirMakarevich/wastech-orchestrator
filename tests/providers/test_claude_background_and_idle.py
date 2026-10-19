"""Claude runs that end without their work, or end and do not exit.

Both shapes come from the ``enrich-07b`` run. ``thinkers`` and ``stories`` launched four
backgrounded subagents each, ended their turn "waiting for the results", and the CLI stopped every
one of them at exit — an 87-character promise accepted as done, about $25 spent. ``sources_gate``
run 99 wrote its final structured result at 16:15:25 with nothing left running, and the process
then sat idle until 16:34:17. The fake CLI reproduces both on any OS.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
from wastech_orchestrator.providers.base import (
    FALLBACK_ELIGIBLE,
    PARK_ELIGIBLE,
    AgentRunRequest,
    ErrorClass,
    ProviderError,
    RunStatus,
)
from wastech_orchestrator.providers.claude import (
    ClaudeCodeProvider,
    ClaudeSettleTracker,
    parse_stream_json,
)
from wastech_orchestrator.providers.process import SettleWatch, SettlingContainment, run_process


@pytest.fixture(autouse=True)
def _make_clone(tmp_path: Path) -> None:
    (tmp_path / "clone").mkdir(exist_ok=True)


def _config(command: str, *, background_tasks: bool = False) -> ProviderConfig:
    return ProviderConfig(
        command=command,
        model="",
        timeout_seconds=7200,
        permission_profile="workspace-write",
        extra_args=(),
        background_tasks=background_tasks,
    )


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _started(task_id: str, *, task_type: str = "local_agent") -> dict[str, object]:
    return {
        "type": "system",
        "subtype": "task_started",
        "task_id": task_id,
        "description": f"work {task_id}",
        "is_backgrounded": True,
        "task_type": task_type,
    }


def _ended(task_id: str, status: str) -> dict[str, object]:
    return {"type": "system", "subtype": "task_notification", "task_id": task_id, "status": status}


def _result(text: str = "done", **extra: object) -> dict[str, object]:
    return {"type": "result", "subtype": "success", "is_error": False, "result": text, **extra}


# -- what the stream says ------------------------------------------------------------------------


def test_backgrounded_subagents_stopped_at_exit_are_reported_as_lost() -> None:
    parsed = parse_stream_json(
        _stream(
            _started("a"),
            _started("b"),
            _ended("a", "stopped"),
            _ended("b", "stopped"),
            _result("Four searches are running. I will wait for them."),
        )
    )
    assert parsed.succeeded is True
    assert parsed.abandoned_work == ("work a (stopped)", "work b (stopped)")


def test_backgrounded_subagents_that_finished_are_not_lost() -> None:
    # sources_gate run 99: six background verifiers, every one completed (one failed on its own,
    # which is the subagent's verdict, not lost work).
    parsed = parse_stream_json(
        _stream(
            _started("a"), _started("b"), _ended("a", "completed"), _ended("b", "failed"), _result()
        )
    )
    assert parsed.abandoned_work == ()


def test_a_backgrounded_shell_reaped_at_exit_is_housekeeping() -> None:
    parsed = parse_stream_json(
        _stream(_started("s", task_type="local_bash"), _ended("s", "stopped"), _result())
    )
    assert parsed.abandoned_work == ()


def test_the_subagent_tally_counts_when_task_events_are_missing() -> None:
    parsed = parse_stream_json(
        _stream(_result(subagent_stats={"started_in_background": 3, "killed": {"system": 3}}))
    )
    assert len(parsed.abandoned_work) == 3


def test_the_last_result_decides_but_the_structured_output_survives_a_closing_prose_turn() -> None:
    # run 99: several results, the last a prose turn after the StructuredOutput call.
    parsed = parse_stream_json(
        _stream(
            _result('{"findings": []}', structured_output={"findings": [{"what": "x"}]}),
            _result("Check finished, 1 finding sent."),
        )
    )
    assert parsed.final_message == "Check finished, 1 finding sent."
    assert parsed.structured_output == {"findings": [{"what": "x"}]}


def test_tokens_cover_every_model_the_session_used_not_the_last_turn() -> None:
    # `usage` is the main agent's last turn; `modelUsage` is the whole session, subagents included,
    # and its costUSD values sum to total_cost_usd. thinkers: 0.41 M tokens beside $14.33.
    parsed = parse_stream_json(
        _stream(
            _result(
                usage={
                    "input_tokens": 10,
                    "cache_read_input_tokens": 349_800,
                    "output_tokens": 6_072,
                },
                modelUsage={
                    "claude-opus": {
                        "inputTokens": 588,
                        "outputTokens": 106_667,
                        "cacheReadInputTokens": 40_896_379,
                        "cacheCreationInputTokens": 446_577,
                        "costUSD": 12.74,
                    },
                    "claude-haiku": {
                        "inputTokens": 3_021_199,
                        "outputTokens": 182_774,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                        "costUSD": 1.59,
                    },
                },
                total_cost_usd=14.33,
            )
        )
    )
    usage = parsed.normalized_usage
    assert usage is not None
    assert usage.output_total == 106_667 + 182_774
    assert usage.cache_read == 40_896_379
    assert usage.input_total == 588 + 3_021_199 + 40_896_379 + 446_577
    assert usage.cost == 14.33


def test_without_model_usage_the_main_agent_usage_is_still_read() -> None:
    parsed = parse_stream_json(_stream(_result(usage={"input_tokens": 4, "output_tokens": 9})))
    assert parsed.normalized_usage is not None
    assert parsed.normalized_usage.output_total == 9


# -- what the adapter does with it -------------------------------------------------------------


def test_a_run_that_lost_its_background_work_is_not_accepted_as_done(
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    provider = ClaudeCodeProvider(
        _config(fake_cli("background_stopped", "claude")),
        security=integration_security,
        artifacts_root=tmp_path,
    )
    with pytest.raises(ProviderError) as excinfo:
        provider.run(make_request())
    assert excinfo.value.error_class is ErrorClass.BACKGROUND_WORK_LOST
    assert "Research group 1 (stopped)" in str(excinfo.value)
    # Not something a fallback provider or a park can paper over: the stage stops with the reason.
    assert ErrorClass.BACKGROUND_WORK_LOST not in FALLBACK_ELIGIBLE
    assert ErrorClass.BACKGROUND_WORK_LOST not in PARK_ELIGIBLE
    # The money was spent either way, so the failed attempt still carries what it cost.
    failed = excinfo.value.result
    assert failed is not None and failed.status is RunStatus.FAILED
    assert failed.normalized_usage is not None and failed.normalized_usage.cost == 4.5


def test_background_tasks_are_switched_off_for_the_cli_by_default(
    security_config: SecurityConfig, tmp_path: Path
) -> None:
    provider = ClaudeCodeProvider(
        _config("claude"), security=security_config, artifacts_root=tmp_path
    )
    assert provider._augment_child_env({})["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_an_operator_can_opt_back_into_background_tasks(
    security_config: SecurityConfig, tmp_path: Path
) -> None:
    provider = ClaudeCodeProvider(
        _config("claude", background_tasks=True), security=security_config, artifacts_root=tmp_path
    )
    assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" not in provider._augment_child_env({})


# -- the idle tail after the final result ----------------------------------------------------------


def _feed(tracker: ClaudeSettleTracker, *events: dict[str, object]) -> None:
    for event in events:
        tracker.feed(json.dumps(event))


def test_the_stream_is_settled_once_a_result_is_last_and_nothing_runs() -> None:
    tracker = ClaudeSettleTracker()
    assert tracker.settled is False
    _feed(tracker, _result(), {"type": "system", "subtype": "post_turn_summary"})
    assert tracker.settled is True


def test_a_result_with_a_subagent_still_out_is_not_the_last_one() -> None:
    # The CLI wakes the agent when the subagent reports, so this result is not final yet.
    tracker = ClaudeSettleTracker()
    _feed(tracker, _started("a"), _result())
    assert tracker.settled is False
    _feed(tracker, _ended("a", "completed"))
    assert tracker.settled is False  # the wake-up turn is still owed
    _feed(tracker, {"type": "assistant", "message": {}}, _result())
    assert tracker.settled is True


def test_a_new_command_after_the_result_reopens_the_run() -> None:
    tracker = ClaudeSettleTracker()
    _feed(tracker, _result(), {"type": "command_lifecycle", "state": "started"})
    assert tracker.settled is False


def test_a_cli_idle_after_its_result_is_stopped_and_the_result_used(
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    provider = ClaudeCodeProvider(
        _config(fake_cli("result_then_idle", "claude")),
        security=integration_security,
        artifacts_root=tmp_path,
        result_idle_seconds=0.5,
    )
    started = time.monotonic()
    result = provider.run(make_request(timeout_seconds=60))
    elapsed = time.monotonic() - started
    # The fake sleeps 30 s after its result; the watch stops it within seconds instead.
    assert elapsed < 20
    assert result.status is RunStatus.SUCCEEDED
    assert result.final_message == "Fake implemented the task."


def test_the_settling_containment_leaves_a_busy_child_alone(tmp_path: Path) -> None:
    # Output that never reads as settled is never cut short, however quiet the child is.
    script = tmp_path / "child.py"
    script.write_text(
        'import sys, time\nprint(\'{"type": "assistant"}\', flush=True)\ntime.sleep(1.5)\n',
        encoding="utf-8",
    )
    stdout = tmp_path / "out.log"
    watch = SettleWatch(make_tracker=ClaudeSettleTracker, idle_seconds=0.2, poll_seconds=0.05)
    containment = SettlingContainment(watch, stdout)
    proc = run_process(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=_child_env(),
        timeout_seconds=30,
        stdout_path=stdout,
        make_containment=lambda: containment,
    )
    assert proc.exit_code == 0
    assert containment.settled.is_set() is False


def _child_env() -> dict[str, str]:
    """The minimal environment a Python child needs on every OS (Windows wants SYSTEMROOT)."""
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")
    return {name: os.environ[name] for name in keep if name in os.environ}
