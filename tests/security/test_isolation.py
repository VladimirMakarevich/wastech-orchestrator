"""The fatal isolation gate — security/isolation.py + adapter isolation_reasons.

Proves the deterministic, offline, host-independent check: a provider that may run must have a legal
configuration, so a profile or an ``extra_args`` flag that would weaken, replace or escalate the
owned authority is flagged. None of these launch a CLI. What a *host* cannot enforce is the adjacent
advisory question and lives in ``test_host_floor.py``.
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


def test_the_memory_opt_in_needs_a_resolvable_config_home(
    claude_config: ProviderConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Owner decision 2026-08-20: the opt-in is expressed as a NARROWED write-deny over the config
    # home, so a home nobody can name has no deny — and that home holds the credentials. Refusing
    # the configuration is the only honest answer; a deny over a guessed path protects nothing.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        claude_mod.Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no")))
    )
    opted_in = replace(claude_config, allow_native_memory=True)

    reasons = claude_mod.isolation_reasons(opted_in)

    assert reasons and any("allow_native_memory" in r for r in reasons)
    # With the opt-in off there is nothing to build and nothing to refuse.
    assert claude_mod.isolation_reasons(replace(claude_config, allow_native_memory=False)) == []


def test_the_memory_opt_in_is_legal_with_an_explicit_config_dir(
    claude_config: ProviderConfig, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # The remedy the message names has to work: an absolute `CLAUDE_CONFIG_DIR` needs no home dir.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(
        claude_mod.Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no")))
    )
    assert claude_mod.isolation_reasons(replace(claude_config, allow_native_memory=True)) == []


def test_codex_full_access_sandbox_in_extra_args_is_flagged(codex_config: ProviderConfig) -> None:
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
    # An authority-bearing flag that would select/replace the owned profile/config surface
    # (here ``-c``, which could inject a competing permissions override) is flagged at preflight.
    reasons = codex_mod.isolation_reasons(replace(codex_config, extra_args=("-c", "x=1")))
    assert reasons and any("reserved" in r for r in reasons)


@pytest.mark.parametrize("flag", ["--full-auto", "-a", "--ask-for-approval"])
def test_codex_reserved_approval_extra_arg_is_flagged(
    codex_config: ProviderConfig, flag: str
) -> None:
    # The approval/sandbox-mode selectors are reserved, so the OFFLINE preflight
    # (strict_isolation gate) reports them before any launch — not only the run-time argv builder.
    reasons = codex_mod.isolation_reasons(replace(codex_config, extra_args=(flag, "on-failure")))
    assert reasons and any("reserved" in r for r in reasons)


# --- reserved and escalating Claude extra_args ----------------------------------------------------


def test_claude_weaker_permission_override_is_flagged(claude_config: ProviderConfig) -> None:
    # An escalation short of the outright bypass value: no list of forbidden tokens can recognize
    # it, because whether `acceptEdits` is weaker depends on the profile it is compared against.
    # This is the signal the full-access removal must not have taken with it.
    reasons = claude_mod.isolation_reasons(
        replace(
            claude_config,
            permission_profile="read-only",
            extra_args=("--permission-mode", "acceptEdits"),
        )
    )
    assert reasons and any("weaker" in r for r in reasons)


def test_claude_bypass_mode_is_flagged_by_both_closers(claude_config: ProviderConfig) -> None:
    # Two independent detectors reach the bypass value — the absolute forbidden-args scan (its
    # reason is prefixed "extra_args") and the profile-aware escalation check (appended bare).
    # Keeping both is defence in depth, and the prefix is the only way to tell them apart, since
    # both describe the same token.
    reasons = claude_mod.isolation_reasons(
        replace(claude_config, extra_args=("--permission-mode", "bypassPermissions"))
    )
    assert any(r.startswith("extra_args ") for r in reasons)
    assert any(not r.startswith("extra_args ") for r in reasons)


def test_claude_reserved_extra_arg_is_flagged(claude_config: ProviderConfig) -> None:
    reasons = claude_mod.isolation_reasons(replace(claude_config, extra_args=("--add-dir", "..")))
    assert reasons and any("reserved" in r for r in reasons)


# --- config-level check_isolation -----------------------------------------------------------------


def test_default_config_passes(base_config: OrchestratorConfig) -> None:
    assert check_isolation(base_config, ISOLATION_CHECKS) == []


def test_disable_read_isolation_not_flagged_under_strict(base_config: OrchestratorConfig) -> None:
    # The sanctioned read-isolation opt-out is never a strict_isolation preflight reason — it
    # relaxes only the read side; the write/permission/sandbox ceiling this gate validates stays.
    cfg = replace(
        base_config,
        security=replace(base_config.security, strict_isolation=True, disable_read_isolation=True),
    )
    assert cfg.security.read_isolation_off is True
    assert check_isolation(cfg, ISOLATION_CHECKS) == []


def test_codex_full_access_fails_with_provider_prefix(base_config: OrchestratorConfig) -> None:
    cfg = _with_provider(
        base_config, ProviderId.CODEX, extra_args=("--sandbox", "danger-full-access")
    )
    reasons = check_isolation(cfg, ISOLATION_CHECKS)
    assert reasons and reasons[0].startswith("codex:")


def test_claude_full_access_fails_with_provider_prefix(base_config: OrchestratorConfig) -> None:
    cfg = _with_provider(base_config, ProviderId.CLAUDE, permission_profile="danger-full-access")
    reasons = check_isolation(cfg, ISOLATION_CHECKS)
    assert any(r.startswith("claude:") for r in reasons)


def test_unallowed_provider_is_not_checked(base_config: OrchestratorConfig) -> None:
    # codex carries an illegal flag, but it is not in agents.allowed → must NOT brick the run: the
    # allowlist is the exact set of providers that may run.
    agents = replace(base_config.agents, allowed=(ProviderId.CLAUDE,))
    cfg = _with_provider(
        replace(base_config, agents=agents), ProviderId.CODEX, extra_args=("--yolo",)
    )
    assert check_isolation(cfg, ISOLATION_CHECKS) == []


def test_allowed_provider_is_checked(base_config: OrchestratorConfig) -> None:
    # codex is in agents.allowed (default), so its illegal flag must still be flagged.
    assert ProviderId.CODEX in base_config.agents.allowed
    cfg = _with_provider(base_config, ProviderId.CODEX, extra_args=("--yolo",))
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
