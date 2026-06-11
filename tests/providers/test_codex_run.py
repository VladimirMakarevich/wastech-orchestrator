"""Unit tests for CodexProvider.run() / preflight() with an injected process runner.

No real Codex binary and no subprocess: ``run_process`` is replaced by a deterministic fake that
writes a canned stdout/last-message and returns a chosen :class:`ProcessResult`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
from wastech_orchestrator.providers.base import (
    FALLBACK_ELIGIBLE,
    AgentProvider,
    AgentRunRequest,
    ErrorClass,
    ProviderError,
    RunStatus,
)
from wastech_orchestrator.providers.codex import CodexProvider
from wastech_orchestrator.providers.process import ProcessResult

FIXED_TIME = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
FAKE_GH_TOKEN = "ghp_" + "abcdef0123456789abcdef0123"


def _success_stream(status: str = "success") -> str:
    events = [
        {"type": "session", "session_id": "sess-99"},
        {"type": "message", "role": "assistant", "text": "stream message"},
        {"type": "usage", "input_tokens": 10, "output_tokens": 5},
        {"type": "result", "status": status, "output": {"summary": "ok"}},
    ]
    return "\n".join(json.dumps(e) for e in events)


@dataclass
class FakeRun:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    launch_error: str | None = None
    last_message: str | None = None
    calls: int = 0
    captured: dict[str, Any] = field(default_factory=dict)

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Any,
        env: Any,
        timeout_seconds: int,
        stdout_path: Any,
        stdin_text: str | None = None,
        monotonic: Any = None,
    ) -> ProcessResult:
        self.calls += 1
        self.captured = {"argv": list(argv), "stdin_text": stdin_text, "env": dict(env)}
        Path(stdout_path).write_text(self.stdout, encoding="utf-8")
        if self.last_message is not None:
            Path(stdout_path).parent.joinpath("last-message.txt").write_text(
                self.last_message, encoding="utf-8"
            )
        code = None if (self.timed_out or self.launch_error is not None) else self.exit_code
        return ProcessResult(
            exit_code=code,
            timed_out=self.timed_out,
            launch_error=self.launch_error,
            duration_seconds=0.5,
            stdout_path=str(stdout_path),
            stderr_text=self.stderr,
        )


def _provider(
    config: ProviderConfig,
    security: SecurityConfig,
    artifacts_root: Path,
    fake: FakeRun,
) -> CodexProvider:
    return CodexProvider(
        config,
        security=security,
        artifacts_root=artifacts_root,
        clock=lambda: FIXED_TIME,
        run_process=fake,
    )


def _attempt_dir(root: Path) -> Path:
    return root / "logs" / "task-001" / "stages" / "planning" / "1-codex"


def test_implements_agent_provider_protocol(
    codex_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    provider = _provider(codex_config, security_config, tmp_path, FakeRun())
    assert isinstance(provider, AgentProvider)
    assert provider.id == "codex"


def test_successful_run(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream(), last_message="Implemented the feature.")
    provider = _provider(codex_config, security_config, tmp_path, fake)
    result = provider.run(make_request())

    assert result.status is RunStatus.SUCCEEDED
    assert result.error is None
    assert result.session_id == "sess-99"
    assert result.final_message == "Implemented the feature."  # last-message file wins
    assert result.structured_output == {"summary": "ok"}
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}

    attempt = _attempt_dir(tmp_path)
    for name in ("request.json", "stdout.log", "stderr.log", "events.jsonl", "result.json"):
        assert (attempt / name).exists(), name


def test_clean_run_with_failure_status_returns_failed_not_raised(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream(status="failed"))
    provider = _provider(codex_config, security_config, tmp_path, fake)
    result = provider.run(make_request())
    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.error_class is ErrorClass.TASK_FAILURE
    # task_failure is never fallback-eligible (it goes to the fixing stage, not another provider).
    assert ErrorClass.TASK_FAILURE not in FALLBACK_ELIGIBLE


def test_timeout_raises_and_writes_result(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(timed_out=True)
    provider = _provider(codex_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.TIMEOUT
    assert exc.value.is_fallback_eligible is True
    # The result artifact is written before the raise (audit trail).
    result_json = json.loads((_attempt_dir(tmp_path) / "result.json").read_text(encoding="utf-8"))
    assert result_json["error"]["error_class"] == "timeout"


def test_missing_binary_raises_binary_not_found(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(launch_error="could not launch 'codex'")
    provider = _provider(codex_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.BINARY_NOT_FOUND


def test_rate_limit_stderr_raises_rate_limited(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(exit_code=1, stderr="Error: rate limit exceeded (429)")
    provider = _provider(codex_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.RATE_LIMITED


def test_invalid_output_raises_invalid_output(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout="this is not jsonl at all", exit_code=0)
    provider = _provider(codex_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.INVALID_OUTPUT


def test_configuration_error_raises_before_launch(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    from dataclasses import replace

    bad = replace(codex_config, extra_args=("--dangerously-bypass-approvals-and-sandbox",))
    fake = FakeRun(stdout=_success_stream())
    provider = _provider(bad, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    assert fake.calls == 0  # never launched
    # The request artifact is still written for the audit trail.
    assert (_attempt_dir(tmp_path) / "request.json").exists()


def test_prompt_is_delivered_via_stdin_not_argv(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    sentinel = "UNIQUE-PROMPT-SENTINEL-7788"
    fake = FakeRun(stdout=_success_stream(), last_message="done")
    provider = _provider(codex_config, security_config, tmp_path, fake)
    provider.run(make_request(prompt=sentinel))
    assert sentinel in fake.captured["stdin_text"]
    assert all(sentinel not in token for token in fake.captured["argv"])


def test_stderr_is_redacted_in_artifact(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(
        stdout=_success_stream(),
        last_message="done",
        stderr=f"warning: token leaked {FAKE_GH_TOKEN}",
    )
    provider = _provider(codex_config, security_config, tmp_path, fake)
    provider.run(make_request())
    stderr_log = (_attempt_dir(tmp_path) / "stderr.log").read_text(encoding="utf-8")
    assert FAKE_GH_TOKEN not in stderr_log
    assert "[REDACTED]" in stderr_log


def test_request_json_redacts_prompt_secret(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream(), last_message="done")
    provider = _provider(codex_config, security_config, tmp_path, fake)
    provider.run(make_request(prompt=f"here is a token {FAKE_GH_TOKEN} do not leak"))
    request_json = (_attempt_dir(tmp_path) / "request.json").read_text(encoding="utf-8")
    assert FAKE_GH_TOKEN not in request_json


def test_preflight_reports_version_when_binary_runs(
    codex_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    fake = FakeRun(stdout="codex-cli 1.2.3\n", exit_code=0)
    provider = _provider(codex_config, security_config, tmp_path, fake)
    health = provider.preflight()
    assert health.executable_found is True
    assert health.version == "1.2.3"
    assert health.provider_id == "codex"


def test_preflight_missing_binary(
    codex_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    fake = FakeRun(launch_error="not found")
    provider = _provider(codex_config, security_config, tmp_path, fake)
    health = provider.preflight()
    assert health.executable_found is False
    assert health.version is None
