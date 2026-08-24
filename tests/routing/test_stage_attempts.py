"""`stage_attempts` counting and bounding across fallback (phase doc 4.4).

Drives the router with in-memory fakes so each attempt's outcome is deterministic. The node pins
``provider=codex`` while claude is the global primary, so the route is (codex, claude): the CODEX
fake is the primary and the CLAUDE fake the (global-primary) fallback.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

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


_EARLY = "2026-08-06T05:10:00+00:00"
_LATE = "2026-08-06T09:40:00+00:00"


@pytest.mark.parametrize(
    "primary_reset,fallback_reset,expected",
    [
        # The incident's own shape: the primary reported its window, the fallback died for an
        # unrelated reason and reported nothing. Taking the SETTLING attempt's instant would discard
        # the only answer there is, in exactly the case the feature exists for.
        (_EARLY, None, _EARLY),
        (None, _EARLY, _EARLY),  # symmetric — whichever attempt knew it
        (_LATE, _EARLY, _EARLY),  # both knew: wake at the earliest, then re-park if still limited
        (_EARLY, _LATE, _EARLY),
        (None, None, None),  # nobody knew: the park stays blind, as before
    ],
)
def test_exhausted_stage_surfaces_the_earliest_reported_reset_instant(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
    primary_reset: str | None,
    fallback_reset: str | None,
    expected: str | None,
) -> None:
    # The instant crosses six hops from the CLI event to the parked task; this is the cheap guard on
    # the one most likely to drop it, because the Router builds its normalized error from the RAISED
    # exception rather than from anything the adapter kept.
    primary = make_fake_provider(
        ProviderId.CODEX, raises=ErrorClass.RATE_LIMITED, raises_resets_at=primary_reset
    )
    fallback = make_fake_provider(
        ProviderId.CLAUDE, raises=ErrorClass.RATE_LIMITED, raises_resets_at=fallback_reset
    )
    router = _router(config, primary, fallback)
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.resets_at == expected


def test_a_reset_instant_survives_a_fallback_that_failed_for_another_reason(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # The literal incident: rate-limited primary, fallback dead on expired credentials. The class
    # the stage settles on is the fallback's, and so is its (absent) instant — but the window the
    # primary reported is still the only useful answer, so it must survive to the park.
    primary = make_fake_provider(
        ProviderId.CODEX, raises=ErrorClass.RATE_LIMITED, raises_resets_at=_EARLY
    )
    fallback = make_fake_provider(ProviderId.CLAUDE, raises=ErrorClass.AUTHENTICATION_FAILED)
    router = _router(config, primary, fallback)
    outcome = router.run_stage(
        make_request(node_id="review"), router.resolve_route("review", ProviderId.CODEX)
    )
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.AUTHENTICATION_FAILED
    assert outcome.terminal_error.resets_at == _EARLY


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


def test_a_raised_attempt_carries_its_own_result_not_a_row_write_clock(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # The adapter builds a complete result for a failed attempt — it is what `result.json` on disk
    # is written from — and it must not die with the stack frame. If the Router records the attempt
    # with `result=None`, the ledger row stamps two identical reads of the row-write clock, and the
    # one thing an operator wants to know about a node failing over and over — what the failing
    # attempt cost, and how long it burned first — is on disk and nowhere queryable.
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.PERMISSION_DENIED)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = _router(config, primary, fallback)
    outcome = router.run_stage(
        make_request(node_id="polish"), router.resolve_route("polish", ProviderId.CODEX)
    )
    raised = next(a for a in outcome.attempts if a.provider is ProviderId.CODEX)
    assert raised.status is None  # still no verdict — the row raised, nothing may read one off it
    assert raised.error_class is ErrorClass.PERMISSION_DENIED
    assert raised.result is not None
    assert raised.result.started_at != raised.result.finished_at  # a real interval
    assert raised.result.exit_code == 1
