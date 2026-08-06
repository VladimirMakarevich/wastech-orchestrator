"""Router bounded same-provider transient retry + backoff (Option A).

A transient infra blip (``PROVIDER_UNAVAILABLE`` / ``NETWORK_UNAVAILABLE``) is retried on the SAME
provider with exponential backoff before falling back to the other allowed provider — a per-provider
budget (``agents.retry.max_attempts``) that is counted *separately* from ``max_stage_attempts``.
Quality failures and non-transient infra classes (``RATE_LIMITED`` / ``TIMEOUT``) are never retried.
``sleep`` is injected so the tests never actually wait; ``delays`` captures the backoff schedule.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from wastech_orchestrator.config.schema import OrchestratorConfig, RetryConfig
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AgentRunResult,
    ErrorClass,
    ProviderError,
    ProviderHealth,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.routing.router import AgentRouter


class _FlakyProvider:
    """Fails the first ``fail_times`` invocations with ``error_class``, then succeeds."""

    def __init__(
        self, provider_id: ProviderId, *, fail_times: int, error_class: ErrorClass
    ) -> None:
        self.id = provider_id.value
        self._left = fail_times
        self._error_class = error_class
        self.requests: list[AgentRunRequest] = []

    @property
    def run_count(self) -> int:
        return len(self.requests)

    def preflight(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.id,
            executable_found=True,
            version="1",
            supports_required_features=True,
            message="ok",
        )

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.requests.append(request)
        if self._left > 0:
            self._left -= 1
            raise ProviderError(self._error_class, f"fake {self._error_class.value}")
        return AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider=self.id,
            node_id=request.node_id,
            attempt=request.attempt,
            exit_code=0,
            started_at="t0",
            finished_at="t1",
            final_message="ok",
        )


class _ScriptedProvider:
    """Runs a scripted sequence of behaviours: an ``ErrorClass`` raises it, ``None`` succeeds."""

    def __init__(self, provider_id: ProviderId, script: list[ErrorClass | None]) -> None:
        self.id = provider_id.value
        self._script = list(script)
        self.requests: list[AgentRunRequest] = []

    @property
    def run_count(self) -> int:
        return len(self.requests)

    def preflight(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.id,
            executable_found=True,
            version="1",
            supports_required_features=True,
            message="ok",
        )

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.requests.append(request)
        action = self._script.pop(0)
        if action is not None:
            raise ProviderError(action, f"fake {action.value}")
        return AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider=self.id,
            node_id=request.node_id,
            attempt=request.attempt,
            exit_code=0,
            started_at="t0",
            finished_at="t1",
            final_message="ok",
        )


_BOTH = (ProviderId.CLAUDE, ProviderId.CODEX)


def _cfg(
    config: OrchestratorConfig,
    *,
    allowed: tuple[ProviderId, ...] | None = None,
    retry: RetryConfig | None = None,
    max_stage_attempts: int | None = None,
) -> OrchestratorConfig:
    agents = config.agents
    return replace(
        config,
        agents=replace(
            agents,
            allowed=allowed if allowed is not None else agents.allowed,
            retry=retry if retry is not None else agents.retry,
            max_stage_attempts=(
                max_stage_attempts if max_stage_attempts is not None else agents.max_stage_attempts
            ),
        ),
    )


def test_recovers_after_transient_failures(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Single allowed provider, no fallback target: the transient blip must be survived by the
    # same-provider retry alone. Two 500s, then success → 3 invocations, backoff [2.0, 4.0].
    cfg = _cfg(config, allowed=(ProviderId.CLAUDE,), retry=RetryConfig(max_attempts=2))
    claude = _FlakyProvider(
        ProviderId.CLAUDE, fail_times=2, error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    delays: list[float] = []
    router = AgentRouter(cfg, {ProviderId.CLAUDE: claude}, sleep=delays.append)  # type: ignore[dict-item]
    route = router.resolve_route("implementation")
    assert route.fallback is None

    outcome = router.run_stage(make_request(node_id="implementation"), route)

    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert outcome.provider_used is ProviderId.CLAUDE
    assert claude.run_count == 3  # 1 initial + 2 retries
    assert delays == [2.0, 4.0]  # min(2*2**0, 30), min(2*2**1, 30)
    assert outcome.stage_attempts == 1  # transient retries do NOT consume a stage hop
    # Every invocation wrote a gap-free audit row; the last is the success.
    assert [a.attempt for a in outcome.attempts] == [1, 2, 3]
    assert [a.error_class for a in outcome.attempts] == [
        ErrorClass.PROVIDER_UNAVAILABLE,
        ErrorClass.PROVIDER_UNAVAILABLE,
        None,
    ]


def test_exhaustion_falls_back_symmetrically(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # claude (global primary) always 500; after its retries are spent the route switches to codex,
    # which succeeds on its first try (so only the primary contributes backoff delays).
    cfg = _cfg(config, allowed=_BOTH, retry=RetryConfig(max_attempts=2))
    claude = _FlakyProvider(
        ProviderId.CLAUDE, fail_times=99, error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    codex = _FlakyProvider(
        ProviderId.CODEX, fail_times=0, error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    delays: list[float] = []
    router = AgentRouter(
        cfg,
        {ProviderId.CLAUDE: claude, ProviderId.CODEX: codex},
        sleep=delays.append,  # type: ignore[dict-item]
    )
    route = router.resolve_route("implementation")
    assert (route.primary, route.fallback) == (ProviderId.CLAUDE, ProviderId.CODEX)

    outcome = router.run_stage(make_request(node_id="implementation"), route)

    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert outcome.provider_used is ProviderId.CODEX
    assert claude.run_count == 3  # 1 + 2 retries
    assert codex.run_count == 1
    assert delays == [2.0, 4.0]  # only the primary retried
    assert outcome.stage_attempts == 2  # two hops, retries excluded
    assert len(outcome.attempts) == 4


def test_both_providers_exhausted_is_terminal(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Both providers always 500: each spends its own retry budget → terminal infra error with the
    # transient class (which the orchestrator turns into a B-lite soft pause).
    cfg = _cfg(config, allowed=_BOTH, retry=RetryConfig(max_attempts=2))
    claude = _FlakyProvider(
        ProviderId.CLAUDE, fail_times=99, error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    codex = _FlakyProvider(
        ProviderId.CODEX, fail_times=99, error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    delays: list[float] = []
    router = AgentRouter(
        cfg,
        {ProviderId.CLAUDE: claude, ProviderId.CODEX: codex},
        sleep=delays.append,  # type: ignore[dict-item]
    )
    route = router.resolve_route("implementation")

    outcome = router.run_stage(make_request(node_id="implementation"), route)

    assert outcome.result is None
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.PROVIDER_UNAVAILABLE
    assert delays == [2.0, 4.0, 2.0, 4.0]  # per-provider budget
    assert outcome.stage_attempts == 2
    assert len(outcome.attempts) == 6  # 3 per provider


def test_rate_limited_is_not_transient_retried(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # RATE_LIMITED is fallback-eligible but deliberately NOT transient-retryable (it wants a long
    # defer). So: one attempt on the primary, no backoff, straight to fallback.
    cfg = _cfg(config, allowed=_BOTH, retry=RetryConfig(max_attempts=3))
    claude = _FlakyProvider(ProviderId.CLAUDE, fail_times=99, error_class=ErrorClass.RATE_LIMITED)
    codex = _FlakyProvider(ProviderId.CODEX, fail_times=0, error_class=ErrorClass.RATE_LIMITED)
    delays: list[float] = []
    router = AgentRouter(
        cfg,
        {ProviderId.CLAUDE: claude, ProviderId.CODEX: codex},
        sleep=delays.append,  # type: ignore[dict-item]
    )
    route = router.resolve_route("implementation")

    outcome = router.run_stage(make_request(node_id="implementation"), route)

    assert outcome.provider_used is ProviderId.CODEX
    assert claude.run_count == 1
    assert delays == []


def test_timeout_is_not_transient_retried(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # TIMEOUT is excluded too (a timeout often means long/partial work — a retry risks duplicating).
    cfg = _cfg(config, allowed=_BOTH, retry=RetryConfig(max_attempts=3))
    claude = _FlakyProvider(ProviderId.CLAUDE, fail_times=99, error_class=ErrorClass.TIMEOUT)
    codex = _FlakyProvider(ProviderId.CODEX, fail_times=0, error_class=ErrorClass.TIMEOUT)
    delays: list[float] = []
    router = AgentRouter(
        cfg,
        {ProviderId.CLAUDE: claude, ProviderId.CODEX: codex},
        sleep=delays.append,  # type: ignore[dict-item]
    )
    route = router.resolve_route("implementation")

    outcome = router.run_stage(make_request(node_id="implementation"), route)

    assert outcome.provider_used is ProviderId.CODEX
    assert claude.run_count == 1
    assert delays == []


def test_quality_failure_is_never_retried(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # A returned quality failure (status=failed) is not an infra error: returned as-is, no retry.
    from tests.routing.conftest import FakeProvider

    cfg = _cfg(config, allowed=(ProviderId.CLAUDE,), retry=RetryConfig(max_attempts=3))
    claude = FakeProvider(ProviderId.CLAUDE, status=RunStatus.FAILED)
    delays: list[float] = []
    router = AgentRouter(cfg, {ProviderId.CLAUDE: claude}, sleep=delays.append)  # type: ignore[dict-item]
    route = router.resolve_route("implementation")

    outcome = router.run_stage(make_request(node_id="implementation"), route)

    assert outcome.result is not None and outcome.result.status is RunStatus.FAILED
    assert claude.run_count == 1
    assert delays == []


def test_retry_budget_independent_of_max_stage_attempts(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # max_stage_attempts=1 (one hop) but the transient retry budget is 3 — the retries must survive
    # even with the hop budget fully spent, proving the two budgets are independent.
    cfg = _cfg(
        config,
        allowed=(ProviderId.CLAUDE,),
        retry=RetryConfig(max_attempts=3),
        max_stage_attempts=1,
    )
    claude = _FlakyProvider(
        ProviderId.CLAUDE, fail_times=3, error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    delays: list[float] = []
    router = AgentRouter(cfg, {ProviderId.CLAUDE: claude}, sleep=delays.append)  # type: ignore[dict-item]
    route = router.resolve_route("implementation")

    outcome = router.run_stage(make_request(node_id="implementation"), route)

    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert claude.run_count == 4  # 1 + 3 retries
    assert outcome.stage_attempts == 1
    assert delays == [2.0, 4.0, 8.0]


def test_resume_then_fresh_degrade(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # Transient blip → resume retry hits SESSION_UNAVAILABLE → drop the session for the next retry,
    # which succeeds (resume → fresh degrade, same shape as the session-unavailable safety net).
    cfg = _cfg(config, allowed=(ProviderId.CLAUDE,), retry=RetryConfig(max_attempts=2))
    claude = _ScriptedProvider(
        ProviderId.CLAUDE,
        [ErrorClass.PROVIDER_UNAVAILABLE, ErrorClass.SESSION_UNAVAILABLE, None],
    )
    delays: list[float] = []
    router = AgentRouter(cfg, {ProviderId.CLAUDE: claude}, sleep=delays.append)  # type: ignore[dict-item]
    route = router.resolve_route("implementation")

    outcome = router.run_stage(make_request(node_id="implementation", session_id="sess"), route)

    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert claude.run_count == 3
    assert [r.session_id for r in claude.requests] == ["sess", "sess", None]
    assert delays == [2.0, 4.0]


def test_max_attempts_zero_disables_transient_retry(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # max_attempts=0 turns the same-provider retry off: the primary's blip goes to fallback.
    cfg = _cfg(config, allowed=_BOTH, retry=RetryConfig(max_attempts=0))
    claude = _FlakyProvider(
        ProviderId.CLAUDE, fail_times=99, error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    codex = _FlakyProvider(
        ProviderId.CODEX, fail_times=0, error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    delays: list[float] = []
    router = AgentRouter(
        cfg,
        {ProviderId.CLAUDE: claude, ProviderId.CODEX: codex},
        sleep=delays.append,  # type: ignore[dict-item]
    )
    route = router.resolve_route("implementation")

    outcome = router.run_stage(make_request(node_id="implementation"), route)

    assert outcome.provider_used is ProviderId.CODEX
    assert claude.run_count == 1
    assert delays == []


def test_max_delay_clamps_backoff(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    # base=10, max=15: delays are min(10*2**k, 15) = [10, 15, 15].
    cfg = _cfg(
        config,
        allowed=(ProviderId.CLAUDE,),
        retry=RetryConfig(max_attempts=3, base_delay_s=10.0, max_delay_s=15.0),
    )
    claude = _FlakyProvider(
        ProviderId.CLAUDE, fail_times=99, error_class=ErrorClass.PROVIDER_UNAVAILABLE
    )
    delays: list[float] = []
    router = AgentRouter(cfg, {ProviderId.CLAUDE: claude}, sleep=delays.append)  # type: ignore[dict-item]
    route = router.resolve_route("implementation")

    outcome = router.run_stage(make_request(node_id="implementation"), route)

    assert outcome.result is None
    assert delays == [10.0, 15.0, 15.0]
    assert claude.run_count == 4
