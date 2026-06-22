"""Router integration scenarios (phase doc "Tests").

Two scenarios run the **real** Codex/Claude adapters over the deterministic fake CLI (building on
the P2/P3 harness) to prove end-to-end wiring: a successful infra-fallback, and fallback denied on a
quality failure. The third — an infra failure after files changed — asserts the contract on the
request the router hands the fallback, so it uses recording in-memory providers (the diff-passing is
a router/Core data-exchange concern, best observed on the ``AgentRunRequest`` itself).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import (
    OrchestratorConfig,
    ProviderConfig,
    SecurityConfig,
)
from wastech_orchestrator.providers.base import (
    AgentProvider,
    AgentRunRequest,
    ErrorClass,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.providers.claude import ClaudeCodeProvider
from wastech_orchestrator.providers.codex import CodexProvider
from wastech_orchestrator.routing.router import AgentRouter
from wastech_orchestrator.routing.snapshots import PartialChange, WorkingTreeSnapshot


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
            sandbox="workspace-write",
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
