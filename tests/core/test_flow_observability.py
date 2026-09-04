"""Per-node observability writers: rendered-prompt, prompt-audit, provider_attempts.

Exercised directly with a hand-built StageOutcome (primary + fallback attempt) so the audit
record's who-metadata, redaction, and the per-attempt rows are pinned independently of the runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wastech_orchestrator.core.flow.observability import (
    record_provider_attempts,
    write_prompt_audit,
    write_rendered_prompt,
)
from wastech_orchestrator.providers.artifacts import node_run_dir, task_artifact_dir
from wastech_orchestrator.providers.base import (
    AgentRunResult,
    ErrorClass,
    NormalizedUsage,
    ProviderId,
    RunStatus,
    UsageScope,
)
from wastech_orchestrator.routing.router import (
    ProviderAttempt,
    ResolvedRoute,
    RouteSource,
    StageOutcome,
)


def _route() -> ResolvedRoute:
    return ResolvedRoute(
        node_id="implementation",
        primary=ProviderId.CODEX,
        fallback=ProviderId.CLAUDE,
        source=RouteSource.CONFIG,
    )


def _result(provider: str, status: RunStatus) -> AgentRunResult:
    return AgentRunResult(
        status=status,
        provider=provider,
        node_id="implementation",
        attempt=1,
        exit_code=0 if status is RunStatus.SUCCEEDED else 1,
        started_at="t0",
        finished_at="t1",
        stdout_path=f"/runs/{provider}/stdout.log",
    )


def _outcome() -> StageOutcome:
    # primary codex failed (infra), fallback claude succeeded — each attempt stamped with what the
    # Router resolved for ITS provider, which is why the two rows do not name the same model.
    settled = _result("claude", RunStatus.SUCCEEDED)
    attempts = (
        ProviderAttempt(
            provider=ProviderId.CODEX,
            attempt=1,
            status=RunStatus.FAILED,
            error_class=ErrorClass.RATE_LIMITED,
            result=_result("codex", RunStatus.FAILED),
            model="gpt-5.5",
            reasoning="xhigh",
        ),
        ProviderAttempt(
            provider=ProviderId.CLAUDE,
            attempt=2,
            status=RunStatus.SUCCEEDED,
            error_class=None,
            result=settled,
            model="claude-opus-5",
            reasoning="high",
        ),
    )
    return StageOutcome(
        route=_route(),
        result=settled,
        provider_used=ProviderId.CLAUDE,
        stage_attempts=2,
        terminal_error=None,
        attempts=attempts,
    )


class _FakeStore:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def record_provider_attempt(self, attempt: Any, conn: Any = None) -> None:
        self.rows.append(attempt)


def _register(calls: list[tuple[str, str, str]]) -> Any:
    return lambda t, k, p: calls.append((t, k, p))


def _step_metadata(step_text: str) -> Any:
    """The step document's fenced ``json`` header, parsed."""
    return json.loads(step_text.split("```json\n", 1)[1].split("\n```", 1)[0])


def test_write_prompt_audit_step_timeline_who_metadata_and_redaction(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str]] = []
    write_prompt_audit(
        artifacts_root=str(tmp_path),
        task_id="task-1",
        node_id="implementation",
        subtask=None,
        run_id=7,
        prompt="do the thing\nwith TOKEN_ABC123",
        route=_route(),
        outcome=_outcome(),
        configured_model=None,  # the node pins neither — the trial's shape for every node but one
        configured_reasoning=None,
        skills_allowed=False,
        skills_required=(),
        started_at="t0",
        secrets=("TOKEN_ABC123",),
        register=_register(calls),
    )
    audit_dir = task_artifact_dir(tmp_path, "task-1") / "prompt-audit"
    step = audit_dir / "000007-implementation.md"
    step_text = step.read_text("utf-8")
    record = _step_metadata(step_text)
    assert record["provider_used"] == "claude"
    # The plain names carry what the settled attempt ACTUALLY ran on, so a node that overrides
    # nothing is not recorded as having run on nothing; the override keeps its own pair of keys.
    assert record["model"] == "claude-opus-5"
    assert record["reasoning"] == "high"
    assert record["model_configured"] is None and record["reasoning_configured"] is None
    # who-metadata: primary codex marked fallback=False, claude fallback=True.
    by_provider = {a["provider"]: a for a in record["agents"]}
    assert by_provider["codex"]["is_fallback"] is False
    assert by_provider["claude"]["is_fallback"] is True
    assert by_provider["codex"]["error_class"] == "rate_limited"
    # Per attempt, not per stage: the failed codex hop is not reported as having run Claude's model.
    assert by_provider["codex"]["model"] == "gpt-5.5"
    assert by_provider["codex"]["reasoning"] == "xhigh"
    assert by_provider["claude"]["model"] == "claude-opus-5"
    # The prompt is the document's body, not a JSON string field: its newlines survive as real
    # newlines an operator can read, and it is out of the metadata block entirely.
    assert "prompt" not in record
    assert step_text.startswith("# implementation — run 000007\n")
    assert step_text.endswith("## Prompt\n\ndo the thing\nwith [REDACTED]\n")
    assert "TOKEN_ABC123" not in step_text  # redacted
    # The timeline stays the machine-readable half: one whole record per line, prompt included.
    timeline = (audit_dir / "timeline.jsonl").read_text("utf-8").splitlines()
    assert len(timeline) == 1
    assert "TOKEN_ABC123" not in json.loads(timeline[0])["prompt"]
    kinds = {k for _, k, _ in calls}
    assert {"prompt_audit", "prompt_audit_timeline"} <= kinds


def test_write_rendered_prompt_redacts_and_registers(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str]] = []
    write_rendered_prompt(
        artifacts_root=str(tmp_path),
        task_id="task-1",
        node_id="implementation",
        run_id=42,
        prompt="prompt with SECRET_XYZ inside",
        secrets=("SECRET_XYZ",),
        register=_register(calls),
    )
    # Per-run: co-located under stages/<node>/run-<id>/ next to the run's provider attempts.
    run_dir = node_run_dir(tmp_path, "task-1", "implementation", 42)
    path = run_dir / "rendered-prompt.md"
    assert "SECRET_XYZ" not in path.read_text("utf-8")
    assert calls == [("task-1", "rendered_prompt", str(path))]


def test_record_provider_attempts_writes_one_row_per_attempt() -> None:
    store = _FakeStore()
    record_provider_attempts(
        store, lambda: "ts", task_id="task-1", node_run_id=7, outcome=_outcome()
    )
    assert [r.provider for r in store.rows] == ["codex", "claude"]
    assert [r.node_run_id for r in store.rows] == [7, 7]
    # Every row carries the owning task so a roll-up needs no ``node_runs`` join.
    assert [r.task_id for r in store.rows] == ["task-1", "task-1"]
    assert store.rows[0].error_class == "rate_limited"
    assert store.rows[0].exit_code == 1


def test_record_provider_attempts_stamps_result_interval_not_clock() -> None:
    # The row carries the attempt's real measured interval (taken from the result), not two
    # identical clock reads at row-write time — so ``SUM(finished_at - started_at)`` is a duration.
    store = _FakeStore()
    record_provider_attempts(
        store, lambda: "ROW-WRITE", task_id="task-1", node_run_id=7, outcome=_outcome()
    )
    for row in store.rows:
        assert row.started_at == "t0"
        assert row.finished_at == "t1"
        assert row.started_at != row.finished_at  # a real interval, never a zero-width stamp
        assert "ROW-WRITE" not in (row.started_at, row.finished_at)


def test_record_provider_attempts_resultless_attempt_falls_back_to_clock() -> None:
    # A fallback attempt that never produced a result has no interval to read — fall back to
    # the clock (both stamps equal is honest here: there is no measured duration).
    store = _FakeStore()
    outcome = StageOutcome(
        route=_route(),
        result=None,
        provider_used=None,
        stage_attempts=1,
        terminal_error=None,
        attempts=(
            ProviderAttempt(
                provider=ProviderId.CODEX,
                attempt=1,
                status=RunStatus.FAILED,
                error_class=ErrorClass.RATE_LIMITED,
                result=None,
            ),
        ),
    )
    record_provider_attempts(
        store, lambda: "CLOCK-TS", task_id="task-1", node_run_id=7, outcome=outcome
    )
    assert store.rows[0].started_at == "CLOCK-TS"
    assert store.rows[0].finished_at == "CLOCK-TS"


def test_record_provider_attempts_supervisor_layer_has_null_node_run() -> None:
    # The constant supervisor layer records with node_run_id None (it is not a graph node).
    store = _FakeStore()
    record_provider_attempts(
        store, lambda: "ts", task_id="task-1", node_run_id=None, outcome=_outcome()
    )
    assert [r.node_run_id for r in store.rows] == [None, None]
    assert [r.task_id for r in store.rows] == ["task-1", "task-1"]


def test_record_provider_attempts_persists_per_run_delta() -> None:
    # A resumed cumulative run persists the summation-safe per-run delta (baseline subtracted) plus
    # the verbatim raw payload — the double-count trap is closed at the persistence boundary.
    store = _FakeStore()
    resume = AgentRunResult(
        status=RunStatus.SUCCEEDED,
        provider="codex",
        node_id="revise",
        attempt=1,
        exit_code=0,
        started_at="t0",
        finished_at="t1",
        session_id="sess-x",
        usage={"input_tokens": 282699},
        normalized_usage=NormalizedUsage(
            scope=UsageScope.SESSION_CUMULATIVE,
            input_total=282699,
            cache_read=187904,
            uncached_input=94795,
            output_total=9364,
            reasoning_output=6066,
        ),
    )
    outcome = StageOutcome(
        route=_route(),
        result=resume,
        provider_used=ProviderId.CODEX,
        stage_attempts=1,
        terminal_error=None,
        attempts=(
            ProviderAttempt(
                provider=ProviderId.CODEX,
                attempt=1,
                status=RunStatus.SUCCEEDED,
                error_class=None,
                result=resume,
            ),
        ),
    )
    baseline = NormalizedUsage(
        scope=UsageScope.SESSION_CUMULATIVE,
        input_total=141464,
        cache_read=76288,
        uncached_input=65176,
        output_total=8329,
        reasoning_output=5935,
    )
    record_provider_attempts(
        store,
        lambda: "ts",
        task_id="task-1",
        node_run_id=9,
        outcome=outcome,
        usage_baseline=baseline,
        baseline_session_id="sess-x",
    )
    row = store.rows[0]
    assert row.usage_scope == "session_cumulative"
    assert row.usage_input_total == 141235
    assert row.usage_output_total == 1035
    assert row.usage_reasoning_output == 131
    assert row.usage_delta_status == "ok"
    assert row.provider_usage_raw == '{"input_tokens":282699}'


def _degraded_outcome() -> StageOutcome:
    """A stage that opened on a live session and then lost it — the case the flag exists for."""
    settled = _result("codex", RunStatus.SUCCEEDED)
    attempts = (
        ProviderAttempt(
            provider=ProviderId.CODEX,
            attempt=1,
            status=None,
            error_class=ErrorClass.SESSION_UNAVAILABLE,
            result=None,
            model="gpt-5.5",
            reasoning="xhigh",
            resumed=True,
        ),
        ProviderAttempt(
            provider=ProviderId.CODEX,
            attempt=2,
            status=RunStatus.SUCCEEDED,
            error_class=None,
            result=settled,
            model="gpt-5.5",
            reasoning="xhigh",
            resumed=False,
        ),
    )
    return StageOutcome(
        route=_route(),
        result=settled,
        provider_used=ProviderId.CODEX,
        stage_attempts=2,
        terminal_error=None,
        attempts=attempts,
    )


def test_prompt_audit_names_the_variant_each_attempt_received(tmp_path: Path) -> None:
    # The record is built from the request the Core assembled, so a stage-level answer would say
    # "continuation" for an attempt that was handed the full text after its session was dropped.
    calls: list[tuple[str, str, str]] = []
    write_prompt_audit(
        artifacts_root=str(tmp_path),
        task_id="task-1",
        node_id="fixing",
        subtask=None,
        run_id=7,
        prompt="FULL TEXT",
        continuation_prompt="CONTINUATION TEXT",
        route=_route(),
        outcome=_degraded_outcome(),
        configured_model=None,
        configured_reasoning=None,
        skills_allowed=False,
        skills_required=(),
        started_at="t0",
        secrets=(),
        register=_register(calls),
    )
    audit_dir = task_artifact_dir(tmp_path, "task-1") / "prompt-audit"
    step_text = (audit_dir / "000007-fixing.md").read_text(encoding="utf-8")
    record = _step_metadata(step_text)

    assert [(a["attempt"], a["resumed"], a["prompt_variant"]) for a in record["agents"]] == [
        (1, True, "continuation"),
        (2, False, "full"),
    ]
    # Both texts, once each, and both as document body: a prompt inside the JSON header would read
    # as one flat line of escaped newlines, which is what the Markdown step file exists to avoid.
    assert "prompt" not in record and "continuation_prompt" not in record
    assert "## Prompt\n\nFULL TEXT" in step_text
    assert "## Continuation prompt\n\nCONTINUATION TEXT" in step_text
    # The machine-readable half still carries the whole record.
    timeline = json.loads((audit_dir / "timeline.jsonl").read_text("utf-8").splitlines()[0])
    assert timeline["prompt"] == "FULL TEXT"
    assert timeline["continuation_prompt"] == "CONTINUATION TEXT"


def test_prompt_audit_omits_the_variant_for_a_node_with_one_text(tmp_path: Path) -> None:
    # Most nodes have nothing to choose between; the record does not grow a key that would always
    # say the same thing.
    calls: list[tuple[str, str, str]] = []
    write_prompt_audit(
        artifacts_root=str(tmp_path),
        task_id="task-1",
        node_id="implementation",
        subtask=None,
        run_id=8,
        prompt="only text",
        route=_route(),
        outcome=_outcome(),
        configured_model=None,
        configured_reasoning=None,
        skills_allowed=False,
        skills_required=(),
        started_at="t0",
        secrets=(),
        register=_register(calls),
    )
    audit_dir = task_artifact_dir(tmp_path, "task-1") / "prompt-audit"
    step_text = (audit_dir / "000008-implementation.md").read_text(encoding="utf-8")
    record = _step_metadata(step_text)
    assert all("prompt_variant" not in agent for agent in record["agents"])
    assert [agent["resumed"] for agent in record["agents"]] == [False, False]
    # One prompt, one body section — the document does not grow an empty second heading.
    assert "## Continuation prompt" not in step_text
    timeline = json.loads((audit_dir / "timeline.jsonl").read_text("utf-8").splitlines()[0])
    assert "continuation_prompt" not in timeline


def test_prompt_audit_records_the_declared_skill_posture(tmp_path: Path) -> None:
    # `skills_allowed` is the fact that is nowhere in the prompt — it rides a CLI flag, not text —
    # so without it the audit could not answer whether the node could invoke a skill at all.
    calls: list[tuple[str, str, str]] = []
    write_prompt_audit(
        artifacts_root=str(tmp_path),
        task_id="task-1",
        node_id="implementation",
        subtask=None,
        run_id=9,
        prompt="p",
        route=_route(),
        outcome=_outcome(),
        configured_model=None,
        configured_reasoning=None,
        skills_allowed=True,
        skills_required=("acme-tdd",),
        started_at="t0",
        secrets=(),
        register=_register(calls),
    )
    audit_dir = task_artifact_dir(tmp_path, "task-1") / "prompt-audit"
    record = _step_metadata((audit_dir / "000009-implementation.md").read_text(encoding="utf-8"))
    assert record["skills_allowed"] is True
    assert record["skills_required"] == ["acme-tdd"]
