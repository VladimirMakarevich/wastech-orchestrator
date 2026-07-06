"""Reliable-stop: a stop-killed agent must never be respawned on the fallback provider.

A SIGKILLed agent surfaces as ``PROCESS_CRASHED`` (fallback-eligible), so without the cancellation
gate the Router would immediately launch a fresh fallback agent. The ``is_cancelled`` seam, checked
before any retry/fallback, reclassifies that raised error as ``CANCELLED`` (terminal, non-fallback)
so the Core parks the task resumable instead.
"""

from __future__ import annotations

from collections.abc import Callable

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    ErrorClass,
    ProviderId,
)
from wastech_orchestrator.routing.router import AgentRouter


def test_cancelled_stop_does_not_fall_back(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.PROCESS_CRASHED)
    fallback = make_fake_provider(ProviderId.CLAUDE)  # would succeed if (wrongly) invoked
    router = AgentRouter(
        config,
        {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback},
        is_cancelled=lambda: True,  # an operator stop is in flight
    )
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.result is None
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.CANCELLED
    assert primary.run_count == 1  # the one killed attempt
    assert fallback.run_count == 0  # NO respawn on the fallback provider


def test_process_crashed_still_falls_back_when_not_cancelled(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # Regression guard: the cancellation check is the ONLY thing suppressing fallback — a genuine
    # PROCESS_CRASHED (no stop requested) still falls back as before.
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.PROCESS_CRASHED)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = AgentRouter(
        config,
        {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback},
        is_cancelled=lambda: False,  # the default
    )
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.result is not None  # the fallback ran and succeeded
    assert outcome.provider_used is ProviderId.CLAUDE
    assert fallback.run_count == 1
