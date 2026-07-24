"""Per-node observability writers (P1.4): rendered-prompt, prompt-audit, provider_attempts.

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
    # primary codex failed (infra), fallback claude succeeded.
    attempts = (
        ProviderAttempt(
            provider=ProviderId.CODEX,
            attempt=1,
            status=RunStatus.FAILED,
            error_class=ErrorClass.RATE_LIMITED,
            result=_result("codex", RunStatus.FAILED),
        ),
        ProviderAttempt(
            provider=ProviderId.CLAUDE,
            attempt=2,
            status=RunStatus.SUCCEEDED,
            error_class=None,
            result=_result("claude", RunStatus.SUCCEEDED),
        ),
    )
    return StageOutcome(
        route=_route(),
        result=_result("claude", RunStatus.SUCCEEDED),
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


def test_write_prompt_audit_step_timeline_who_metadata_and_redaction(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str]] = []
    write_prompt_audit(
        artifacts_root=str(tmp_path),
        task_id="task-1",
        node_id="implementation",
        subtask=None,
        run_id=7,
        prompt="do the thing with TOKEN_ABC123",
        route=_route(),
        outcome=_outcome(),
        model="gpt-x",
        reasoning="high",
        started_at="t0",
        secrets=("TOKEN_ABC123",),
        register=_register(calls),
    )
    audit_dir = task_artifact_dir(tmp_path, "task-1") / "prompt-audit"
    step = audit_dir / "000007-implementation.json"
    record = json.loads(step.read_text("utf-8"))
    assert record["provider_used"] == "claude"
    assert record["model"] == "gpt-x"
    # The effective reasoning (post-override) is auditable alongside the model (ADR Q#4).
    assert record["reasoning"] == "high"
    # who-metadata: primary codex marked fallback=False, claude fallback=True.
    by_provider = {a["provider"]: a for a in record["agents"]}
    assert by_provider["codex"]["is_fallback"] is False
    assert by_provider["claude"]["is_fallback"] is True
    assert by_provider["codex"]["error_class"] == "rate_limited"
    assert "TOKEN_ABC123" not in record["prompt"]  # redacted
    assert (audit_dir / "timeline.jsonl").read_text("utf-8").count("\n") == 1
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
    # VF-8: every row carries the owning task so a roll-up needs no ``node_runs`` join.
    assert [r.task_id for r in store.rows] == ["task-1", "task-1"]
    assert store.rows[0].error_class == "rate_limited"
    assert store.rows[0].exit_code == 1


def test_record_provider_attempts_supervisor_layer_has_null_node_run() -> None:
    # VF-8: the constant supervisor layer records with node_run_id None (it is not a graph node).
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
