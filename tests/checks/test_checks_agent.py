"""Agent-assisted discovery + provider selection (automatic check discovery §6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wastech_orchestrator.checks.agent import DISCOVERY_OUTPUT_SCHEMA, AgentCheckDiscovery
from wastech_orchestrator.checks.discovery_factory import (
    build_discovery,
    select_discovery_provider,
)
from wastech_orchestrator.checks.inspect import RepositoryEvidence
from wastech_orchestrator.checks.model import CheckSource
from wastech_orchestrator.config.schema import CheckDiscoveryConfig
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

_DOC = {
    "checks": [
        {
            "name": "tests",
            "argv": [".venv/bin/python", "-m", "pytest"],
            "evidence": ["pytest in pyproject.toml"],
            "confidence": "high",
        }
    ],
}


class _FakeProvider:
    """A deterministic AgentProvider stub (no CLI) for discovery unit tests."""

    def __init__(
        self,
        *,
        provider_id: str = "claude",
        structured: dict[str, Any] | None = None,
        status: RunStatus = RunStatus.SUCCEEDED,
        raises: ProviderError | None = None,
        found: bool = True,
    ) -> None:
        self.id = provider_id
        self._structured = structured
        self._status = status
        self._raises = raises
        self._found = found
        self.last_request: AgentRunRequest | None = None

    def preflight(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.id,
            executable_found=self._found,
            version="1",
            authenticated=True,
            supports_required_features=True,
            message="ok",
        )

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.last_request = request
        if self._raises is not None:
            raise self._raises
        return AgentRunResult(
            status=self._status,
            provider=self.id,
            stage=request.stage,
            attempt=1,
            exit_code=0,
            started_at="t",
            finished_at="t",
            structured_output=self._structured,
        )


def _cfg(**kw: Any) -> CheckDiscoveryConfig:
    return CheckDiscoveryConfig(model="claude-haiku-4-5-20251001", **kw)


def _discovery(provider: _FakeProvider, tmp_path: Path) -> AgentCheckDiscovery:
    return AgentCheckDiscovery(provider, discovery_cfg=_cfg(), artifacts_root=tmp_path)


def test_valid_structured_output_becomes_agent_candidates(tmp_path: Path) -> None:
    provider = _FakeProvider(structured=_DOC)
    candidates = _discovery(provider, tmp_path).discover(tmp_path, RepositoryEvidence(tmp_path))
    assert len(candidates) == 1
    assert candidates[0].source is CheckSource.AGENT
    assert candidates[0].argv == (".venv/bin/python", "-m", "pytest")


def test_request_carries_cheap_model_and_readonly_profile(tmp_path: Path) -> None:
    provider = _FakeProvider(structured=_DOC)
    _discovery(provider, tmp_path).discover(tmp_path, RepositoryEvidence(tmp_path))
    request = provider.last_request
    assert request is not None
    assert request.permission_profile == "read-only"
    assert request.output_schema == DISCOVERY_OUTPUT_SCHEMA
    assert request.model == "claude-haiku-4-5-20251001"
    assert request.reasoning == "low"
    assert request.stage is Stage.PLANNING


def test_provider_error_returns_empty(tmp_path: Path) -> None:
    provider = _FakeProvider(raises=ProviderError(ErrorClass.TIMEOUT, "boom"))
    assert _discovery(provider, tmp_path).discover(tmp_path, RepositoryEvidence(tmp_path)) == ()


def test_failed_status_returns_empty(tmp_path: Path) -> None:
    provider = _FakeProvider(structured=_DOC, status=RunStatus.FAILED)
    assert _discovery(provider, tmp_path).discover(tmp_path, RepositoryEvidence(tmp_path)) == ()


def test_malformed_output_returns_empty(tmp_path: Path) -> None:
    provider = _FakeProvider(structured={"setup": []})  # no checks
    assert _discovery(provider, tmp_path).discover(tmp_path, RepositoryEvidence(tmp_path)) == ()


def test_unsafe_argv_returns_empty(tmp_path: Path) -> None:
    unsafe = {"checks": [{"name": "t", "argv": ["pytest;", "rm"], "confidence": "high"}]}
    provider = _FakeProvider(structured=unsafe)
    assert _discovery(provider, tmp_path).discover(tmp_path, RepositoryEvidence(tmp_path)) == ()


def test_select_provider_prefers_explicit(tmp_path: Path) -> None:
    from dataclasses import replace

    from wastech_orchestrator.config.loader import loads_config

    config = loads_config(
        "repo:\n  url: x\nagents:\n  allowed: [claude, codex]\n"
        "  providers:\n    claude:\n      command: claude\n    codex:\n      command: codex\n"
    ).config
    config = replace(
        config, checks=replace(config.checks, discovery=_cfg(provider=ProviderId.CODEX))
    )
    providers = {
        ProviderId.CLAUDE: _FakeProvider(provider_id="claude"),
        ProviderId.CODEX: _FakeProvider(provider_id="codex"),
    }
    chosen = select_discovery_provider(config, providers)  # type: ignore[arg-type]
    assert chosen is providers[ProviderId.CODEX]


def test_build_discovery_is_none_without_model(tmp_path: Path) -> None:
    from dataclasses import replace

    from wastech_orchestrator.config.loader import loads_config

    config = loads_config(
        "repo:\n  url: x\nagents:\n  allowed: [claude]\n"
        "  providers:\n    claude:\n      command: claude\n"
    ).config
    config = replace(
        config, checks=replace(config.checks, discovery=CheckDiscoveryConfig(model=""))
    )
    providers = {ProviderId.CLAUDE: _FakeProvider()}
    assert build_discovery(config, providers, tmp_path) is None  # type: ignore[arg-type]
