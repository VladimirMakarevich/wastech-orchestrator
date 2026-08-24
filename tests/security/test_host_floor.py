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
from wastech_orchestrator.providers import codex as codex_mod
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.security.isolation import (
    HostFloorCheck,
    check_isolation,
    describe_host_floor,
)

_INCAPABLE = (
    claude_mod.SandboxCapability.NATIVE_WINDOWS,
    claude_mod.SandboxCapability.LINUX_MISSING_DEPS,
)


@pytest.fixture
def base_config(packaged_config_text: str) -> OrchestratorConfig:
    return loads_config(packaged_config_text).config


def _with_strict(config: OrchestratorConfig, strict: bool) -> OrchestratorConfig:
    return replace(config, security=replace(config.security, strict_isolation=strict))


def _table(
    *,
    claude: claude_mod.SandboxCapability = claude_mod.SandboxCapability.MACOS,
    codex_system: str = "Darwin",
) -> dict[ProviderId, HostFloorCheck]:
    """The bound table with BOTH hosts injected, so no assertion depends on the running machine.

    The two providers answer differently shaped questions — Claude's sandbox availability is a
    platform plus two executables, Codex's is decided by its own CLI and, on native Windows, by an
    elevated backend — so each takes its own seam.
    """
    return {
        ProviderId.CLAUDE: lambda: claude_mod.host_floor_gap(capability=claude),
        ProviderId.CODEX: lambda: codex_mod.host_floor_gap(system=codex_system),
    }


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
    base_config: OrchestratorConfig, capability: claude_mod.SandboxCapability, profile: str
) -> None:
    # The old check asked "does this config need a sandbox it cannot get", so it answered
    # differently for a read-only provider default. The floor question is about the machine, and the
    # machine does not change when a profile does.
    #
    # Ам1-11: the parametrization has to reach something that CAN see the profile, or it measures
    # nothing — the provider function does not take one. So the assertion runs through
    # `describe_host_floor` over a config whose Claude block carries each profile in turn: the line
    # must be identical either way, which is the property the review correction asked for.
    providers = dict(base_config.agents.providers)
    claude_cfg = providers[ProviderId.CLAUDE]
    providers[ProviderId.CLAUDE] = replace(claude_cfg, permission_profile=profile)
    config = replace(base_config, agents=replace(base_config.agents, providers=providers))
    lines = describe_host_floor(config, _table(claude=capability))
    assert any(line.startswith("claude: ") for line in lines)
    assert lines == describe_host_floor(base_config, _table(claude=capability))


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


def test_a_capable_host_says_nothing_at_all(base_config: OrchestratorConfig) -> None:
    assert describe_host_floor(base_config, _table()) == ()


def test_a_provider_outside_the_allowlist_is_not_asked(base_config: OrchestratorConfig) -> None:
    # Same rule as the fatal gate: only the providers that may actually run get to produce a line,
    # so a configured-but-unused Claude block never annoys a Codex-only fleet.
    agents = replace(base_config.agents, allowed=(ProviderId.CODEX,))
    table = _table(claude=claude_mod.SandboxCapability.NATIVE_WINDOWS)
    assert describe_host_floor(replace(base_config, agents=agents), table) == ()


# --- Codex: a host whose answer only its own CLI has (Ам1-4 / Ам2-8) ------------------------------


def test_codex_says_a_windows_host_is_not_classifiable_offline() -> None:
    # The requirement is about the HOST, and the table had no Codex entry at all — so a Codex-only
    # park on native Windows printed no floor line, got no preamble paragraph, and met the answer
    # inside its first attempt as a canary refusal.
    gap = codex_mod.host_floor_gap(system="Windows")
    assert gap is not None
    assert "cannot be classified offline" in gap
    # No `--capability-smoke` flag exists — `worc preflight` runs the smoke unconditionally.
    assert "worc preflight" in gap
    assert "--capability-smoke" not in gap
    # And it states the verdict the owner chose, in both directions.
    assert "warning under strict_isolation: false" in gap
    assert "refuses the attempt under strict_isolation: true" in gap


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_codex_says_nothing_on_a_posix_host(system: str) -> None:
    assert codex_mod.host_floor_gap(system=system) is None


def test_a_codex_only_fleet_on_windows_gets_a_floor_line(base_config: OrchestratorConfig) -> None:
    # The end the finding was about: `allowed: [codex]` used to produce an empty report on the one
    # host class where the floor is least certain.
    agents = replace(base_config.agents, allowed=(ProviderId.CODEX,))
    lines = describe_host_floor(replace(base_config, agents=agents), _table(codex_system="Windows"))
    assert len(lines) == 1 and lines[0].startswith("codex: native Windows")


def test_the_bound_table_carries_both_providers() -> None:
    # The composition table is what production uses; a missing entry is silence, not a default.
    assert set(HOST_FLOOR_CHECKS) == {ProviderId.CLAUDE, ProviderId.CODEX}
