"""strict_isolation preflight — security/isolation.py + adapter isolation_reasons.

Proves the deterministic, offline check: a provider that may run must be able to enable its required
isolation; ``extra_args`` or a profile/sandbox that would weaken it is flagged. None of these launch
a CLI.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from wastech_orchestrator.composition import ISOLATION_CHECKS
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig, ProviderConfig
from wastech_orchestrator.providers import claude as claude_mod
from wastech_orchestrator.providers import codex as codex_mod
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.security.isolation import check_isolation


@pytest.fixture
def base_config(packaged_config_text: str) -> OrchestratorConfig:
    return loads_config(packaged_config_text).config


@pytest.fixture
def claude_config(base_config: OrchestratorConfig) -> ProviderConfig:
    return base_config.agents.providers[ProviderId.CLAUDE]


@pytest.fixture
def codex_config(base_config: OrchestratorConfig) -> ProviderConfig:
    return base_config.agents.providers[ProviderId.CODEX]


def _with_provider(
    config: OrchestratorConfig, pid: ProviderId, **overrides: object
) -> OrchestratorConfig:
    providers = dict(config.agents.providers)
    providers[pid] = replace(providers[pid], **overrides)
    return replace(config, agents=replace(config.agents, providers=providers))


# --- adapter-level isolation_reasons --------------------------------------------------------------


def test_claude_clean_config_has_no_reasons(claude_config: ProviderConfig) -> None:
    assert claude_mod.isolation_reasons(claude_config) == []


def test_codex_clean_config_has_no_reasons(codex_config: ProviderConfig) -> None:
    assert codex_mod.isolation_reasons(codex_config) == []


def test_claude_unknown_profile_is_flagged(claude_config: ProviderConfig) -> None:
    reasons = claude_mod.isolation_reasons(replace(claude_config, permission_profile="bogus"))
    assert reasons and "bogus" in reasons[0]


def test_claude_full_access_profile_is_flagged(claude_config: ProviderConfig) -> None:
    reasons = claude_mod.isolation_reasons(
        replace(claude_config, permission_profile="danger-full-access")
    )
    assert reasons


def test_claude_skip_permissions_extra_arg_is_flagged(claude_config: ProviderConfig) -> None:
    reasons = claude_mod.isolation_reasons(
        replace(claude_config, extra_args=("--dangerously-skip-permissions",))
    )
    assert reasons


def test_claude_bypass_permission_mode_extra_arg_is_flagged(claude_config: ProviderConfig) -> None:
    reasons = claude_mod.isolation_reasons(
        replace(claude_config, extra_args=("--permission-mode", "bypassPermissions"))
    )
    assert reasons


def test_codex_danger_full_access_sandbox_is_flagged(codex_config: ProviderConfig) -> None:
    reasons = codex_mod.isolation_reasons(replace(codex_config, sandbox="danger-full-access"))
    assert reasons and "danger-full-access" in reasons[0]


def test_codex_full_access_sandbox_in_extra_args_is_flagged(codex_config: ProviderConfig) -> None:
    # Full access selected via extra_args (not the sandbox field) is also reported as "no isolation"
    # — the structured selector is gated, not absolutely banned (provider-config-cleanup #1).
    reasons = codex_mod.isolation_reasons(
        replace(codex_config, extra_args=("--sandbox", "danger-full-access"))
    )
    assert reasons and any("danger-full-access" in r for r in reasons)


def test_codex_bypass_extra_arg_is_flagged(codex_config: ProviderConfig) -> None:
    reasons = codex_mod.isolation_reasons(
        replace(codex_config, extra_args=("--dangerously-bypass-approvals-and-sandbox",))
    )
    assert reasons


def test_codex_reserved_extra_arg_is_flagged(codex_config: ProviderConfig) -> None:
    # WRI-003: an authority-bearing flag that would select/replace the owned profile/config surface
    # (here ``-c``, which could inject a competing permissions override) is flagged at preflight.
    reasons = codex_mod.isolation_reasons(replace(codex_config, extra_args=("-c", "x=1")))
    assert reasons and any("reserved" in r for r in reasons)


@pytest.mark.parametrize("flag", ["--full-auto", "-a", "--ask-for-approval"])
def test_codex_reserved_approval_extra_arg_is_flagged(
    codex_config: ProviderConfig, flag: str
) -> None:
    # C2 (WRI-003 AC6): the approval/sandbox-mode selectors are reserved, so the OFFLINE preflight
    # (strict_isolation gate) reports them before any launch — not only the run-time argv builder.
    reasons = codex_mod.isolation_reasons(replace(codex_config, extra_args=(flag, "on-failure")))
    assert reasons and any("reserved" in r for r in reasons)


# --- WRI-002: host-aware sandbox availability + reserved Claude extra_args
# -------------------------


def test_claude_workspace_write_missing_sandbox_deps_is_flagged(
    claude_config: ProviderConfig,
) -> None:
    # A configured workspace-write Claude on a Linux/WSL2 host missing bubblewrap+socat cannot get
    # its required Bash sandbox → strict_isolation preflight must flag it (offline, no CLI
    # launched).
    reasons = claude_mod.isolation_reasons(
        replace(claude_config, permission_profile="workspace-write"),
        capability=claude_mod.SandboxCapability.LINUX_MISSING_DEPS,
    )
    assert reasons and any("bubblewrap" in r for r in reasons)


def test_claude_read_only_missing_sandbox_deps_is_clean(claude_config: ProviderConfig) -> None:
    # A read-only node needs no Bash sandbox, so a sandbox-less host is not flagged.
    reasons = claude_mod.isolation_reasons(
        replace(claude_config, permission_profile="read-only"),
        capability=claude_mod.SandboxCapability.LINUX_MISSING_DEPS,
    )
    assert reasons == []


def test_claude_native_windows_workspace_write_not_flagged(claude_config: ProviderConfig) -> None:
    # Native Windows degrades to a Bash-less restricted mode — not a preflight failure.
    reasons = claude_mod.isolation_reasons(
        replace(claude_config, permission_profile="workspace-write"),
        capability=claude_mod.SandboxCapability.NATIVE_WINDOWS,
    )
    assert reasons == []


def test_claude_reserved_extra_arg_is_flagged(claude_config: ProviderConfig) -> None:
    reasons = claude_mod.isolation_reasons(replace(claude_config, extra_args=("--add-dir", "..")))
    assert reasons and any("reserved" in r for r in reasons)


# --- config-level check_isolation -----------------------------------------------------------------


def test_default_config_passes(base_config: OrchestratorConfig) -> None:
    assert check_isolation(base_config, ISOLATION_CHECKS) == []


def test_disable_read_isolation_not_flagged_under_strict(base_config: OrchestratorConfig) -> None:
    # VF-6: the sanctioned read-isolation opt-out is never a strict_isolation preflight reason — it
    # relaxes only the read side; the write/permission/sandbox ceiling this gate validates stays.
    cfg = replace(
        base_config,
        security=replace(base_config.security, strict_isolation=True, disable_read_isolation=True),
    )
    assert cfg.security.read_isolation_off is True
    assert check_isolation(cfg, ISOLATION_CHECKS) == []


def test_codex_full_access_fails_with_provider_prefix(base_config: OrchestratorConfig) -> None:
    cfg = _with_provider(base_config, ProviderId.CODEX, sandbox="danger-full-access")
    reasons = check_isolation(cfg, ISOLATION_CHECKS)
    assert reasons and reasons[0].startswith("codex:")


def test_claude_full_access_fails_with_provider_prefix(base_config: OrchestratorConfig) -> None:
    cfg = _with_provider(base_config, ProviderId.CLAUDE, permission_profile="danger-full-access")
    reasons = check_isolation(cfg, ISOLATION_CHECKS)
    assert any(r.startswith("claude:") for r in reasons)


def test_unallowed_provider_is_not_checked(base_config: OrchestratorConfig) -> None:
    # codex has a forbidden sandbox, but it is not in agents.allowed → must NOT brick the run (PRE.1
    # makes the allowlist the exact set of providers that may run).
    agents = replace(base_config.agents, allowed=(ProviderId.CLAUDE,))
    cfg = _with_provider(
        replace(base_config, agents=agents), ProviderId.CODEX, sandbox="danger-full-access"
    )
    assert check_isolation(cfg, ISOLATION_CHECKS) == []


def test_allowed_provider_is_checked(base_config: OrchestratorConfig) -> None:
    # codex is in agents.allowed (default), so its bad sandbox must still be flagged.
    assert ProviderId.CODEX in base_config.agents.allowed
    cfg = _with_provider(base_config, ProviderId.CODEX, sandbox="danger-full-access")
    assert any(r.startswith("codex:") for r in check_isolation(cfg, ISOLATION_CHECKS))


def test_allow_git_evidence_not_flagged_under_strict(base_config: OrchestratorConfig) -> None:
    # The grant does not relax the ceiling, so it is never a strict_isolation preflight reason. The
    # host check that matters for it is per-attempt and lives in the adapter: a granted shell on a
    # host that cannot sandbox it is refused there (CAPABILITY_UNAVAILABLE), with the node's
    # declaration in hand. This gate sees only provider config and would over-flag every run.
    cfg = replace(
        base_config,
        security=replace(base_config.security, strict_isolation=True, allow_git_evidence=True),
    )
    assert check_isolation(cfg, ISOLATION_CHECKS) == []
