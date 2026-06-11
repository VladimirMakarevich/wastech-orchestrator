"""Smoke-тесты контракта провайдера. Проверяют, что базовые инварианты на месте."""

from __future__ import annotations

from wastech_orchestrator.providers.base import (
    FALLBACK_ELIGIBLE,
    AgentProvider,
    AgentRunRequest,
    ErrorClass,
    ProviderError,
    Stage,
)


def test_stage_values_are_canonical() -> None:
    assert {s.value for s in Stage} == {
        "planning",
        "implementation",
        "testing",
        "review",
        "fixing",
        "publishing",
    }


def test_quality_errors_are_not_fallback_eligible() -> None:
    # Ошибки качества/конфигурации не должны вызывать смену провайдера (спек §7.3).
    for ec in (ErrorClass.TASK_FAILURE, ErrorClass.CONFIGURATION_ERROR):
        assert ec not in FALLBACK_ELIGIBLE


def test_infra_error_is_fallback_eligible() -> None:
    err = ProviderError(ErrorClass.TIMEOUT, "timed out")
    assert err.is_fallback_eligible is True


def test_conditional_errors_excluded_from_unconditional_set() -> None:
    # authorization_failed / permission_denied — условный fallback, решает Router.
    assert ErrorClass.AUTHORIZATION_FAILED not in FALLBACK_ELIGIBLE
    assert ErrorClass.PERMISSION_DENIED not in FALLBACK_ELIGIBLE


def test_request_is_constructible() -> None:
    req = AgentRunRequest(
        task_id="task-001",
        stage=Stage.PLANNING,
        working_directory="./workspace/repo",
        prompt="...",
        permission_profile="workspace-write",
        timeout_seconds=1800,
        attempt=1,
    )
    assert req.extra_args == []


def test_protocol_runtime_checkable() -> None:
    class Dummy:
        id = "dummy"

        def preflight(self):  # type: ignore[no-untyped-def]
            ...

        def run(self, request):  # type: ignore[no-untyped-def]
            ...

    assert isinstance(Dummy(), AgentProvider)
