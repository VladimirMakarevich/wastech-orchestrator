"""Shared provider-integration matrix against a fake CLI (tests, Phase 3).

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

# Every test here is a slow integration test (real git / subprocess / process tree).
pytestmark = pytest.mark.slow

PROVIDERS = ("codex", "claude")


@pytest.fixture(autouse=True)
def _make_clone(tmp_path: Path) -> None:
    # The request's working_directory must exist for the subprocess cwd.
    (tmp_path / "clone").mkdir(exist_ok=True)


# The version/capability probes launch a real (fake-CLI) subprocess. Under `pytest -n auto` the
# machine can be saturated by 12 workers, so the production 10 s probe ceiling is granted a generous
# budget here to keep these integration tests deterministic (the fake always returns in well under
# a second — this only guards against pathological scheduling starvation, never real slowness).
_PROBE_TIMEOUT_UNDER_LOAD = 120.0


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
        )
        return CodexProvider(
            config,
            security=security,
            artifacts_root=artifacts_root,
            preflight_timeout_seconds=_PROBE_TIMEOUT_UNDER_LOAD,
        )
    config = ProviderConfig(
        command=command,
        model="",
        timeout_seconds=7200,
        permission_profile="workspace-write",
        extra_args=(),
    )
    return ClaudeCodeProvider(
        config,
        security=security,
        artifacts_root=artifacts_root,
        preflight_timeout_seconds=_PROBE_TIMEOUT_UNDER_LOAD,
    )


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
        tmp_path / "logs" / "task-001" / "stages" / "planning" / "run-000001" / f"1-{provider_name}"
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


def test_claude_error_max_turns_surfaces_structured_subtype(
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Claude-only: exhausting the turn cap is a returned TASK_FAILURE (with the subtype surfaced
    # structurally — the max-turns gate's trigger), never a raised crash, even on a non-zero exit.
    provider = _build(
        "claude", fake_cli("error_max_turns", "claude"), integration_security, tmp_path
    )
    result = provider.run(make_request())
    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.error_class is ErrorClass.TASK_FAILURE
    assert result.error.failure_subtype == "error_max_turns"
    assert result.session_id == "sess-fake"


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
def test_session_limit_raises_rate_limited(
    provider_name: str,
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # The subscription/session-limit terminal must be RAISED as RATE_LIMITED (so the Router can fall
    # over / the orchestrator can park), NOT returned as a quality TASK_FAILURE. Claude surfaces it
    # structurally on stdout (HTTP 429 / rate_limit_event / banner, empty stderr); codex only on
    # stderr. Both must classify identically — the exact failure that both post-mortems mis-labeled.
    provider = _build(
        provider_name, fake_cli("session_limit", provider_name), integration_security, tmp_path
    )
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.RATE_LIMITED


@pytest.mark.parametrize("provider_name", PROVIDERS)
def test_no_work_raises_agent_no_progress(
    provider_name: str,
    fake_cli: Callable[..., str],
    integration_security: SecurityConfig,
    tmp_path: Path,
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # EXPERIMENTAL(no-work-infra) — remove with the feature.
    # A parseable terminal event that did ZERO work (non-success, output_tokens 0, no structured
    # output, not max_turns, no rate-limit signature) is the GENERIC no-work net: it must be RAISED
    # as AGENT_NO_PROGRESS (so the Router falls over / the orchestrator fails it), NOT returned as a
    # quality TASK_FAILURE. Both dialects classify identically off the normalized fields.
    provider = _build(
        provider_name, fake_cli("no_work", provider_name), integration_security, tmp_path
    )
    with pytest.raises(ProviderError) as exc:
        provider.run(make_request())
    assert exc.value.error_class is ErrorClass.AGENT_NO_PROGRESS


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
