"""Unit tests for CodexProvider.run() / preflight() with an injected process runner.

No real Codex binary and no subprocess: ``run_process`` is replaced by a deterministic fake that
writes a canned stdout/last-message and returns a chosen :class:`ProcessResult`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
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
    build_effective_prompt,
)
from wastech_orchestrator.providers.codex import CodexProvider
from wastech_orchestrator.providers.process import ProcessResult
from wastech_orchestrator.runtime_layout import InternalDenyPolicy

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
        recorder: Any = None,
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
    return root / "logs" / "task-001" / "stages" / "planning" / "run-000001" / "1-codex"


@dataclass
class FakeCanary:
    """A scripted ``codex sandbox`` probe runner: returns ``results`` in call order."""

    results: list[tuple[int, str]]
    calls: int = 0

    def __call__(self, argv: list[str], cwd: str, env: Mapping[str, str]) -> tuple[int, str]:
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


def _deny_for(request: AgentRunRequest) -> InternalDenyPolicy:
    wd = Path(request.working_directory)
    return InternalDenyPolicy(
        control_home=wd / ".worc", private_home=wd / ".worc", env_file=None, provider_homes=()
    )


def _isolated_provider(
    config: ProviderConfig,
    security: SecurityConfig,
    root: Path,
    fake: FakeRun,
    *,
    canary: FakeCanary,
    deny: InternalDenyPolicy | None,
) -> CodexProvider:
    return CodexProvider(
        config,
        security=security,
        artifacts_root=root,
        clock=lambda: FIXED_TIME,
        run_process=fake,
        deny_policy=deny,
        canary_runner=canary,
    )


def test_canary_passes_then_run_proceeds_and_writes_evidence(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # With a deny set present, the pre-launch canary runs; the frozen exchange task
    # packet is the mandatory positive control. Probe order: private-read, private-shell-read,
    # exchange-read (allowed), exchange-write (denied). When they hold, the real launch proceeds.
    fake = FakeRun(stdout=_success_stream(), last_message='{"summary":"ok"}')
    canary = FakeCanary(results=[(1, "denied"), (1, "denied"), (0, "task"), (1, "denied")])
    exchange = tmp_path / "task.md"
    exchange.write_text("EXCHANGE_TASK", encoding="utf-8")
    request = make_request(task_path=str(exchange))
    provider = _isolated_provider(
        codex_config, security_config, tmp_path, fake, canary=canary, deny=_deny_for(request)
    )
    result = provider.run(request)
    assert result.status is RunStatus.SUCCEEDED
    assert fake.calls == 1  # launched only AFTER the canary passed
    assert canary.calls == 4  # 2 private-deny + exchange-read (positive control) + exchange-write
    canary_json = _attempt_dir(tmp_path) / "canary.json"
    assert json.loads(canary_json.read_text())["ok"] is True


def test_canary_leak_fails_closed_before_any_model_launch(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # A denied path that reads successfully is a security violation — non-fallback, pre-model.
    fake = FakeRun(stdout=_success_stream())
    canary = FakeCanary(results=[(0, "SECRET LEAKED")])
    request = make_request()
    provider = _isolated_provider(
        codex_config, security_config, tmp_path, fake, canary=canary, deny=_deny_for(request)
    )
    with pytest.raises(ProviderError) as exc:
        provider.run(request)
    assert exc.value.error_class is ErrorClass.CONFIGURATION_ERROR
    assert exc.value.error_class not in FALLBACK_ELIGIBLE
    assert fake.calls == 0  # the model was NEVER launched


def test_canary_capability_failure_is_pre_model(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream())
    canary = FakeCanary(results=[(1, "windows: refusing to run unsandboxed")])
    request = make_request()
    provider = _isolated_provider(
        codex_config, security_config, tmp_path, fake, canary=canary, deny=_deny_for(request)
    )
    with pytest.raises(ProviderError) as exc:
        provider.run(request)
    assert exc.value.error_class is ErrorClass.CAPABILITY_UNAVAILABLE
    assert fake.calls == 0


def test_full_access_escape_skips_canary(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # danger-full-access emits no permission profile, so there is nothing (and no claim) to prove.
    fake = FakeRun(stdout=_success_stream(), last_message='{"summary":"ok"}')
    canary = FakeCanary(results=[(0, "should never be called")])
    request = make_request()
    provider = _isolated_provider(
        replace(codex_config, sandbox="danger-full-access"),
        security_config,
        tmp_path,
        fake,
        canary=canary,
        deny=_deny_for(request),
    )
    result = provider.run(request)
    assert result.status is RunStatus.SUCCEEDED
    assert canary.calls == 0  # skipped
    assert fake.calls == 1


def test_canary_skipped_without_deny_policy(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # No internal deny set to prove (a bare harness) → the canary is not run at all.
    fake = FakeRun(stdout=_success_stream(), last_message='{"summary":"ok"}')
    canary = FakeCanary(results=[(0, "should never be called")])
    provider = _isolated_provider(
        codex_config, security_config, tmp_path, fake, canary=canary, deny=None
    )
    provider.run(make_request())
    assert canary.calls == 0
    assert fake.calls == 1


def test_implements_agent_provider_protocol(
    codex_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    provider = _provider(codex_config, security_config, tmp_path, FakeRun())
    assert isinstance(provider, AgentProvider)
    assert provider.id == "codex"


def test_stdin_is_plain_prompt_without_injection(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Codex no longer injects a repository-instruction block — stdin is just the flow prompt
    # (+ context-file footer); the agent reads the repo's root files itself via native discovery.
    provider = _provider(codex_config, security_config, tmp_path, FakeRun())
    stdin = provider._stdin_text(make_request(prompt="just the task"))
    assert "<repository-instructions>" not in stdin
    assert stdin.startswith("just the task")


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


def test_schema_requested_structured_output_from_last_message(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Smoke-tested against codex-cli 0.139.0: a schema-constrained run's terminal
    # `turn.completed` event carries only `{type, usage}` — no `output` field — so the schema
    # result must come from the `--output-last-message` file instead.
    stream = "\n".join(
        json.dumps(e)
        for e in (
            {"type": "session", "session_id": "sess-99"},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
        )
    )
    fake = FakeRun(stdout=stream, last_message='{"findings": []}')
    provider = _provider(codex_config, security_config, tmp_path, fake)
    schema = {"type": "object", "properties": {"findings": {"type": "array"}}}
    result = provider.run(make_request(output_schema=schema))

    assert result.status is RunStatus.SUCCEEDED
    assert result.structured_output == {"findings": []}
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}


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


_HELPER_STDERR = (
    "ERROR codex_core::exec: windows sandbox: orchestrator_helper_launch_failed: setup refresh "
    "failed to launch helper: helper=codex-windows-sandbox-setup.exe, error=program not found"
)


def test_sandbox_helper_failure_stderr_normalizes_to_permission_denied(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Nonzero exit with no parseable terminal event: classify() matches the Windows sandbox-helper
    # signature and normalizes it into the existing PERMISSION_DENIED infra class.
    fake = FakeRun(exit_code=1, stderr=_HELPER_STDERR)
    provider = _provider(codex_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.PERMISSION_DENIED


def test_false_success_with_helper_stderr_raises_infra_not_succeeded(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # The Windows-10 incident: codex printed a clean terminal SUCCESS (exit 0) while stderr carried
    # the fatal sandbox-helper failure, so the run never touched the workspace. The post-success
    # guard turns that false success into a raised PERMISSION_DENIED (an infra failure the Router
    # falls over on) instead of returning RunStatus.SUCCEEDED.
    fake = FakeRun(
        stdout=_success_stream(),
        exit_code=0,
        stderr=_HELPER_STDERR,
        last_message="Could not create the file: the sandbox helper is broken.",
    )
    provider = _provider(codex_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.PERMISSION_DENIED
    # The failed-attempt artifact is written before the raise, recording the infra class — and
    # crucially NOT the false "succeeded" the incident logged.
    result_json = json.loads((_attempt_dir(tmp_path) / "result.json").read_text(encoding="utf-8"))
    assert result_json["status"] == "failed"
    assert result_json["error"]["error_class"] == "permission_denied"


# The run-000011 incident string: on this Windows host the sandbox could not spawn a child process,
# so EVERY command and the apply_patch write failed with seclogon's CreateProcessWithLogonW — a
# DIFFERENT stderr shape than the setup-helper _HELPER_STDERR above, which the guard must catch too.
_SANDBOX_RUNTIME_STDERR = (
    "ERROR codex_core::exec: exec error: windows sandbox: CreateProcessWithLogonW failed: 2\n"
    "apply_patch verification failed: fs sandbox helper failed with status exit code: 1: "
    "windows sandbox failed: CreateProcessWithLogonW failed: 2"
)


def test_sandbox_runtime_failure_nonzero_exit_normalizes_to_permission_denied(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Nonzero exit with no parseable terminal event: classify() matches the runtime sandbox-child
    # launch failure (CreateProcessWithLogonW / fs sandbox helper) and normalizes to PERMISSION.
    fake = FakeRun(exit_code=1, stderr=_SANDBOX_RUNTIME_STDERR)
    provider = _provider(codex_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.PERMISSION_DENIED


def test_false_success_with_createprocess_stderr_raises_infra(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # The run-000011 incident: codex printed a clean terminal SUCCESS (exit 0) while stderr carried
    # the runtime CreateProcessWithLogonW failure, so the run never touched the workspace (the whole
    # article went only to last-message.txt). The post-success guard must turn this false success
    # into a raised PERMISSION_DENIED so the Router falls over to Claude instead of trusting it.
    fake = FakeRun(
        stdout=_success_stream(),
        exit_code=0,
        stderr=_SANDBOX_RUNTIME_STDERR,
        last_message="Here is the whole article — I could not write it to disk.",
    )
    provider = _provider(codex_config, security_config, tmp_path, fake)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.PERMISSION_DENIED
    result_json = json.loads((_attempt_dir(tmp_path) / "result.json").read_text(encoding="utf-8"))
    assert result_json["status"] == "failed"
    assert result_json["error"]["error_class"] == "permission_denied"


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


def _provider_at_level(
    config: ProviderConfig, security: SecurityConfig, root: Path, fake: FakeRun, level: str
) -> CodexProvider:
    return CodexProvider(
        config,
        security=security,
        artifacts_root=root,
        clock=lambda: FIXED_TIME,
        run_process=fake,
        artifact_level=level,
    )


def test_artifact_level_minimal_prunes_on_success(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream())
    provider = _provider_at_level(codex_config, security_config, tmp_path, fake, "minimal")
    provider.run(make_request())
    survivors = {p.name for p in _attempt_dir(tmp_path).iterdir()}
    assert survivors == {"result.json"}


def test_artifact_level_minimal_is_strict_on_failure(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # A timeout raises, but _finalize_failure still writes result.json and then prunes — minimal is
    # strict: only result.json survives, even on failure (it carries the exit code + error class).
    fake = FakeRun(timed_out=True)
    provider = _provider_at_level(codex_config, security_config, tmp_path, fake, "minimal")
    with pytest.raises(ProviderError):
        provider.run(make_request())
    survivors = {p.name for p in _attempt_dir(tmp_path).iterdir()}
    assert survivors == {"result.json"}
    result_json = json.loads((_attempt_dir(tmp_path) / "result.json").read_text(encoding="utf-8"))
    assert result_json["error"]["error_class"] == "timeout"


def test_artifact_level_standard_keeps_stdout_stderr_result(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream())
    provider = _provider_at_level(codex_config, security_config, tmp_path, fake, "standard")
    provider.run(make_request())
    survivors = {p.name for p in _attempt_dir(tmp_path).iterdir()}
    assert survivors == {"result.json", "stdout.log", "stderr.log"}


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


def test_request_json_prompt_includes_context_footer(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    """``request.json``'s ``"prompt"`` must match what was actually piped to stdin — the
    context-files footer, not just the bare Core-rendered template (audit-trail parity)."""
    fake = FakeRun(stdout=_success_stream(), last_message="done")
    provider = _provider(codex_config, security_config, tmp_path, fake)
    request = make_request(
        prompt="Do the thing.",
        task_path="/t/task.md",
        plan_path="/t/plan.md",
    )
    provider.run(request)
    request_json = json.loads((_attempt_dir(tmp_path) / "request.json").read_text())

    effective_prompt = build_effective_prompt(request)
    assert "Context files (read them as needed" in effective_prompt
    assert request_json["prompt"] == effective_prompt
    assert request_json["prompt"] == fake.captured["stdin_text"]


def test_request_json_context_paths_includes_skill_reference_paths(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    fake = FakeRun(stdout=_success_stream(), last_message="done")
    provider = _provider(codex_config, security_config, tmp_path, fake)
    provider.run(make_request(skill_reference_paths=("/skills/foo/SKILL.md",)))
    request_json = json.loads((_attempt_dir(tmp_path) / "request.json").read_text())
    assert request_json["context_paths"]["skill_reference_paths"] == ["/skills/foo/SKILL.md"]
    assert "/skills/foo/SKILL.md" in request_json["prompt"]


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


def test_raw_session_id_redacted_in_artifacts(
    codex_config: ProviderConfig,
    security_config: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Durable sessions: the raw session id lives ONLY in state.db. The resume id we pass via
    # ``exec resume <id>`` and the freshly emitted id (``sess-99``) must not appear verbatim in any
    # artifact (request argv / stdout / events / result.json) — but the in-memory result keeps the
    # raw emitted id so the orchestrator can persist it to the editing_lineage store.
    fake = FakeRun(stdout=_success_stream(), last_message="done")
    provider = _provider(codex_config, security_config, tmp_path, fake)
    result = provider.run(make_request(session_id="raw-resume-id-1234"))

    assert result.session_id == "sess-99"  # raw emitted id returned in-memory (for state.db only)
    attempt = _attempt_dir(tmp_path)
    blobs = {
        name: (attempt / name).read_text(encoding="utf-8")
        for name in ("request.json", "stdout.log", "events.jsonl", "result.json")
    }
    for name, blob in blobs.items():
        assert "raw-resume-id-1234" not in blob, name  # the resume id never lands on disk
        assert "sess-99" not in blob, name  # the emitted raw id is scrubbed / normalized
    assert "resume" in blobs["request.json"]  # the argv shows the resume subcommand (id redacted)
    assert "session:" in blobs["result.json"]  # result.json records the normalized correlator


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


class _ProbingFakeRun:
    """A fake runner answering ``--version``, ``exec --help`` and ``exec resume --help`` by argv."""

    def __init__(self, *, help_has_config: bool, resume_help: str | None = None) -> None:
        self._help_has_config = help_has_config
        # Canned ``codex exec resume --help`` text; None => the healthy 0.142.x form advertising the
        # -m/--model and -c/--config options this adapter places after ``resume`` (probe).
        self._resume_help = resume_help
        self.argvs: list[list[str]] = []

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
        self.argvs.append(list(argv))
        if "--version" in argv:
            out = "codex-cli 0.139.0\n"
        elif "exec" in argv and "resume" in argv and "--help" in argv:
            # Must be checked before the plain ``exec --help`` branch (that also matches).
            if self._resume_help is None:
                out = (
                    "Usage: codex exec resume [SESSION_ID] [PROMPT]\n"
                    "  -m, --model <M>\n  -c, --config <key=value>\n"
                )
            else:
                out = self._resume_help
        elif "exec" in argv and "--help" in argv:
            out = "Usage: codex exec [OPTIONS]\n  --model <M>\n"
            if self._help_has_config:
                out += "  -c, --config <key=value>\n"
        else:
            out = ""
        Path(stdout_path).write_text(out, encoding="utf-8")
        return ProcessResult(
            exit_code=0,
            timed_out=False,
            launch_error=None,
            duration_seconds=0.1,
            stdout_path=str(stdout_path),
            stderr_text="",
        )


def _codex_config_with_reasoning() -> ProviderConfig:
    return ProviderConfig(
        command="codex",
        model="",
        reasoning="high",
        timeout_seconds=7200,
        permission_profile="workspace-write",
        extra_args=(),
    )


def test_preflight_fails_when_codex_exec_lacks_config_override(
    security_config: SecurityConfig, tmp_path: Path
) -> None:
    # Preflight probes `codex exec --help`; a build whose exec help lacks -c/--config cannot receive
    # the official model_reasoning_effort override and is reported unsupported before a real run.
    fake = _ProbingFakeRun(help_has_config=False)
    provider = CodexProvider(
        _codex_config_with_reasoning(),
        security=security_config,
        artifacts_root=tmp_path,
        clock=lambda: FIXED_TIME,
        run_process=fake,
    )
    health = provider.preflight()
    assert health.supports_required_features is False
    assert "-c/--config" in health.message
    assert "model_reasoning_effort" in health.message


def test_preflight_passes_when_codex_exec_accepts_config_override(
    security_config: SecurityConfig, tmp_path: Path
) -> None:
    fake = _ProbingFakeRun(help_has_config=True)
    provider = CodexProvider(
        _codex_config_with_reasoning(),
        security=security_config,
        artifacts_root=tmp_path,
        clock=lambda: FIXED_TIME,
        run_process=fake,
    )
    health = provider.preflight()
    assert health.supports_required_features is True


def test_preflight_probes_config_support_when_reasoning_unset(
    codex_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    # Network grants also use -c, so the capability probe is independent of configured reasoning.
    fake = _ProbingFakeRun(help_has_config=True)
    provider = CodexProvider(
        codex_config,
        security=security_config,
        artifacts_root=tmp_path,
        clock=lambda: FIXED_TIME,
        run_process=fake,
    )
    health = provider.preflight()
    assert health.supports_required_features is True
    assert any("--help" in argv for argv in fake.argvs)


def test_preflight_no_resume_grammar_drift_on_current_codex(
    codex_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    # `codex exec resume --help` advertising -m/--model and -c/--config (the 0.142.x form)
    # yields no degradation — the resume argv this adapter builds is valid.
    fake = _ProbingFakeRun(help_has_config=True)
    provider = CodexProvider(
        codex_config,
        security=security_config,
        artifacts_root=tmp_path,
        clock=lambda: FIXED_TIME,
        run_process=fake,
    )
    health = provider.preflight()
    assert health.supports_required_features is True
    assert health.degraded_reasons == ()
    assert any("resume" in argv and "--help" in argv for argv in fake.argvs)


def test_preflight_flags_resume_grammar_drift(
    codex_config: ProviderConfig, security_config: SecurityConfig, tmp_path: Path
) -> None:
    # A future `codex exec resume --help` that no longer advertises -m/-c means the options the
    # adapter places after `resume` would be rejected — surfaced as an advisory degradation (fatal
    # only without a fallback; `run_preflight` decides). It is NOT a hard capability block.
    fake = _ProbingFakeRun(
        help_has_config=True, resume_help="Usage: codex exec resume [SESSION_ID]\n"
    )
    provider = CodexProvider(
        codex_config,
        security=security_config,
        artifacts_root=tmp_path,
        clock=lambda: FIXED_TIME,
        run_process=fake,
    )
    health = provider.preflight()
    assert health.supports_required_features is True  # not a hard block on its own
    assert health.degraded_reasons
    assert "resume" in health.degraded_reasons[0]
