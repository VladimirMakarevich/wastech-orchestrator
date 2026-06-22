"""`stage_attempts` counting and bounding across fallback (phase doc 4.4).

Drives the router with in-memory fakes so each attempt's outcome is deterministic. The node pins
``provider=codex`` while claude is the global primary, so the route is (codex, claude): the CODEX
fake is the primary and the CLAUDE fake the (global-primary) fallback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    ErrorClass,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.routing.router import AgentRouter


def _router(config: OrchestratorConfig, primary: object, fallback: object) -> AgentRouter:
    return AgentRouter(config, {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback})


def test_success_on_primary_does_not_invoke_fallback(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    primary = make_fake_provider(ProviderId.CODEX)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = _router(config, primary, fallback)
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.stage_attempts == 1
    assert outcome.provider_used is ProviderId.CODEX
    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert fallback.run_count == 0


def test_stage_attempts_increment_across_fallback(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.TIMEOUT)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = _router(config, primary, fallback)
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.stage_attempts == 2
    assert outcome.provider_used is ProviderId.CLAUDE
    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert [a.provider for a in outcome.attempts] == [ProviderId.CODEX, ProviderId.CLAUDE]
    assert [a.attempt for a in outcome.attempts] == [1, 2]


def test_max_stage_attempts_one_blocks_fallback(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    cfg = replace(config, agents=replace(config.agents, max_stage_attempts=1))
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.TIMEOUT)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = _router(cfg, primary, fallback)
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.stage_attempts == 1
    assert fallback.run_count == 0
    assert outcome.result is None
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.TIMEOUT


def test_both_infra_failures_exhaust_the_stage(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.TIMEOUT)
    fallback = make_fake_provider(ProviderId.CLAUDE, raises=ErrorClass.RATE_LIMITED)
    router = _router(config, primary, fallback)
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.stage_attempts == 2
    assert outcome.result is None
    assert outcome.provider_used is None
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.RATE_LIMITED  # the last error wins
    assert len(outcome.attempts) == 2


def test_non_fallback_infra_error_stops_at_primary(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # configuration_error is not fallback-eligible → no retry; surfaced for `failed`.
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.CONFIGURATION_ERROR)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = _router(config, primary, fallback)
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.stage_attempts == 1
    assert fallback.run_count == 0
    assert outcome.result is None
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.CONFIGURATION_ERROR
