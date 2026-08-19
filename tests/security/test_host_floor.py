"""The advisory host-floor verdict — claude.host_floor_gap + security.isolation.describe_host_floor.

Proves the question the fatal gate deliberately does not ask: can an OS-enforced write floor exist
on this host at all? The answer must be host-only (never a function of the configured profile), must
cover both incapable host classes, and must never stop a run — a host without a sandbox is still a
host an operator has to work on. The capability is injected, so both branches run on any OS.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from wastech_orchestrator.composition import HOST_FLOOR_CHECKS
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.providers import claude as claude_mod
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.security.isolation import check_isolation, describe_host_floor

_INCAPABLE = (
    claude_mod.SandboxCapability.NATIVE_WINDOWS,
    claude_mod.SandboxCapability.LINUX_MISSING_DEPS,
)


@pytest.fixture
def base_config(packaged_config_text: str) -> OrchestratorConfig:
    return loads_config(packaged_config_text).config


def _with_strict(config: OrchestratorConfig, strict: bool) -> OrchestratorConfig:
    return replace(config, security=replace(config.security, strict_isolation=strict))


# --- the provider answer --------------------------------------------------------------------------


def test_native_windows_has_no_floor() -> None:
    # Today's code deliberately did not flag native Windows at all: it degrades to a Bash-less mode,
    # which was read as "not a preflight failure" and therefore as nothing to say. The floor is
    # still absent there, and that is what has to be said out loud.
    gap = claude_mod.host_floor_gap(capability=claude_mod.SandboxCapability.NATIVE_WINDOWS)
    assert gap is not None and "Windows" in gap


def test_linux_without_sandbox_deps_has_no_floor_and_names_the_remedy() -> None:
    gap = claude_mod.host_floor_gap(capability=claude_mod.SandboxCapability.LINUX_MISSING_DEPS)
    assert gap is not None and "bubblewrap" in gap and "socat" in gap


@pytest.mark.parametrize(
    "capability",
    [claude_mod.SandboxCapability.MACOS, claude_mod.SandboxCapability.LINUX_AVAILABLE],
)
def test_a_capable_host_reports_nothing(capability: claude_mod.SandboxCapability) -> None:
    assert claude_mod.host_floor_gap(capability=capability) is None


# --- the verdict is host-only, and it is not a refusal --------------------------------------------


@pytest.mark.parametrize("capability", _INCAPABLE)
@pytest.mark.parametrize("profile", ["read-only", "workspace-write"])
def test_the_verdict_ignores_the_configured_profile(
    capability: claude_mod.SandboxCapability, profile: str
) -> None:
    # The old check asked "does this config need a sandbox it cannot get", so it answered
    # differently for a read-only provider default. The floor question is about the machine, and the
    # machine does not change when a profile does.
    assert claude_mod.host_floor_gap(capability=capability) is not None


@pytest.mark.parametrize("capability", _INCAPABLE)
@pytest.mark.parametrize("strict", [True, False])
def test_an_incapable_host_is_never_a_fatal_reason(
    base_config: OrchestratorConfig,
    monkeypatch: pytest.MonkeyPatch,
    capability: claude_mod.SandboxCapability,
    strict: bool,
) -> None:
    # The two questions must not leak into each other: whatever the host cannot do, the fatal gate
    # (which is what stops a run before a branch exists) stays empty for a legal config.
    monkeypatch.setattr(claude_mod, "default_sandbox_probe", lambda: capability)
    config = _with_strict(base_config, strict)
    assert check_isolation(config, {}) == []
    assert describe_host_floor(config, HOST_FLOOR_CHECKS) != ()


@pytest.mark.parametrize("capability", _INCAPABLE)
def test_the_line_states_the_loss_and_matches_the_strict_state(
    base_config: OrchestratorConfig,
    monkeypatch: pytest.MonkeyPatch,
    capability: claude_mod.SandboxCapability,
) -> None:
    monkeypatch.setattr(claude_mod, "default_sandbox_probe", lambda: capability)
    (relaxed,) = describe_host_floor(_with_strict(base_config, False), HOST_FLOOR_CHECKS)
    (strict,) = describe_host_floor(_with_strict(base_config, True), HOST_FLOOR_CHECKS)
    for line in (relaxed, strict):
        assert line.startswith("claude: ")
        assert ".git" in line and ".worc" in line and "state.db" in line
    # The tails differ because the truth does: with the master switch off the shell runs
    # unsandboxed, with it on the shell is withheld instead. One text saying both would be false
    # half the time.
    assert "strict_isolation=false" in relaxed and "unsandboxed" in relaxed
    assert "strict_isolation=true" in strict and "withheld" in strict


def test_a_capable_host_says_nothing_at_all(
    base_config: OrchestratorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        claude_mod, "default_sandbox_probe", lambda: claude_mod.SandboxCapability.MACOS
    )
    assert describe_host_floor(base_config, HOST_FLOOR_CHECKS) == ()


def test_a_provider_outside_the_allowlist_is_not_asked(
    base_config: OrchestratorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same rule as the fatal gate: only the providers that may actually run get to produce a line,
    # so a configured-but-unused Claude block never annoys a Codex-only fleet.
    monkeypatch.setattr(
        claude_mod, "default_sandbox_probe", lambda: claude_mod.SandboxCapability.NATIVE_WINDOWS
    )
    agents = replace(base_config.agents, allowed=(ProviderId.CODEX,))
    assert describe_host_floor(replace(base_config, agents=agents), HOST_FLOOR_CHECKS) == ()
