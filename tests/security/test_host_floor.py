"""The advisory floor verdict — claude.host_floor_gap + security.isolation.describe_host_floor.

Proves the question the fatal gate deliberately does not ask: does an OS-enforced write floor exist
for this run at all? Three things remove it and all three are pinned here — the two incapable host
classes, and the advanced mode, which raises no sandbox on any host (owner decision 2026-09-03).

That decision revoked one clause of this file's original charter: the answer is no longer host-ONLY,
because a host-only answer would report a floor the mode does not have. What survives unchanged is
everything the clause was protecting — the verdict still ignores the configured profile, and it
still must never stop a run, on any host at either posture. The one configuration value it now reads
is ``strict_isolation``, and the Codex half is pinned as mode-INdependent, because that asymmetry is
the mode's most misreadable property.

Both the capability and the Codex platform are injected, so every branch runs on any OS. Note that
``base_config`` is the shipped example config, whose ``strict_isolation`` is ``false`` — so it is
the MODE, and a test about a capable host has to say ``_with_strict(..., True)`` to mean one.
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
        ProviderId.CLAUDE: lambda *, strict_isolation: claude_mod.host_floor_gap(
            strict_isolation=strict_isolation, capability=claude
        ),
        ProviderId.CODEX: lambda *, strict_isolation: codex_mod.host_floor_gap(
            strict_isolation=strict_isolation, system=codex_system
        ),
    }


# --- the provider answer --------------------------------------------------------------------------


def test_native_windows_has_no_floor() -> None:
    # Today's code deliberately did not flag native Windows at all: it degrades to a Bash-less mode,
    # which was read as "not a preflight failure" and therefore as nothing to say. The floor is
    # still absent there, and that is what has to be said out loud.
    gap = claude_mod.host_floor_gap(
        strict_isolation=True, capability=claude_mod.SandboxCapability.NATIVE_WINDOWS
    )
    assert gap is not None and "Windows" in gap


def test_linux_without_sandbox_deps_has_no_floor_and_names_the_remedy() -> None:
    gap = claude_mod.host_floor_gap(
        strict_isolation=True, capability=claude_mod.SandboxCapability.LINUX_MISSING_DEPS
    )
    assert gap is not None and "bubblewrap" in gap and "socat" in gap


@pytest.mark.parametrize(
    "capability",
    [claude_mod.SandboxCapability.MACOS, claude_mod.SandboxCapability.LINUX_AVAILABLE],
)
def test_a_capable_host_reports_nothing(capability: claude_mod.SandboxCapability) -> None:
    assert claude_mod.host_floor_gap(strict_isolation=True, capability=capability) is None


@pytest.mark.parametrize("capability", list(claude_mod.SandboxCapability))
def test_the_mode_has_no_floor_on_any_host(capability: claude_mod.SandboxCapability) -> None:
    # The regression this whole change exists for: `needs_sandbox` was computed from the tool plan
    # and the host and never from the mode, so on macOS and on a dependency-complete Linux the mode
    # promised "no restrictions" and shipped a sandbox. Now the answer is the same on all four
    # classes, and it names the mode rather than the machine — because the machine is not what is
    # missing.
    gap = claude_mod.host_floor_gap(strict_isolation=False, capability=capability)
    assert gap is not None, capability
    assert "advanced mode" in gap and "security.strict_isolation: false" in gap
    # No remedy is offered: the remedy is the operator's own posture decision, not a missing
    # dependency, so the line must not read like something to go and install.
    assert "install" not in gap, capability


def test_the_mode_and_an_incapable_host_do_not_produce_two_reasons() -> None:
    # The mode answer is checked first, so a native-Windows host in the mode gets one sentence
    # rather than a pile. Which one is deliberate: the mode is the reason on every host, and
    # "install bubblewrap" would be actively wrong advice there.
    gap = claude_mod.host_floor_gap(
        strict_isolation=False, capability=claude_mod.SandboxCapability.LINUX_MISSING_DEPS
    )
    assert gap is not None and "advanced mode" in gap
    assert "bubblewrap" not in gap


# --- the verdict is host-only, and it is not a refusal --------------------------------------------


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("capability", _INCAPABLE)
@pytest.mark.parametrize("profile", ["read-only", "workspace-write"])
def test_the_verdict_ignores_the_configured_profile(
    base_config: OrchestratorConfig,
    capability: claude_mod.SandboxCapability,
    profile: str,
    strict: bool,
) -> None:
    # The old check asked "does this config need a sandbox it cannot get", so it answered
    # differently for a read-only provider default. The floor question is about the machine, and the
    # machine does not change when a profile does.
    #
    # The parametrization has to reach something that CAN see the profile, or it measures
    # nothing — the provider function does not take one. So the assertion runs through
    # `describe_host_floor` over a config whose Claude block carries each profile in turn: the line
    # must be identical either way, which is the property the review correction asked for.
    providers = dict(base_config.agents.providers)
    claude_cfg = providers[ProviderId.CLAUDE]
    providers[ProviderId.CLAUDE] = replace(claude_cfg, permission_profile=profile)
    config = _with_strict(
        replace(base_config, agents=replace(base_config.agents, providers=providers)), strict
    )
    lines = describe_host_floor(config, _table(claude=capability))
    assert any(line.startswith("claude: ") for line in lines)
    assert lines == describe_host_floor(
        _with_strict(base_config, strict), _table(claude=capability)
    )


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
    capability: claude_mod.SandboxCapability,
) -> None:
    # Through the injected table, not HOST_FLOOR_CHECKS: the unpacking below expects the Claude
    # line alone, and on a native-Windows host the real Codex probe contributes a second one — so
    # the live table would make this test's arity a property of the machine running it. `_table`
    # pins Codex to a capable host, which is the whole reason the file has that seam.
    (relaxed,) = describe_host_floor(_with_strict(base_config, False), _table(claude=capability))
    (strict,) = describe_host_floor(_with_strict(base_config, True), _table(claude=capability))
    for line in (relaxed, strict):
        assert line.startswith("claude: ")
        assert ".git" in line and ".worc" in line and "state.db" in line
    # The tails differ because the truth does: with the master switch off the shell runs
    # unsandboxed, with it on the shell is withheld instead. One text saying both would be false
    # half the time.
    assert "strict_isolation=false" in relaxed and "unsandboxed" in relaxed
    assert "strict_isolation=true" in strict and "withheld" in strict


def test_a_capable_host_says_nothing_at_all_under_strict_isolation(
    base_config: OrchestratorConfig,
) -> None:
    assert describe_host_floor(_with_strict(base_config, True), _table()) == ()


def test_a_capable_host_gets_the_line_in_the_mode(base_config: OrchestratorConfig) -> None:
    # The counterweight, and the end-to-end shape of the fix: the same macOS host that says nothing
    # under strict isolation must produce a full line in the mode — Claude's only, since Codex
    # keeps its generated profile there. `base_config` is already the mode (the shipped default).
    (line,) = describe_host_floor(base_config, _table())
    assert line.startswith("claude: ") and "advanced mode" in line
    assert ".git" in line and ".worc" in line and "state.db" in line
    assert "strict_isolation=false" in line and "unsandboxed" in line


def test_a_provider_outside_the_allowlist_is_not_asked(base_config: OrchestratorConfig) -> None:
    # Same rule as the fatal gate: only the providers that may actually run get to produce a line,
    # so a configured-but-unused Claude block never annoys a Codex-only fleet.
    agents = replace(base_config.agents, allowed=(ProviderId.CODEX,))
    table = _table(claude=claude_mod.SandboxCapability.NATIVE_WINDOWS)
    assert describe_host_floor(replace(base_config, agents=agents), table) == ()


# --- Codex: a host whose answer only its own CLI has ----------------------------------------------


def test_codex_says_a_windows_host_is_not_classifiable_offline() -> None:
    # The requirement is about the HOST, and the table had no Codex entry at all — so a Codex-only
    # park on native Windows printed no floor line, got no preamble paragraph, and met the answer
    # inside its first attempt as a canary refusal.
    gap = codex_mod.host_floor_gap(strict_isolation=True, system="Windows")
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
    assert codex_mod.host_floor_gap(strict_isolation=True, system=system) is None


@pytest.mark.parametrize("system", ["Darwin", "Linux", "Windows"])
def test_codex_answers_the_same_at_both_postures(system: str) -> None:
    # The asymmetry, pinned where it cannot be missed: the mode removes Claude's sandbox and NOT
    # Codex's, which always gets a generated permission profile with no opt-out (the one selector
    # that would remove it, `danger-full-access`, is absolutely forbidden). "No restrictions in the
    # advanced mode" is therefore true of Claude and false of Codex, and a future change that made
    # this parameter matter here has to come past this test and say so.
    assert codex_mod.host_floor_gap(
        strict_isolation=False, system=system
    ) == codex_mod.host_floor_gap(strict_isolation=True, system=system)


def test_a_codex_only_fleet_on_windows_gets_a_floor_line(base_config: OrchestratorConfig) -> None:
    # `allowed: [codex]` must not produce an empty report on the one host class where the floor is
    # least certain.
    agents = replace(base_config.agents, allowed=(ProviderId.CODEX,))
    lines = describe_host_floor(replace(base_config, agents=agents), _table(codex_system="Windows"))
    assert len(lines) == 1 and lines[0].startswith("codex: native Windows")


def test_the_bound_table_carries_both_providers() -> None:
    # The composition table is what production uses; a missing entry is silence, not a default.
    assert set(HOST_FLOOR_CHECKS) == {ProviderId.CLAUDE, ProviderId.CODEX}


def test_the_live_table_answers_the_same_on_every_host_the_suite_runs_on(
    base_config: OrchestratorConfig,
) -> None:
    """The real table, uninjected, must not make an assertion's arity a property of the runner.

    Every test that reads ``HOST_FLOOR_CHECKS`` without injecting a host inherits whatever the
    machine says, and that is not hypothetical: three floor tests asserted a single line, passed on
    macOS and Linux, and failed only on the native-Windows runner, where the real Codex probe
    contributed a second one. The suite pins both provider hosts in one autouse fixture so the live
    table is deterministic; this is the guard on that promise, and it fails on Windows the moment
    either half of the pin is dropped.
    """
    lines = describe_host_floor(_with_strict(base_config, False), HOST_FLOOR_CHECKS)
    assert len(lines) == 1 and lines[0].startswith("claude: the advanced mode")
    assert describe_host_floor(_with_strict(base_config, True), HOST_FLOOR_CHECKS) == ()
