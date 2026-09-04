"""Router integration scenarios (phase doc "Tests").

Two scenarios run the **real** Codex/Claude adapters over the deterministic fake CLI (building on
the provider harness) to prove end-to-end wiring: a successful infra-fallback, and fallback denied
on a
quality failure. The third — an infra failure after files changed — asserts the contract on the
request the router hands the fallback, so it uses recording in-memory providers (the diff-passing is
a router/Core data-exchange concern, best observed on the ``AgentRunRequest`` itself).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import (
    OrchestratorConfig,
    ProviderConfig,
    RetryConfig,
    SecurityConfig,
)
from wastech_orchestrator.core.flow.nodes.evaluator import _FINDINGS_SCHEMA
from wastech_orchestrator.providers.base import (
    AgentProvider,
    AgentRunRequest,
    ErrorClass,
    ProviderId,
    RunStatus,
    build_effective_prompt,
)
from wastech_orchestrator.providers.claude import ClaudeCodeProvider
from wastech_orchestrator.providers.codex import CodexProvider
from wastech_orchestrator.routing.router import AgentRouter
from wastech_orchestrator.routing.snapshots import PartialChange, WorkingTreeSnapshot

# Every test here is a slow integration test (real git / subprocess / process tree).
pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _make_clone(tmp_path: Path) -> None:
    # The request's working_directory must exist for the subprocess cwd.
    (tmp_path / "clone").mkdir(exist_ok=True)


def _build_provider(
    name: str, command: str, security: SecurityConfig, artifacts_root: Path
) -> AgentProvider:
    if name == "codex":
        cfg = ProviderConfig(
            command=command,
            model="",
            timeout_seconds=7200,
            permission_profile="workspace-write",
            extra_args=(),
        )
        return CodexProvider(cfg, security=security, artifacts_root=artifacts_root)
    cfg = ProviderConfig(
        command=command,
        model="",
        timeout_seconds=7200,
        permission_profile="workspace-write",
        extra_args=(),
    )
    return ClaudeCodeProvider(cfg, security=security, artifacts_root=artifacts_root)


def test_successful_infra_fallback(
    config: OrchestratorConfig,
    integration_security: SecurityConfig,
    fake_cli: Callable[..., str],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # review route = (codex, claude): the primary rate-limits (infra), the fallback succeeds.
    codex = _build_provider(
        "codex", fake_cli("rate_limited", "codex"), integration_security, tmp_path
    )
    claude = _build_provider(
        "claude", fake_cli("success", "claude"), integration_security, tmp_path
    )
    router = AgentRouter(config, {ProviderId.CODEX: codex, ProviderId.CLAUDE: claude})
    route = router.resolve_route("review", ProviderId.CODEX)

    outcome = router.run_stage(
        make_request(node_id="review", working_directory=str(tmp_path / "clone")), route
    )

    assert outcome.provider_used is ProviderId.CLAUDE
    assert outcome.stage_attempts == 2
    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert outcome.result.session_id == "sess-fake"
    # Both attempts are recorded for the audit, with the primary's infra class.
    assert [a.provider for a in outcome.attempts] == [ProviderId.CODEX, ProviderId.CLAUDE]
    assert outcome.attempts[0].error_class is ErrorClass.RATE_LIMITED


def test_codex_review_succeeds_under_findings_schema_no_fallback(
    config: OrchestratorConfig,
    integration_security: SecurityConfig,
    fake_cli: Callable[..., str],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # With _FINDINGS_SCHEMA now strict, the real codex adapter runs the review under
    # --output-schema and returns findings — the router accepts it on attempt 1 and never falls back
    # to claude. Before the strictness fix the schema 400-crashed every codex review
    # (process_crashed) and the
    # same-vendor claude fallback silently reviewed instead, so cross-provider review never ran.
    codex = _build_provider("codex", fake_cli("success", "codex"), integration_security, tmp_path)
    claude = _build_provider(
        "claude", fake_cli("success", "claude"), integration_security, tmp_path
    )
    router = AgentRouter(config, {ProviderId.CODEX: codex, ProviderId.CLAUDE: claude})
    route = router.resolve_route("review", ProviderId.CODEX)

    outcome = router.run_stage(
        make_request(
            node_id="review",
            working_directory=str(tmp_path / "clone"),
            output_schema=_FINDINGS_SCHEMA,
        ),
        route,
    )

    assert outcome.provider_used is ProviderId.CODEX  # codex ran and was accepted
    assert outcome.stage_attempts == 1  # no fallback hop
    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert outcome.result.structured_output == {"findings": []}  # honored the strict schema
    assert not (tmp_path / "logs" / "task-001" / "stages" / "review" / "2-claude").exists()


def test_fallback_denied_on_quality_failure(
    config: OrchestratorConfig,
    integration_security: SecurityConfig,
    fake_cli: Callable[..., str],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # The primary exits cleanly but reports a quality failure → returned as-is, fallback never runs.
    codex = _build_provider(
        "codex", fake_cli("task_failure", "codex"), integration_security, tmp_path
    )
    claude = _build_provider(
        "claude", fake_cli("success", "claude"), integration_security, tmp_path
    )
    router = AgentRouter(config, {ProviderId.CODEX: codex, ProviderId.CLAUDE: claude})
    route = router.resolve_route("review", ProviderId.CODEX)

    outcome = router.run_stage(
        make_request(node_id="review", working_directory=str(tmp_path / "clone")), route
    )

    assert outcome.stage_attempts == 1
    assert outcome.provider_used is ProviderId.CODEX
    assert outcome.result is not None and outcome.result.status is RunStatus.FAILED
    assert outcome.result.error is not None
    assert outcome.result.error.error_class is ErrorClass.TASK_FAILURE
    # The fallback (claude) never ran — no second attempt directory was created.
    assert not (tmp_path / "logs" / "task-001" / "stages" / "review" / "2-claude").exists()


def test_infra_failure_after_changes_hands_diff_to_fallback(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_snapshot_hook: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    #: an infra failure that changed files is not rolled back; the fallback gets the current
    # diff. The snapshot hook reports a partial change; assert the fallback request gets it.
    diff_path = str(tmp_path / "partial.diff")
    snap = WorkingTreeSnapshot(
        commit_sha="sha-after", porcelain_status=" M file.py", diff_checksum="ck1", artifacts=()
    )
    partial = PartialChange(before=snap, after=snap, diff_path=diff_path, note="partial attempt")
    hook = make_snapshot_hook(partial)
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.TIMEOUT)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = AgentRouter(config, {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback})
    route = router.resolve_route("review", ProviderId.CODEX)

    outcome = router.run_stage(
        make_request(node_id="review", diff_path="cumulative.diff"), route, snapshot=hook
    )

    assert outcome.provider_used is ProviderId.CLAUDE
    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    # The fallback received the PARTIAL diff, not the original cumulative one.
    assert fallback.requests[0].diff_path == diff_path
    assert outcome.partial_change is partial
    assert hook.capture_calls == 1
    assert hook.partial_calls == 1


def test_cross_provider_fallback_drops_provider_specific_request_fields(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # A node pinned to codex-specific settings: when codex infra-fails, the claude fallback must NOT
    # receive codex's provider-specific model/reasoning/extra_args/session id. Portable context
    # stays.
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.RATE_LIMITED)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = AgentRouter(config, {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback})
    route = router.resolve_route("review", ProviderId.CODEX)

    outcome = router.run_stage(
        make_request(
            node_id="review",
            model="gpt-5.5",
            reasoning="high",
            extra_args=["-c", 'model_reasoning_effort="high"'],
            session_id="codex-session-123",
            task_path="/logs/task.md",
            output_schema={"type": "object"},
            network_access=True,
        ),
        route,
    )

    assert outcome.provider_used is ProviderId.CLAUDE
    # The primary saw the pins; the cross-provider fallback saw provider-specific fields cleared.
    assert primary.requests[0].model == "gpt-5.5"  # type: ignore[attr-defined]
    assert primary.requests[0].reasoning == "high"  # type: ignore[attr-defined]
    assert primary.requests[0].extra_args == [  # type: ignore[attr-defined]
        "-c",
        'model_reasoning_effort="high"',
    ]
    assert primary.requests[0].session_id == "codex-session-123"  # type: ignore[attr-defined]
    assert fallback.requests[0].model is None  # type: ignore[attr-defined]
    assert fallback.requests[0].reasoning is None  # type: ignore[attr-defined]
    assert fallback.requests[0].extra_args == []  # type: ignore[attr-defined]
    assert fallback.requests[0].session_id is None  # type: ignore[attr-defined]
    assert fallback.requests[0].task_path == "/logs/task.md"  # type: ignore[attr-defined]
    assert fallback.requests[0].output_schema == {"type": "object"}  # type: ignore[attr-defined]
    assert fallback.requests[0].network_access is True  # type: ignore[attr-defined]


def test_permission_denied_infra_falls_back_to_claude(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    # The corrected Windows sandbox-helper routing: a pinned-Codex workspace-write node that raises
    # PERMISSION_DENIED (the normalized helper failure) is a CONDITIONAL fallback — allowed here
    # because the fallback profile is same-or-stricter (both providers are workspace-write in the
    # packaged config) — so the stage falls over to Claude instead of dead-ending in a fixing loop.
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.PERMISSION_DENIED)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = AgentRouter(config, {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback})
    route = router.resolve_route("review", ProviderId.CODEX)

    outcome = router.run_stage(make_request(node_id="review"), route)

    assert outcome.provider_used is ProviderId.CLAUDE
    assert outcome.stage_attempts == 2  # one Codex hop, one Claude hop — no fixing loop
    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert outcome.attempts[0].error_class is ErrorClass.PERMISSION_DENIED


def test_codex_minimal_reasoning_fallback_maps_to_claude_low(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
) -> None:
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.RATE_LIMITED)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = AgentRouter(config, {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback})
    route = router.resolve_route("review", ProviderId.CODEX)

    outcome = router.run_stage(make_request(node_id="review", reasoning="minimal"), route)

    assert outcome.provider_used is ProviderId.CLAUDE
    assert primary.requests[0].reasoning == "minimal"  # type: ignore[attr-defined]
    assert fallback.requests[0].reasoning == "low"  # type: ignore[attr-defined]


def _with_retry(
    config: OrchestratorConfig,
    retry: RetryConfig,
    *,
    allowed: tuple[ProviderId, ...] | None = None,
) -> OrchestratorConfig:
    agents = config.agents
    return replace(
        config,
        agents=replace(
            agents,
            retry=retry,
            allowed=allowed if allowed is not None else agents.allowed,
        ),
    )


def test_transient_500_recovers_same_provider(
    config: OrchestratorConfig,
    integration_security: SecurityConfig,
    fake_cli: Callable[..., str],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # The real Claude adapter over a CLI that 5xx's twice then succeeds. Single allowed provider →
    # no fallback; the same-provider transient retry must carry it. Inject a no-op sleep via tiny
    # delays so the test never actually waits.
    cfg = _with_retry(
        config,
        RetryConfig(max_attempts=2, base_delay_s=0.0, max_delay_s=0.0),
        allowed=(ProviderId.CLAUDE,),
    )
    claude = _build_provider(
        "claude", fake_cli("flaky_500_2", "claude"), integration_security, tmp_path
    )
    router = AgentRouter(cfg, {ProviderId.CLAUDE: claude}, sleep=lambda _d: None)
    route = router.resolve_route("implementation")
    assert route.fallback is None

    outcome = router.run_stage(
        make_request(node_id="implementation", working_directory=str(tmp_path / "clone")), route
    )

    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert outcome.provider_used is ProviderId.CLAUDE
    assert outcome.stage_attempts == 1  # retries do not spend a stage hop
    # Three audit rows: two transient failures then the success.
    assert [a.error_class for a in outcome.attempts] == [
        ErrorClass.PROVIDER_UNAVAILABLE,
        ErrorClass.PROVIDER_UNAVAILABLE,
        None,
    ]


def test_transient_500_exhaustion_falls_back(
    config: OrchestratorConfig,
    integration_security: SecurityConfig,
    fake_cli: Callable[..., str],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # Default route (claude primary, codex symmetric fallback): claude always 5xx, codex succeeds.
    cfg = _with_retry(config, RetryConfig(max_attempts=2, base_delay_s=0.0, max_delay_s=0.0))
    claude = _build_provider(
        "claude", fake_cli("provider_unavailable", "claude"), integration_security, tmp_path
    )
    codex = _build_provider("codex", fake_cli("success", "codex"), integration_security, tmp_path)
    router = AgentRouter(
        cfg, {ProviderId.CLAUDE: claude, ProviderId.CODEX: codex}, sleep=lambda _d: None
    )
    route = router.resolve_route("implementation")
    assert (route.primary, route.fallback) == (ProviderId.CLAUDE, ProviderId.CODEX)

    outcome = router.run_stage(
        make_request(node_id="implementation", working_directory=str(tmp_path / "clone")), route
    )

    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert outcome.provider_used is ProviderId.CODEX
    assert outcome.stage_attempts == 2
    # 3 claude attempts (1 + 2 retries) then codex.
    assert [a.provider for a in outcome.attempts] == [
        ProviderId.CLAUDE,
        ProviderId.CLAUDE,
        ProviderId.CLAUDE,
        ProviderId.CODEX,
    ]


def test_transient_500_exhaustion_terminal_when_single_provider(
    config: OrchestratorConfig,
    integration_security: SecurityConfig,
    fake_cli: Callable[..., str],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # Single allowed provider, always 5xx: no fallback, retries exhaust → terminal infra error
    # carrying the transient class (which the orchestrator would turn into a B-lite soft pause).
    cfg = _with_retry(
        config,
        RetryConfig(max_attempts=2, base_delay_s=0.0, max_delay_s=0.0),
        allowed=(ProviderId.CLAUDE,),
    )
    claude = _build_provider(
        "claude", fake_cli("provider_unavailable", "claude"), integration_security, tmp_path
    )
    router = AgentRouter(cfg, {ProviderId.CLAUDE: claude}, sleep=lambda _d: None)
    route = router.resolve_route("implementation")

    outcome = router.run_stage(
        make_request(node_id="implementation", working_directory=str(tmp_path / "clone")), route
    )

    assert outcome.result is None
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.PROVIDER_UNAVAILABLE
    assert len(outcome.attempts) == 3  # 1 + max_attempts retries


def test_session_limit_stdout_falls_back(
    config: OrchestratorConfig,
    integration_security: SecurityConfig,
    fake_cli: Callable[..., str],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # Claude's STRUCTURAL stdout session-limit (429 / rate_limit_event / banner, empty stderr) is
    # RAISED as RATE_LIMITED, so the Router falls over to codex (a separate quota) and finishes.
    # This is the path the field failures needed: a *raised* limit, not a returned task_failure.
    claude = _build_provider(
        "claude", fake_cli("session_limit", "claude"), integration_security, tmp_path
    )
    codex = _build_provider("codex", fake_cli("success", "codex"), integration_security, tmp_path)
    router = AgentRouter(config, {ProviderId.CLAUDE: claude, ProviderId.CODEX: codex})
    route = router.resolve_route("implementation")
    assert (route.primary, route.fallback) == (ProviderId.CLAUDE, ProviderId.CODEX)

    outcome = router.run_stage(
        make_request(node_id="implementation", working_directory=str(tmp_path / "clone")), route
    )

    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert outcome.provider_used is ProviderId.CODEX
    assert outcome.stage_attempts == 2
    # No same-provider retry for a rate limit (RATE_LIMITED ∉ TRANSIENT_RETRYABLE): straight to
    # fallback — exactly one claude attempt, then codex.
    assert [a.provider for a in outcome.attempts] == [ProviderId.CLAUDE, ProviderId.CODEX]
    assert outcome.attempts[0].error_class is ErrorClass.RATE_LIMITED


def test_session_limit_both_providers_terminal(
    config: OrchestratorConfig,
    integration_security: SecurityConfig,
    fake_cli: Callable[..., str],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # Both providers rate-limited: the route exhausts and the router surfaces a terminal
    # RATE_LIMITED (result=None) — which the orchestrator turns into a resumable park, not a fail.
    claude = _build_provider(
        "claude", fake_cli("session_limit", "claude"), integration_security, tmp_path
    )
    codex = _build_provider(
        "codex", fake_cli("session_limit", "codex"), integration_security, tmp_path
    )
    router = AgentRouter(config, {ProviderId.CLAUDE: claude, ProviderId.CODEX: codex})
    route = router.resolve_route("implementation")

    outcome = router.run_stage(
        make_request(node_id="implementation", working_directory=str(tmp_path / "clone")), route
    )

    assert outcome.result is None
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.RATE_LIMITED
    # One attempt per provider — no tight same-provider retry for a rate limit.
    assert [a.provider for a in outcome.attempts] == [ProviderId.CLAUDE, ProviderId.CODEX]


def test_no_work_falls_back(
    config: OrchestratorConfig,
    integration_security: SecurityConfig,
    fake_cli: Callable[..., str],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # EXPERIMENTAL(no-work-infra) — remove with the feature.
    # A no-work run (raised AGENT_NO_PROGRESS, fallback-eligible) must make the Router fall over to
    # the other provider — the same escape the review/fix machinery never gave it before.
    claude = _build_provider(
        "claude", fake_cli("no_work", "claude"), integration_security, tmp_path
    )
    codex = _build_provider("codex", fake_cli("success", "codex"), integration_security, tmp_path)
    router = AgentRouter(config, {ProviderId.CLAUDE: claude, ProviderId.CODEX: codex})
    route = router.resolve_route("implementation")

    outcome = router.run_stage(
        make_request(node_id="implementation", working_directory=str(tmp_path / "clone")), route
    )

    assert outcome.result is not None and outcome.result.status is RunStatus.SUCCEEDED
    assert outcome.provider_used is ProviderId.CODEX
    assert [a.provider for a in outcome.attempts] == [ProviderId.CLAUDE, ProviderId.CODEX]
    assert outcome.attempts[0].error_class is ErrorClass.AGENT_NO_PROGRESS


def test_no_work_both_providers_terminal(
    config: OrchestratorConfig,
    integration_security: SecurityConfig,
    fake_cli: Callable[..., str],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # EXPERIMENTAL(no-work-infra) — remove with the feature.
    # Both providers do no work: the route exhausts and surfaces a terminal AGENT_NO_PROGRESS
    # (result=None) — which the orchestrator turns into a _fail (not park; it is not park-eligible).
    claude = _build_provider(
        "claude", fake_cli("no_work", "claude"), integration_security, tmp_path
    )
    codex = _build_provider("codex", fake_cli("no_work", "codex"), integration_security, tmp_path)
    router = AgentRouter(config, {ProviderId.CLAUDE: claude, ProviderId.CODEX: codex})
    route = router.resolve_route("implementation")

    outcome = router.run_stage(
        make_request(node_id="implementation", working_directory=str(tmp_path / "clone")), route
    )

    assert outcome.result is None
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.error_class is ErrorClass.AGENT_NO_PROGRESS
    assert [a.provider for a in outcome.attempts] == [ProviderId.CLAUDE, ProviderId.CODEX]


def test_a_degraded_attempt_falls_back_to_the_full_prompt(
    config: OrchestratorConfig,
    make_fake_provider: Callable[..., object],
    make_request: Callable[..., AgentRunRequest],
    tmp_path: Path,
) -> None:
    # The failure this design exists to prevent: "carry on where you left off" arriving in a
    # brand-new session with no rules and no history — a run that reports success and quietly
    # produces worse work. The router clears the session without touching either text, so the
    # substitute attempt selects the full prompt on its own, with no router change at all.
    primary = make_fake_provider(ProviderId.CODEX, raises=ErrorClass.RATE_LIMITED)
    fallback = make_fake_provider(ProviderId.CLAUDE)
    router = AgentRouter(config, {ProviderId.CODEX: primary, ProviderId.CLAUDE: fallback})
    route = router.resolve_route("fixing", ProviderId.CODEX)

    outcome = router.run_stage(
        make_request(
            node_id="fixing",
            prompt="FULL",
            continuation_prompt="CONT",
            session_id="codex-session-123",
        ),
        route,
    )

    assert outcome.provider_used is ProviderId.CLAUDE
    primary_req = primary.requests[0]  # type: ignore[attr-defined]
    fallback_req = fallback.requests[0]  # type: ignore[attr-defined]
    # Both texts are portable, so both survive the switch; only the session is provider-specific.
    assert fallback_req.continuation_prompt == "CONT"
    assert fallback_req.prompt == "FULL"
    assert fallback_req.session_id is None
    assert build_effective_prompt(primary_req).startswith("CONT")
    assert build_effective_prompt(fallback_req).startswith("FULL")
    # And each attempt records the session state it actually ran under.
    assert [a.resumed for a in outcome.attempts] == [True, False]
