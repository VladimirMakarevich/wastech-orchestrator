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
from wastech_orchestrator.security.isolation import IsolationCheck
from wastech_orchestrator.state_store import StateStore
from wastech_orchestrator.task.validation_gate import ValidationGate

# ProviderId → offline isolation check, bound here (the composition root) so ``core`` and the
# CLI preflight can run the ``strict_isolation`` gate without importing a concrete adapter.
ISOLATION_CHECKS: dict[ProviderId, IsolationCheck] = {
    ProviderId.CLAUDE: claude.isolation_reasons,
    ProviderId.CODEX: codex.isolation_reasons,
}


def build_providers(
    config: OrchestratorConfig,
    *,
    artifacts_root: str | Path,
    heartbeat_seconds: float = 30.0,
    agent_handle_recorder: AgentHandleRecorder | None = None,
) -> dict[ProviderId, AgentProvider]:
    """Construct the real provider adapters for the configured providers.

    Used by :func:`build_orchestrator` and the CLI ``preflight`` command. ``agent_handle_recorder``
    is set only by the ``watch`` daemon so a hard stop can reap a running agent's whole subtree; it
    is ``None`` for one-shot CLI runs and tests.
    """
    root = str(Path(artifacts_root))
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
    artifacts_root: str | Path,
    gh_runner: Callable[..., Any] | None = None,
    heartbeat_seconds: float = 30.0,
    is_recovery_rerun: Callable[[str], bool] = lambda _id: False,
    agent_handle_recorder: AgentHandleRecorder | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> Orchestrator:
    """Wire the full dependency graph from a validated config (used by the CLI and e2e tests).

    Constructs the real provider adapters, Router, State Store (``<artifacts_root>/state.db``),
    ledger (``<artifacts_root>/logs/completed.jsonl``), Git Manager, Check Runner, loop controller,
    and validation gate. The Core depends only on these interfaces — never on a provider directly.

    ``is_recovery_rerun`` is threaded into the gate so the ``rerun`` command can admit exactly
    the re-run id past the duplicate-id check (scoped to one id; every other gate check still runs).

    ``agent_handle_recorder`` and ``is_cancelled`` are set only by the ``watch`` daemon: the
    recorder lets a hard stop reap a running agent's subtree, while ``is_cancelled`` both stops the
    flow at the next node boundary and tells the Router a raised provider error is a stop-kill (not
    a crash), so it never falls back to a fresh agent.
    """
    root = Path(artifacts_root)
    providers = build_providers(
        config,
        artifacts_root=root,
        heartbeat_seconds=heartbeat_seconds,
        agent_handle_recorder=agent_handle_recorder,
    )

    store = StateStore.open(root / "state.db")
    ledger = Ledger(root / "logs")
    router = AgentRouter(config, providers, is_cancelled=is_cancelled)
    git = GitManager(
        config,
        store=store,
        artifacts_root=str(root),
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
        artifacts_root=str(root),
        notifier=notifier,
        resolver=resolver,
        heartbeat_seconds=heartbeat_seconds,
        isolation_checks=ISOLATION_CHECKS,
        is_cancelled=is_cancelled,
    )
