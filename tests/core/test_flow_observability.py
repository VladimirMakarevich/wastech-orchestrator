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
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.base import (
    AgentRunResult,
    ErrorClass,
    ProviderId,
    RunStatus,
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
        subtask=None,
        prompt="prompt with SECRET_XYZ inside",
        secrets=("SECRET_XYZ",),
        register=_register(calls),
    )
    stages = task_artifact_dir(tmp_path, "task-1") / "stages" / "implementation"
    path = stages / "rendered-prompt.md"
    assert "SECRET_XYZ" not in path.read_text("utf-8")
    assert calls == [("task-1", "rendered_prompt", str(path))]


def test_record_provider_attempts_writes_one_row_per_attempt() -> None:
    store = _FakeStore()
    record_provider_attempts(_Services(store), run_id=7, outcome=_outcome())
    assert [r.provider for r in store.rows] == ["codex", "claude"]
    assert [r.node_run_id for r in store.rows] == [7, 7]
    assert store.rows[0].error_class == "rate_limited"
    assert store.rows[0].exit_code == 1


class _Services:
    """Minimal stand-in exposing the two fields record_provider_attempts reads."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.clock = lambda: "ts"
