"""Router resume safety net — ``session_unavailable`` fresh-retry (durable sessions, P2.2).

When a resume attempt fails because the provider can no longer find the session, the Router retries
the SAME provider once with a fresh session. This is infrastructure, not a quality failure: it never
falls back to another provider and never charges a fix iteration (the fix loop is engine-owned).
"""

from __future__ import annotations

from collections.abc import Callable

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AgentRunResult,
    ErrorClass,
    ProviderError,
    ProviderHealth,
    ProviderId,
    RunStatus,
    Stage,
)
from wastech_orchestrator.routing.router import AgentRouter


class _SessionAwareProvider:
    """Raises ``SESSION_UNAVAILABLE`` when asked to resume; succeeds on a fresh (no-session) run."""

    def __init__(self, provider_id: ProviderId) -> None:
        self.id = provider_id.value
        self.requests: list[AgentRunRequest] = []

    def preflight(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.id,
            executable_found=True,
            version="1",
            authenticated=True,
            supports_required_features=True,
            message="ok",
        )

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.requests.append(request)
        if request.session_id is not None:
            raise ProviderError(ErrorClass.SESSION_UNAVAILABLE, "session not found")
        return AgentRunResult(
            status=RunStatus.SUCCEEDED,
            provider=self.id,
            stage=request.stage,
            attempt=request.attempt,
            exit_code=0,
            started_at="t0",
            finished_at="t1",
            final_message="ok",
            session_id="fresh-sess",
        )


def test_session_unavailable_retries_without_resume_no_fix_iteration(
    config: OrchestratorConfig, make_request: Callable[..., AgentRunRequest]
) -> None:
    providers = {
        ProviderId.CLAUDE: _SessionAwareProvider(ProviderId.CLAUDE),
        ProviderId.CODEX: _SessionAwareProvider(ProviderId.CODEX),
    }
    router = AgentRouter(config, providers)  # type: ignore[arg-type]
    route = router.resolve_route(Stage.IMPLEMENTATION, None)  # the config's global primary
    primary = providers[route.primary]

    request = make_request(stage=Stage.IMPLEMENTATION, session_id="stale-session")
    outcome = router.run_stage(request, route)

    # The resume failed (session gone) → the SAME provider was retried fresh, which succeeded.
    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert outcome.provider_used is route.primary
    assert outcome.stage_attempts == 2  # one resume attempt + one fresh retry, same provider
    # The retry stripped the stale session id; the run succeeded, so no rework edge is taken and the
    # engine's fix_iterations is never charged (this stays inside one node run).
    assert [r.session_id for r in primary.requests] == ["stale-session", None]
    others = [p for pid, p in providers.items() if pid is not route.primary]
    assert all(o.requests == [] for o in others)  # never fell back to the other provider
