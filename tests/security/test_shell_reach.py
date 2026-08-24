"""«У попытки есть шелл» — security/shell_reach.py + адаптерные attempt_has_shell + запрос роутера.

Проверяет предусловие П4.2: брекет детекта кейится на фактическом наличии шелла у этого провайдера
на этом хосте, а не на объявленном гранте git-evidence. Ни один тест не запускает CLI.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from wastech_orchestrator.composition import SHELL_CHECKS
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig, ProviderConfig
from wastech_orchestrator.providers import claude as claude_mod
from wastech_orchestrator.providers import codex as codex_mod
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.providers.claude import SandboxCapability
from wastech_orchestrator.routing.router import AgentRouter
from wastech_orchestrator.security.shell_reach import (
    ShellCheck,
    ShellQuery,
    any_provider_grants_shell,
)


def _checks(
    capability: SandboxCapability = SandboxCapability.MACOS,
) -> dict[ProviderId, ShellCheck]:
    """The bound table with Claude's host capability INJECTED rather than probed.

    The composition table asks the real host, which makes any assertion about Claude's answer a
    statement about the machine running the suite: substituting ``NATIVE_WINDOWS`` turns "a granted
    read-only node has a shell" red. This file holds the cross-host logic, so the host is a
    parameter here exactly as it is in the adapter-level cases below.
    """
    return {
        ProviderId.CLAUDE: lambda cfg, q: claude_mod.attempt_has_shell(
            cfg, q, capability=capability
        ),
        ProviderId.CODEX: SHELL_CHECKS[ProviderId.CODEX],
    }


@pytest.fixture
def base_config(packaged_config_text: str) -> OrchestratorConfig:
    """The packaged config with strict isolation pinned back on.

    These tests are about the CONTRAST between the two isolation values, so the baseline has to be
    stated rather than inherited: the packaged config ships `strict_isolation: false` since
    2026-08-24, which would otherwise make every "no shell here" case silently untrue and the
    advanced-mode case a comparison against itself. The mode is opted into per test.
    """
    cfg = loads_config(packaged_config_text).config
    return replace(cfg, security=replace(cfg.security, strict_isolation=True))


@pytest.fixture
def claude_config(base_config: OrchestratorConfig) -> ProviderConfig:
    return base_config.agents.providers[ProviderId.CLAUDE]


@pytest.fixture
def codex_config(base_config: OrchestratorConfig) -> ProviderConfig:
    return base_config.agents.providers[ProviderId.CODEX]


class _NameOnlyProvider:
    """Enough of a provider for the Router's availability check — the query never runs one."""

    def __init__(self, provider_id: ProviderId) -> None:
        self.id = provider_id.value


def _providers() -> dict[ProviderId, object]:
    return {pid: _NameOnlyProvider(pid) for pid in (ProviderId.CODEX, ProviderId.CLAUDE)}


def _query(
    profile: str | None = None, *, git_evidence: bool = False, strict_isolation: bool = True
) -> ShellQuery:
    return ShellQuery(
        permission_profile=profile, git_evidence=git_evidence, strict_isolation=strict_isolation
    )


# --- adapter answers ------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["read-only", "workspace-write", None])
def test_codex_runs_commands_on_every_profile(codex_config: ProviderConfig, profile: str) -> None:
    # Codex has no shell-less mode: `:read-only` forbids every mutation but still permits command
    # execution — which is why "shell present, fingerprint absent" was a real class before the
    # rekey.
    assert codex_mod.attempt_has_shell(codex_config, _query(profile)) is True


def test_claude_read_only_has_no_shell(claude_config: ProviderConfig) -> None:
    # Read-only maps to a tool set without Bash — a hard tool-level gate, so nothing to bracket.
    assert (
        claude_mod.attempt_has_shell(
            claude_config, _query("read-only"), capability=SandboxCapability.MACOS
        )
        is False
    )


def test_claude_read_only_with_the_grant_has_a_shell(claude_config: ProviderConfig) -> None:
    # The grant adds Bash (scoped to the read-only git verbs) — scoped, but a shell.
    assert (
        claude_mod.attempt_has_shell(
            claude_config,
            _query("read-only", git_evidence=True),
            capability=SandboxCapability.MACOS,
        )
        is True
    )


def test_claude_read_only_has_a_shell_in_the_advanced_mode(claude_config: ProviderConfig) -> None:
    # The link the whole phase turns on, and it is easy to break without noticing: once the mode
    # gives a read-only node a shell, THIS answer is what makes the core resolve the write-deny
    # roots for an attempt that was never meant to write and bracket it for drift. Without it a
    # read-only node would reach `.git` with no explicit deny naming it anywhere.
    for capability in (
        SandboxCapability.MACOS,
        SandboxCapability.NATIVE_WINDOWS,
        SandboxCapability.LINUX_MISSING_DEPS,
    ):
        assert (
            claude_mod.attempt_has_shell(
                claude_config,
                _query("read-only", strict_isolation=False),
                capability=capability,
            )
            is True
        ), capability
    # And the grant is irrelevant to that answer now — declared or not, the shell is there.
    assert (
        claude_mod.attempt_has_shell(
            claude_config,
            _query("read-only", git_evidence=False, strict_isolation=False),
            capability=SandboxCapability.MACOS,
        )
        is True
    )


def test_claude_workspace_write_has_a_shell(claude_config: ProviderConfig) -> None:
    assert (
        claude_mod.attempt_has_shell(
            claude_config, _query("workspace-write"), capability=SandboxCapability.MACOS
        )
        is True
    )


def test_claude_native_windows_drops_the_shell_under_strict_isolation(
    claude_config: ProviderConfig,
) -> None:
    # The answer is host-specific, which is the whole reason it cannot live in the core: with no OS
    # sandbox for Bash, native Windows drops the tool and the attempt has no shell to watch.
    for profile, granted in (("workspace-write", False), ("read-only", True)):
        assert (
            claude_mod.attempt_has_shell(
                claude_config,
                _query(profile, git_evidence=granted),
                capability=SandboxCapability.NATIVE_WINDOWS,
            )
            is False
        )


def test_claude_answers_yes_when_the_attempt_would_be_refused(
    claude_config: ProviderConfig,
) -> None:
    # A supported host missing its sandbox dependencies refuses the attempt pre-model. The honest
    # answer to "would it have run a shell" is yes, and a bracket costs one fingerprint.
    assert (
        claude_mod.attempt_has_shell(
            claude_config,
            _query("workspace-write"),
            capability=SandboxCapability.LINUX_MISSING_DEPS,
        )
        is True
    )


def test_a_node_ceiling_overrides_the_configured_profile(claude_config: ProviderConfig) -> None:
    # The node's resolved ceiling decides, not the provider default: the packaged Claude block is
    # workspace-write, and a read-only node on it still has no shell.
    assert claude_config.permission_profile == "workspace-write"
    assert (
        claude_mod.attempt_has_shell(
            claude_config, _query("read-only"), capability=SandboxCapability.MACOS
        )
        is False
    )


# --- the provider-neutral dispatcher --------------------------------------------------------------


def test_an_unbound_check_counts_as_a_shell(
    base_config: OrchestratorConfig, codex_config: ProviderConfig
) -> None:
    # Fail-closed: an unclassifiable attempt is bracketed rather than left unwatched.
    assert (
        any_provider_grants_shell(
            [ProviderId.CODEX], {ProviderId.CODEX: codex_config}, {}, _query("read-only")
        )
        is True
    )


def test_a_missing_provider_block_counts_as_a_shell(base_config: OrchestratorConfig) -> None:
    assert (
        any_provider_grants_shell([ProviderId.CODEX], {}, SHELL_CHECKS, _query("read-only")) is True
    )


def test_the_dispatcher_answers_from_the_bound_table(base_config: OrchestratorConfig) -> None:
    # Claude read-only alone → no shell; adding Codex (which always has one) flips the answer, which
    # is exactly what a route with a fallback has to do.
    configs = base_config.agents.providers
    claude_only = any_provider_grants_shell(
        [ProviderId.CLAUDE], configs, _checks(), _query("read-only")
    )
    with_codex = any_provider_grants_shell(
        [ProviderId.CLAUDE, ProviderId.CODEX], configs, _checks(), _query("read-only")
    )
    assert (claude_only, with_codex) == (False, True)


# --- the Router's per-route query -----------------------------------------------------------------


def test_the_route_query_asks_both_ends(base_config: OrchestratorConfig) -> None:
    # The bracket is taken before the run, so an infra failure can still move the attempt to the
    # fallback — a different CLI with its own answer. Claude primary + Codex fallback therefore
    # reports a shell even for a read-only node.
    providers = _providers()
    router = AgentRouter(base_config, providers, shell_checks=_checks())
    route = router.resolve_route("review", ProviderId.CLAUDE)
    assert (route.primary, route.fallback) == (ProviderId.CLAUDE, ProviderId.CODEX)
    assert (
        router.route_grants_shell(route, permission_profile="read-only", git_evidence=False) is True
    )


def test_the_route_query_reports_no_shell_when_neither_end_has_one(
    base_config: OrchestratorConfig,
) -> None:
    # With Claude the only allowed provider there is no fallback, so a read-only node without the
    # grant has no shell anywhere on the route and is not bracketed.
    cfg = replace(base_config, agents=replace(base_config.agents, allowed=(ProviderId.CLAUDE,)))
    providers = _providers()
    router = AgentRouter(cfg, providers, shell_checks=_checks())
    route = router.resolve_route("review")
    assert route.fallback is None
    assert (
        router.route_grants_shell(route, permission_profile="read-only", git_evidence=False)
        is False
    )
    assert (
        router.route_grants_shell(route, permission_profile="read-only", git_evidence=True) is True
    )


def test_an_unwired_router_reports_a_shell(base_config: OrchestratorConfig) -> None:
    # No injected table (a unit harness) → fail-closed, same as an unbound check.
    providers = _providers()
    router = AgentRouter(base_config, providers)
    route = router.resolve_route("review")
    assert (
        router.route_grants_shell(route, permission_profile="read-only", git_evidence=False) is True
    )


def test_the_route_query_gives_a_read_only_node_a_shell_in_the_advanced_mode(
    base_config: OrchestratorConfig,
) -> None:
    # End to end, with Claude the ONLY allowed provider so no fallback can supply the answer: the
    # same route that reports no shell on the shipped default reports one in the mode. This is the
    # question the core keys the write-guard resolution and the drift bracket on, so if this ever
    # regresses a read-only node reaches `.git` unwatched and with nothing naming it in a deny.
    claude_only = replace(
        base_config, agents=replace(base_config.agents, allowed=(ProviderId.CLAUDE,))
    )
    strict = AgentRouter(claude_only, _providers(), shell_checks=_checks())
    advanced = AgentRouter(
        replace(claude_only, security=replace(claude_only.security, strict_isolation=False)),
        _providers(),
        shell_checks=_checks(),
    )
    for router, expected in ((strict, False), (advanced, True)):
        route = router.resolve_route("review")
        assert route.fallback is None
        answer = router.route_grants_shell(
            route, permission_profile="read-only", git_evidence=False
        )
        assert answer is expected
