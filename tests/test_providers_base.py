"""Smoke tests for the provider contract. They verify that the basic invariants are in place."""

from __future__ import annotations

from dataclasses import replace

from wastech_orchestrator.providers.base import (
    FALLBACK_ELIGIBLE,
    AgentProvider,
    AgentRunRequest,
    ErrorClass,
    ProviderError,
    ProviderId,
    build_context_footer,
    build_effective_prompt,
)

_SHA = "3f9c2a7e1d4b5a6c8e0f1a2b3c4d5e6f7a8b9c0d"


def test_provider_ids_are_canonical() -> None:
    assert {p.value for p in ProviderId} == {"codex", "claude"}


def test_quality_errors_are_not_fallback_eligible() -> None:
    # Quality/configuration errors must not trigger a provider switch.
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
        node_id="planning",
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


def _request(**kwargs: object) -> AgentRunRequest:
    return AgentRunRequest(
        task_id="task-001",
        node_id="review",
        working_directory="/repo",
        prompt="ROLE",
        permission_profile="read-only",
        timeout_seconds=60,
        attempt=1,
        node_run_id=1,
        task_path="/x/task.md",
        diff_path="/x/current.diff",
        review_artifacts_path="/x/findings.json",
        **kwargs,  # type: ignore[arg-type]
    )


def test_footer_without_the_change_fields_is_unchanged() -> None:
    # A node before the first edit, and an attempt handed a partial diff, see the footer they
    # always did, byte for byte.
    assert build_context_footer(_request()) == (
        "Context files (read them as needed; do not assume their contents):\n"
        "- task: /x/task.md\n"
        "- diff: /x/current.diff\n"
        "- review: /x/findings.json"
    )


def test_footer_lists_the_overview_before_the_diff_then_the_base_and_the_guidance() -> None:
    request = _request(
        change_overview_path="/x/change-overview.txt",
        diff_base=_SHA,
        change_guidance="Start from the change overview.",
    )
    assert build_context_footer(request) == (
        "Context files (read them as needed; do not assume their contents):\n"
        "- task: /x/task.md\n"
        "- change overview: /x/change-overview.txt\n"
        "- diff: /x/current.diff\n"
        "- review: /x/findings.json\n"
        "\n"
        f"Change base: {_SHA}\n"
        "Start from the change overview."
    )
    # The footer stays last in the stdin prompt, after the role text.
    assert build_effective_prompt(request).startswith("ROLE\n\nContext files")


def test_footer_states_the_base_even_without_context_paths() -> None:
    request = replace(_request(), task_path=None, diff_path=None, review_artifacts_path=None)
    assert build_context_footer(replace(request, diff_base=_SHA)) == f"Change base: {_SHA}"
    assert build_context_footer(request) == ""
