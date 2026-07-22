"""Composition root: build the concrete provider adapters and wire the full Orchestrator graph.

This is the one module allowed to import the concrete CLI adapters
(:mod:`wastech_orchestrator.providers.claude` / :mod:`wastech_orchestrator.providers.codex`).
Keeping :func:`build_providers` and :func:`build_orchestrator` here — out of ``core`` — is what lets
the ``core-not-concrete-adapters`` import-linter contract stay green with no exception: the Core
depends only on the ``AgentProvider`` interface, never on a specific adapter. The CLI (and the e2e
tests) construct their Orchestrator through these factories.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from wastech_orchestrator.check_runner import CheckRunner
from wastech_orchestrator.checks.resolver import CheckResolver
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.orchestrator import Orchestrator
from wastech_orchestrator.git_manager import GitManager
from wastech_orchestrator.ledger import Ledger
from wastech_orchestrator.notify import build_notifier
from wastech_orchestrator.providers import claude, codex
from wastech_orchestrator.providers.base import AgentProvider, ProviderId
from wastech_orchestrator.providers.process import AgentHandleRecorder
from wastech_orchestrator.routing.router import AgentRouter
from wastech_orchestrator.runtime_layout import (
    CONTROL_BUNDLE_DIRNAME,
    InternalDenyPolicy,
    RuntimeLayout,
)
from wastech_orchestrator.security.isolation import IsolationCheck
from wastech_orchestrator.state_store import StateStore
from wastech_orchestrator.task.validation_gate import ValidationGate

# ProviderId → offline isolation check, bound here (the composition root) so ``core`` and the
# CLI preflight can run the ``strict_isolation`` gate without importing a concrete adapter.
ISOLATION_CHECKS: dict[ProviderId, IsolationCheck] = {
    ProviderId.CLAUDE: claude.isolation_reasons,
    ProviderId.CODEX: codex.isolation_reasons,
}

# ProviderId → its config/credential home resolver, bound here (the composition root) so the
# provider-neutral :class:`RuntimeLayout` never learns a provider-specific path. Used to populate
# the WRI-004 :class:`InternalDenyPolicy` with the operator-owned auth homes to deny.
_PROVIDER_CONFIG_HOMES: dict[ProviderId, Callable[[], Path]] = {
    ProviderId.CLAUDE: claude.claude_config_home,
    ProviderId.CODEX: codex.codex_config_home,
}


def build_internal_deny_policy(
    config: OrchestratorConfig,
    layout: RuntimeLayout,
    *,
    env_file: Path | None = None,
) -> InternalDenyPolicy:
    """Assemble the WRI-004 internal deny policy at the composition boundary.

    Collects the control/private homes from the provider-neutral ``layout``, the resolved
    default/explicit ``env_file`` (which may live outside ``private_home``), the config or
    credential homes of the *configured* providers (:data:`_PROVIDER_CONFIG_HOMES`), and the WRI-010
    frozen-control-bundle root (``<private_home>/control-bundles``). Resolving the provider homes
    here — not inside :class:`RuntimeLayout` — keeps the layout provider-neutral.

    WRI-004/010 only represent these targets; WRI-002/003 project them into provider enforcement.
    """
    provider_homes = tuple(
        _PROVIDER_CONFIG_HOMES[pid]()
        for pid in config.agents.providers
        if pid in _PROVIDER_CONFIG_HOMES
    )
    return InternalDenyPolicy(
        control_home=layout.control_home,
        private_home=layout.private_home,
        env_file=env_file,
        provider_homes=provider_homes,
        frozen_control_bundle=layout.private_home / CONTROL_BUNDLE_DIRNAME,
    )


def build_providers(
    config: OrchestratorConfig,
    *,
    layout: RuntimeLayout,
    heartbeat_seconds: float = 30.0,
    agent_handle_recorder: AgentHandleRecorder | None = None,
) -> dict[ProviderId, AgentProvider]:
    """Construct the real provider adapters for the configured providers.

    Used by :func:`build_orchestrator` and the CLI ``preflight`` command. Providers own the private
    artifact tree, so they are rooted at ``layout.private_home``. ``agent_handle_recorder`` is set
    only by the ``watch`` daemon so a hard stop can reap a running agent's whole subtree; it is
    ``None`` for one-shot CLI runs and tests.
    """
    root = str(layout.private_home)
    artifact_level = config.logging.artifacts
    providers: dict[ProviderId, AgentProvider] = {}
    for pid, provider_cfg in config.agents.providers.items():
        if pid is ProviderId.CLAUDE:
            providers[pid] = claude.ClaudeCodeProvider(
                provider_cfg,
                security=config.security,
                artifacts_root=root,
                heartbeat_seconds=heartbeat_seconds,
                artifact_level=artifact_level,
                agent_handle_recorder=agent_handle_recorder,
            )
        elif pid is ProviderId.CODEX:
            providers[pid] = codex.CodexProvider(
                provider_cfg,
                security=config.security,
                artifacts_root=root,
                heartbeat_seconds=heartbeat_seconds,
                artifact_level=artifact_level,
                agent_handle_recorder=agent_handle_recorder,
            )
    return providers


def build_orchestrator(
    config: OrchestratorConfig,
    *,
    layout: RuntimeLayout,
    env_file: Path | None = None,
    gh_runner: Callable[..., Any] | None = None,
    heartbeat_seconds: float = 30.0,
    is_recovery_rerun: Callable[[str], bool] = lambda _id: False,
    agent_handle_recorder: AgentHandleRecorder | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> Orchestrator:
    """Wire the full dependency graph from a validated config (used by the CLI and e2e tests).

    Constructs the real provider adapters, Router, State Store (``<private_home>/state.db``), ledger
    (``<private_home>/logs/completed.jsonl``), Git Manager, Check Runner, loop controller, and
    validation gate. The one provider-neutral ``layout`` is injected into every consumer so each
    reads the surface it owns (private runtime state here; ``control_home`` for flows/tools inside
    the Orchestrator). The Core depends only on these interfaces — never on a provider directly.

    ``env_file`` is the CLI-resolved default/explicit ``.env`` path, threaded only into the internal
    deny policy (WRI-004 groundwork; unread until WRI-002/003 project it).

    ``is_recovery_rerun`` is threaded into the gate so the ``rerun`` command can admit exactly
    the re-run id past the duplicate-id check (scoped to one id; every other gate check still runs).

    ``agent_handle_recorder`` and ``is_cancelled`` are set only by the ``watch`` daemon: the
    recorder lets a hard stop reap a running agent's subtree, while ``is_cancelled`` both stops the
    flow at the next node boundary and tells the Router a raised provider error is a stop-kill (not
    a crash), so it never falls back to a fresh agent.
    """
    private_home = layout.private_home
    providers = build_providers(
        config,
        layout=layout,
        heartbeat_seconds=heartbeat_seconds,
        agent_handle_recorder=agent_handle_recorder,
    )

    store = StateStore.open(private_home / "state.db")
    ledger = Ledger(private_home / "logs")
    router = AgentRouter(config, providers, is_cancelled=is_cancelled)
    git = GitManager(
        config,
        store=store,
        artifacts_root=str(private_home),
        gh_runner=gh_runner,
        heartbeat_seconds=heartbeat_seconds,
    )
    checks = CheckRunner(config, heartbeat_seconds=heartbeat_seconds)
    # The resolver just normalizes the operator's ``checks.command_sets`` (no discovery).
    resolver = CheckResolver(config)
    gate = ValidationGate(
        config,
        store_has_task_id=store.task_id_exists,
        ledger_has_task_id=ledger.has_task_id,
        is_recovery_rerun=is_recovery_rerun,
        ledger_only_validation_rejects=ledger.only_validation_rejects,
    )
    notifier = build_notifier(config.telegram)
    return Orchestrator(
        config,
        router=router,
        git=git,
        checks=checks,
        store=store,
        ledger=ledger,
        gate=gate,
        layout=layout,
        deny_policy=build_internal_deny_policy(config, layout, env_file=env_file),
        notifier=notifier,
        resolver=resolver,
        heartbeat_seconds=heartbeat_seconds,
        isolation_checks=ISOLATION_CHECKS,
        is_cancelled=is_cancelled,
    )
