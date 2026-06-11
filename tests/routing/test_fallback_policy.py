"""Fallback decision table (§7.2/§7.3): which errors fall back, the conditional rule,
and that a quality failure never triggers fallback (phase doc 4.3)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.base import (
    FALLBACK_ELIGIBLE,
    AgentRunRequest,
    ErrorClass,
    ProviderId,
    RunStatus,
    Stage,
)
from wastech_orchestrator.routing.router import (
    CONDITIONAL_FALLBACK,
    AgentRouter,
    fallback_allowed,
)

WORKSPACE = "workspace-write"
READONLY = "read-only"


@pytest.mark.parametrize("error_class", sorted(FALLBACK_ELIGIBLE, key=lambda e: e.value))
def test_infra_classes_always_fall_back(error_class: ErrorClass) -> None:
    assert fallback_allowed(error_class, primary_profile=WORKSPACE, fallback_profile=WORKSPACE)


@pytest.mark.parametrize(
    "error_class",
    [ErrorClass.CONFIGURATION_ERROR, ErrorClass.TASK_FAILURE],
)
def test_non_fallback_classes_never_fall_back(error_class: ErrorClass) -> None:
    # Even with an equal/stricter profile, a non-infra class is never eligible.
    assert not fallback_allowed(error_class, primary_profile=WORKSPACE, fallback_profile=READONLY)


@pytest.mark.parametrize("error_class", sorted(CONDITIONAL_FALLBACK, key=lambda e: e.value))
@pytest.mark.parametrize(
    "primary_profile,fallback_profile,expected",
    [
        (WORKSPACE, WORKSPACE, True),  # same profile
        (WORKSPACE, READONLY, True),  # fallback is stricter
        (READONLY, WORKSPACE, False),  # fallback is looser → never relax the policy
        (WORKSPACE, "bogus", False),  # unknown candidate → fail-closed
        ("bogus", WORKSPACE, False),  # unknown reference → fail-closed
    ],
)
def test_conditional_auth_permission_rule(
    error_class: ErrorClass,
    primary_profile: str,
    fallback_profile: str,
    expected: bool,
) -> None:
    assert (
        fallback_allowed(
            error_class, primary_profile=primary_profile, fallback_profile=fallback_profile
        )
        is expected
    )


def test_quality_failure_is_not_a_fallback_trigger(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # A returned status=failed (not a raised error) is surfaced as-is for the Core to route to
    # fixing — never retried on the fallback (§7.3, phase doc 4.3).
    primary = make_fake_provider(ProviderId.CODEX, status=RunStatus.FAILED)
    fallback = make_fake_provider(ProviderId.CLAUDE)  # would succeed if (wrongly) invoked
    router = AgentRouter(config, {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback})
    outcome = router.run_stage(make_request(stage=Stage.REVIEW), router.resolve_route(Stage.REVIEW))
    assert outcome.stage_attempts == 1
    assert outcome.provider_used is ProviderId.CODEX
    assert outcome.result is not None and outcome.result.status is RunStatus.FAILED
    assert outcome.terminal_error is None
    assert fallback.run_count == 0
