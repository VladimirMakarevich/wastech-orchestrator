"""Fallback decision table: which errors fall back, the conditional rule,
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
    [
        ErrorClass.CONFIGURATION_ERROR,
        ErrorClass.TASK_FAILURE,
        ErrorClass.INVALID_INVOCATION,
        ErrorClass.MODEL_REQUEST_INVALID,
        # An unproven process-tree quiescence is a security/manual-action condition — never
        # respawn a fresh agent on the other provider while an unknown writer may still be live.
        ErrorClass.CONTAINMENT_UNVERIFIED,
    ],
)
def test_non_fallback_classes_never_fall_back(error_class: ErrorClass) -> None:
    # Even with an equal/stricter profile, a non-infra class is never eligible.
    assert not fallback_allowed(error_class, primary_profile=WORKSPACE, fallback_profile=READONLY)


def test_containment_unverified_raised_does_not_fall_back(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # End-to-end through the router: a raised CONTAINMENT_UNVERIFIED is terminal for the
    # stage — the other provider is never invoked, so no fresh agent races the unproven subtree.
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.CONTAINMENT_UNVERIFIED)
    fallback = make_fake_provider(ProviderId.CLAUDE)  # would succeed if (wrongly) invoked
    router = AgentRouter(config, {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback})
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.result is None
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.CONTAINMENT_UNVERIFIED
    assert fallback.run_count == 0  # the fallback provider was never launched


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


# --- CAPABILITY_UNAVAILABLE host-verified fallback
# ----------------------------------------


@pytest.mark.parametrize(
    "primary_profile,fallback_profile,can_isolate,expected",
    [
        (WORKSPACE, WORKSPACE, True, True),  # same profile + fallback can isolate → allowed
        (WORKSPACE, WORKSPACE, False, False),  # fallback cannot isolate here → refused
        (WORKSPACE, READONLY, True, True),  # stricter + can isolate → allowed
        (READONLY, WORKSPACE, True, False),  # looser profile → refused even if it can isolate
    ],
)
def test_capability_unavailable_needs_same_or_stricter_and_isolable(
    primary_profile: str, fallback_profile: str, can_isolate: bool, expected: bool
) -> None:
    assert (
        fallback_allowed(
            ErrorClass.CAPABILITY_UNAVAILABLE,
            primary_profile=primary_profile,
            fallback_profile=fallback_profile,
            fallback_can_isolate=can_isolate,
        )
        is expected
    )


def test_capability_unavailable_falls_over_only_to_an_isolable_provider(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # End-to-end: a primary that raises CAPABILITY_UNAVAILABLE falls over to the other provider ONLY
    # when that provider can itself isolate the node on this host (empty isolation reasons).
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.CAPABILITY_UNAVAILABLE)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = AgentRouter(
        config,
        {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback},
        isolation_checks={ProviderId.CLAUDE: lambda _cfg: [], ProviderId.CODEX: lambda _cfg: []},
    )
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert fallback.run_count == 1  # the isolable fallback was used
    assert outcome.result is not None


def test_capability_unavailable_is_terminal_when_fallback_cannot_isolate(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.CAPABILITY_UNAVAILABLE)
    fallback = make_fake_provider(ProviderId.CLAUDE)  # would succeed if (wrongly) invoked
    router = AgentRouter(
        config,
        {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback},
        # The fallback cannot isolate the node on this host → no cross-provider recovery.
        isolation_checks={
            ProviderId.CLAUDE: lambda _cfg: ["Bash sandbox unavailable"],
            ProviderId.CODEX: lambda _cfg: [],
        },
    )
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert fallback.run_count == 0  # never launched an equally-unisolable provider
    assert outcome.result is None
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.CAPABILITY_UNAVAILABLE


def test_quality_failure_is_not_a_fallback_trigger(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # A returned status=failed (not a raised error) is surfaced as-is for the Core to route to
    # fixing — never retried on the fallback (phase doc 4.3).
    primary = make_fake_provider(ProviderId.CODEX, status=RunStatus.FAILED)
    fallback = make_fake_provider(ProviderId.CLAUDE)  # would succeed if (wrongly) invoked
    router = AgentRouter(config, {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback})
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.stage_attempts == 1
    assert outcome.provider_used is ProviderId.CODEX
    assert outcome.result is not None and outcome.result.status is RunStatus.FAILED
    assert outcome.terminal_error is None
    assert fallback.run_count == 0
