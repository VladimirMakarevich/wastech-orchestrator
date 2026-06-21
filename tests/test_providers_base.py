"""Smoke tests for the provider contract. They verify that the basic invariants are in place."""

from __future__ import annotations

from wastech_orchestrator.providers.base import (
    FALLBACK_ELIGIBLE,
    AgentProvider,
    AgentRunRequest,
    ErrorClass,
    ProviderError,
    ProviderId,
    Stage,
)


def test_stage_values_are_canonical() -> None:
    assert {s.value for s in Stage} == {
        "refinement",
        "planning",
        "implementation",
        "testing",
        "review",
        "fixing",
        "summary",
        "publishing",
    }


def test_provider_ids_are_canonical() -> None:
    assert {p.value for p in ProviderId} == {"codex", "claude"}


def test_quality_errors_are_not_fallback_eligible() -> None:
    # Quality/configuration errors must not trigger a provider switch (spec §7.3).
    for ec in (ErrorClass.TASK_FAILURE, ErrorClass.CONFIGURATION_ERROR):
        assert ec not in FALLBACK_ELIGIBLE


def test_infra_error_is_fallback_eligible() -> None:
    err = ProviderError(ErrorClass.TIMEOUT, "timed out")
    assert err.is_fallback_eligible is True


def test_conditional_errors_excluded_from_unconditional_set() -> None:
    # authorization_failed / permission_denied are a conditional fallback, decided by the Router.
    assert ErrorClass.AUTHORIZATION_FAILED not in FALLBACK_ELIGIBLE
    assert ErrorClass.PERMISSION_DENIED not in FALLBACK_ELIGIBLE


def test_request_is_constructible() -> None:
    req = AgentRunRequest(
        task_id="task-001",
        stage=Stage.PLANNING,
        working_directory="./workspace/repo",
        prompt="...",
        permission_profile="workspace-write",
        timeout_seconds=7200,
        attempt=1,
        node_run_id=42,
    )
    assert req.extra_args == []
    assert req.node_run_id == 42


def test_protocol_runtime_checkable() -> None:
    class Dummy:
        id = "dummy"

        def preflight(self):  # type: ignore[no-untyped-def]
            ...

        def run(self, request):  # type: ignore[no-untyped-def]
            ...

    assert isinstance(Dummy(), AgentProvider)
