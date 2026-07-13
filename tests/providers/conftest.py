"""Shared fixtures for provider-adapter tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
from wastech_orchestrator.providers.base import AgentRunRequest


@pytest.fixture
def codex_config() -> ProviderConfig:
    return ProviderConfig(
        command="codex",
        model="",
        timeout_seconds=7200,
        permission_profile="workspace-write",
        extra_args=(),
        sandbox="workspace-write",
    )


@pytest.fixture
def claude_config() -> ProviderConfig:
    return ProviderConfig(
        command="claude",
        model="",
        timeout_seconds=7200,
        permission_profile="workspace-write",
        extra_args=(),
        max_turns=None,
    )


@pytest.fixture
def security_config() -> SecurityConfig:
    return SecurityConfig(
        strict_isolation=True,
        allowed_environment=("PATH", "HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR"),
        denied_read_paths=(".env", "secrets/**"),
        denied_commands=("git commit", "git push", "gh pr create"),
    )


@pytest.fixture
def integration_security() -> SecurityConfig:
    """Like the production allowlist, plus the OS essentials a real child process needs to launch.

    The allowlist *mechanism* is unit-tested in tests/security/test_env.py; integration tests must
    actually spawn a subprocess, so the shell/interpreter needs e.g. SystemRoot/ComSpec on Windows.
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
        allowed_environment=(
            "PATH",
            "HOME",
            "USERPROFILE",
            "CODEX_HOME",
            "CLAUDE_CONFIG_DIR",
            *os_essentials,
        ),
        denied_read_paths=(".env", "secrets/**"),
        denied_commands=("git commit", "git push", "gh pr create"),
    )


@pytest.fixture
def make_request(tmp_path: Any) -> Callable[..., AgentRunRequest]:
    """Factory for an :class:`AgentRunRequest` with sensible defaults, overridable per test."""

    def _make(**overrides: Any) -> AgentRunRequest:
        defaults: dict[str, Any] = {
            "task_id": "task-001",
            "node_id": "planning",
            "working_directory": str(tmp_path / "clone"),
            "prompt": "Implement the requested feature.",
            "permission_profile": "workspace-write",
            "timeout_seconds": 7200,
            "attempt": 1,
            "node_run_id": 1,
        }
        defaults.update(overrides)
        return AgentRunRequest(**defaults)

    return _make
