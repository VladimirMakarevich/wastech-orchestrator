"""Fixtures for Agent Router tests (Phase 4).

Provides an in-memory recording ``FakeProvider`` (to drive route/fallback/attempt logic
deterministically), a ``FakeSnapshotHook`` (the contract), an ``AgentRunRequest`` factory, the
real default config (loaded from the packaged example), and the integration security allowlist. The
root ``fake_cli`` fixture (tests/conftest.py) is reused for the real-provider integration tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig, SecurityConfig
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AgentRunResult,
    ErrorClass,
    NormalizedError,
    ProviderError,
    ProviderHealth,
    ProviderId,
    RunStatus,
)
from wastech_orchestrator.routing.snapshots import PartialChange, WorkingTreeSnapshot

_T0 = "2026-01-01T00:00:00+00:00"
_T1 = "2026-01-01T00:00:01+00:00"


class FakeProvider:
    """In-memory :class:`AgentProvider` that records each request and either raises or returns.

    Configure with ``raises=<ErrorClass>`` to simulate an infrastructure ``ProviderError``, or
    ``status=<RunStatus>`` to return that result (a quality failure when ``FAILED``). Records every
    received :class:`AgentRunRequest` in ``requests`` so tests can assert what the fallback got.
    """

    def __init__(
        self,
        provider_id: ProviderId,
        *,
        raises: ErrorClass | None = None,
        status: RunStatus = RunStatus.SUCCEEDED,
    ) -> None:
        self.id = provider_id.value
        self._provider_id = provider_id
        self._raises = raises
        self._status = status
        self.requests: list[AgentRunRequest] = []

    @property
    def run_count(self) -> int:
        return len(self.requests)

    def preflight(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.id,
            executable_found=True,
            version="fake-1.0",
            authenticated=True,
            supports_required_features=True,
            message="fake",
        )

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.requests.append(request)
        if self._raises is not None:
            raise ProviderError(self._raises, f"fake {self._raises.value}")
        error = (
            NormalizedError(ErrorClass.TASK_FAILURE, "fake quality failure")
            if self._status is RunStatus.FAILED
            else None
        )
        return AgentRunResult(
            status=self._status,
            provider=self.id,
            node_id=request.node_id,
            attempt=request.attempt,
            exit_code=0,
            started_at=_T0,
            finished_at=_T1,
            final_message="fake message",
            error=error,
        )


class FakeSnapshotHook:
    """A deterministic :class:`SnapshotHook`. Records calls; has no rollback by design."""

    def __init__(self, partial: PartialChange | None = None) -> None:
        self._partial = partial
        self.capture_calls = 0
        self.partial_calls = 0

    def capture(self) -> WorkingTreeSnapshot:
        self.capture_calls += 1
        return WorkingTreeSnapshot(
            commit_sha="sha-before",
            porcelain_status="",
            diff_checksum="checksum-before",
            artifacts=(),
        )

    def partial_change_since(self, before: WorkingTreeSnapshot) -> PartialChange | None:
        self.partial_calls += 1
        return self._partial


@pytest.fixture
def config(packaged_config_text: str) -> OrchestratorConfig:
    """The packaged default config — canonical routing, both providers, max_stage_attempts=3.

    ``packaged_config_text`` comes from the root tests/conftest.py.
    """
    return loads_config(packaged_config_text).config


@pytest.fixture
def make_fake_provider() -> Callable[..., FakeProvider]:
    def _make(
        provider_id: ProviderId,
        *,
        raises: ErrorClass | None = None,
        status: RunStatus = RunStatus.SUCCEEDED,
    ) -> FakeProvider:
        return FakeProvider(provider_id, raises=raises, status=status)

    return _make


@pytest.fixture
def make_snapshot_hook() -> Callable[..., FakeSnapshotHook]:
    def _make(partial: PartialChange | None = None) -> FakeSnapshotHook:
        return FakeSnapshotHook(partial)

    return _make


@pytest.fixture
def make_request() -> Callable[..., AgentRunRequest]:
    """Factory for an :class:`AgentRunRequest` with sensible defaults, overridable per test."""

    def _make(**overrides: Any) -> AgentRunRequest:
        defaults: dict[str, Any] = {
            "task_id": "task-001",
            "node_id": "review",
            "working_directory": "/tmp/clone",
            "prompt": "Do the stage.",
            "permission_profile": "workspace-write",
            "timeout_seconds": 7200,
            "attempt": 1,
            "node_run_id": 1,
        }
        defaults.update(overrides)
        return AgentRunRequest(**defaults)

    return _make


@pytest.fixture
def integration_security() -> SecurityConfig:
    """Production-like allowlist plus the OS essentials a real child process needs to launch.

    Mirrors tests/providers/conftest.py (not visible from this package). The allowlist mechanism is
    unit-tested in tests/security/test_env.py; here we must actually spawn a subprocess.
    """
    os_essentials = (
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
    )
    return SecurityConfig(
        strict_isolation=True,
        allowed_environment=("PATH", "HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
        + os_essentials,
        denied_read_paths=(".env", "secrets/**"),
        denied_commands=("git commit", "git push", "gh pr create"),
    )
