"""Shared provider-integration matrix against a fake CLI (spec §4.4 tests, Phase 3).

The same scenario matrix runs against **both** ``codex`` and ``claude`` via the dialect-aware
``fake_cli`` stub, proving the two adapters are behaviourally interchangeable behind the
``AgentProvider`` contract. These exercise the *real* process runner (no injected fake) by pointing
``ProviderConfig.command`` at a deterministic stub launcher. No real CLI, no network.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
from wastech_orchestrator.providers.base import (
    AgentProvider,
    AgentRunRequest,
    ErrorClass,
    ProviderError,
    RunStatus,
)
from wastech_orchestrator.providers.claude import ClaudeCodeProvider
from wastech_orchestrator.providers.codex import CodexProvider

PROVIDERS = ("codex", "claude")


@pytest.fixture(autouse=True)
def _make_clone(tmp_path: Path) -> None:
    # The request's working_directory must exist for the subprocess cwd.
    (tmp_path / "clone").mkdir(exist_ok=True)


def _build(
    name: str, command: str, security: SecurityConfig, artifacts_root: Path
) -> AgentProvider:
    if name == "codex":
        config = ProviderConfig(
            command=command,
            model="",
            timeout_seconds=7200,
            permission_profile="workspace-write",
            extra_args=(),
            sandbox="workspace-write",
        )
        return CodexProvider(config, security=security, artifacts_root=artifacts_root)
    config = ProviderConfig(
        command=command,
        model="",
        timeout_seconds=7200,
        permission_profile="workspace-write",
        extra_args=(),
    )
    return ClaudeCodeProvider(config, security=security, artifacts_root=artifacts_root)


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_successful_run(
    provider_name: str,
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    provider = _build(
        provider_name, fake_cli("success", provider_name), integration_security, tmp_path
    )
    result = provider.run(make_request())
    # Identical normalized result for both dialects → interchangeability.
    assert result.status is RunStatus.SUCCEEDED
    assert result.session_id == "sess-fake"
    assert result.final_message == "Fake implemented the task."
    assert result.structured_output == {"summary": "fake done"}
    attempt = (
        tmp_path
        / "logs"
        / "task-001"
        / "stages"
        / "planning"
        / "run-000001"
        / f"1-{provider_name}"
    )
    for name in ("request.json", "stdout.log", "stderr.log", "events.jsonl", "result.json"):
        assert (attempt / name).exists(), name


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_task_failure_returns_failed(
    provider_name: str,
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    provider = _build(
        provider_name, fake_cli("task_failure", provider_name), integration_security, tmp_path
    )
    result = provider.run(make_request())
    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.error_class is ErrorClass.TASK_FAILURE


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_binary_not_found(
    provider_name: str,
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    missing = str(tmp_path / f"no-such-{provider_name}-binary")
    provider = _build(provider_name, missing, integration_security, tmp_path)
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.BINARY_NOT_FOUND


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_authentication_failed(
    provider_name: str,
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    provider = _build(
        provider_name, fake_cli("auth_failed", provider_name), integration_security, tmp_path
    )
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.AUTHENTICATION_FAILED


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_rate_limited(
    provider_name: str,
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    provider = _build(
        provider_name, fake_cli("rate_limited", provider_name), integration_security, tmp_path
    )
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.RATE_LIMITED


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_process_crashed(
    provider_name: str,
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    provider = _build(
        provider_name, fake_cli("process_crashed", provider_name), integration_security, tmp_path
    )
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.PROCESS_CRASHED


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_invalid_output(
    provider_name: str,
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    provider = _build(
        provider_name, fake_cli("invalid_output", provider_name), integration_security, tmp_path
    )
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.INVALID_OUTPUT


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_timeout(
    provider_name: str,
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    provider = _build(
        provider_name, fake_cli("timeout", provider_name), integration_security, tmp_path
    )
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request(timeout_seconds=1))
    assert exc.value.error_class is ErrorClass.TIMEOUT


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_preflight_detects_fake_version(
    provider_name: str,
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
) -> None:
    provider = _build(
        provider_name, fake_cli("version", provider_name), integration_security, tmp_path
    )
    health = provider.preflight()
    assert health.executable_found is True
    assert health.version == "1.2.3"


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_implements_agent_provider_protocol(
    provider_name: str,
    integration_security: SecurityConfig,
    tmp_path: Path,
) -> None:
    provider = _build(provider_name, provider_name, integration_security, tmp_path)
    assert isinstance(provider, AgentProvider)
    assert provider.id == provider_name
