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
from wastech_orchestrator.runtime_layout import InternalDenyPolicy, RuntimeLayout
from wastech_orchestrator.security.isolation import HostFloorCheck, IsolationCheck
from wastech_orchestrator.security.shell_reach import ShellCheck
from wastech_orchestrator.state_store import StateStore
from wastech_orchestrator.task.validation_gate import ValidationGate

# ProviderId → offline isolation check, bound here (the composition root) so ``core`` and the
# CLI preflight can run the ``strict_isolation`` gate without importing a concrete adapter.
ISOLATION_CHECKS: dict[ProviderId, IsolationCheck] = {
    ProviderId.CLAUDE: claude.isolation_reasons,
    ProviderId.CODEX: codex.isolation_reasons,
}

# ProviderId → offline "what can this host not enforce?" answer, bound here for the same reason.
# Only Claude is listed: its floor rides an OS sandbox that can be classified offline, while Codex
# runs its own backend and answers the same question per attempt, from inside it, with a canary.
# An absent entry is the honest "no answer here" — binding a hardcoded "the floor exists" would be a
# claim about a host nothing verified.
HOST_FLOOR_CHECKS: dict[ProviderId, HostFloorCheck] = {
    ProviderId.CLAUDE: claude.host_floor_gap,
    # Codex answers a different shape of the same question — its sandbox availability is decided by
    # its own CLI, not by a platform plus two executables — but the requirement is about the HOST,
    # so an entry that says "not classifiable offline here" beats no entry at all: without one a
    # Codex-only park on native Windows printed no floor line and got no preamble paragraph.
    ProviderId.CODEX: codex.host_floor_gap,
}

# ProviderId → offline "does this attempt get a shell?" check, bound here for the same reason: the
# core's per-attempt detection bracket keys on command execution, and only the adapter knows whether
# its resolved tool set keeps one on this host.
SHELL_CHECKS: dict[ProviderId, ShellCheck] = {
    ProviderId.CLAUDE: claude.attempt_has_shell,
    ProviderId.CODEX: codex.attempt_has_shell,
}


def build_internal_deny_policy(
    layout: RuntimeLayout,
    *,
    env_file: Path | None = None,
) -> InternalDenyPolicy:
    """Assemble the internal deny policy at the composition boundary.

    Collects the control/private homes from the provider-neutral ``layout``, the resolved
    default/explicit ``env_file`` (which may live outside ``private_home``), and the per-task
    runtime root (``layout.runs_home``) that parents every frozen bundle, seal, and quarantined
    tree. The provider CLIs' own config homes are deliberately not collected — the deny they would
    carry is per-file, not per-directory, and a whole-home deny breaks a CLI whose own binary or
    helpers live inside it (see :class:`InternalDenyPolicy`).

    These are representations only; the provider adapters project them into their own policy.
    """
    return InternalDenyPolicy(
        control_home=layout.control_home,
        private_home=layout.private_home,
        env_file=env_file,
        runs_home=layout.runs_home,
    )


def build_providers(
    config: OrchestratorConfig,
    *,
    layout: RuntimeLayout,
    heartbeat_seconds: float = 30.0,
    agent_handle_recorder: AgentHandleRecorder | None = None,
    deny_policy: InternalDenyPolicy | None = None,
) -> dict[ProviderId, AgentProvider]:
    """Construct the real provider adapters for the configured providers.

    Used by :func:`build_orchestrator` and the CLI ``preflight`` command. Providers own the private
    artifact tree, so they are rooted at ``layout.private_home``. ``agent_handle_recorder`` is set
    only by the ``watch`` daemon so a hard stop can reap a running agent's whole subtree; it is
    ``None`` for one-shot CLI runs and tests. ``deny_policy`` is the internal
    read-deny
    set each adapter projects into its tool/OS-sandbox policy; when ``None`` it is built from the
    ``layout`` (the CLI ``preflight`` caller passes nothing — it never launches an agent).
    """
    root = str(layout.private_home)
    artifact_level = config.logging.artifacts
    policy = deny_policy if deny_policy is not None else build_internal_deny_policy(layout)
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
                deny_policy=policy,
            )
        elif pid is ProviderId.CODEX:
            providers[pid] = codex.CodexProvider(
                provider_cfg,
                security=config.security,
                artifacts_root=root,
                heartbeat_seconds=heartbeat_seconds,
                artifact_level=artifact_level,
                agent_handle_recorder=agent_handle_recorder,
                deny_policy=policy,
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
    deny policy (the adapters project it into provider syntax).

    ``is_recovery_rerun`` is threaded into the gate so the ``rerun`` command can admit exactly
    the re-run id past the duplicate-id check (scoped to one id; every other gate check still runs).

    ``agent_handle_recorder`` and ``is_cancelled`` are set only by the ``watch`` daemon: the
    recorder lets a hard stop reap a running agent's subtree, while ``is_cancelled`` both stops the
    flow at the next node boundary and tells the Router a raised provider error is a stop-kill (not
    a crash), so it never falls back to a fresh agent.
    """
    private_home = layout.private_home
    # Build the internal deny policy once (with the resolved ``env_file``) and thread
    # it
    # into the providers (read/write-deny projection), the Orchestrator (audit), and — as the
    # offline
    # isolation-check table — the Router's ``CAPABILITY_UNAVAILABLE`` fallback-eligibility gate.
    deny_policy = build_internal_deny_policy(layout, env_file=env_file)
    providers = build_providers(
        config,
        layout=layout,
        heartbeat_seconds=heartbeat_seconds,
        agent_handle_recorder=agent_handle_recorder,
        deny_policy=deny_policy,
    )

    store = StateStore.open(private_home / "state.db")
    ledger = Ledger(private_home / "logs")
    router = AgentRouter(
        config,
        providers,
        is_cancelled=is_cancelled,
        isolation_checks=ISOLATION_CHECKS,
        shell_checks=SHELL_CHECKS,
    )
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
        deny_policy=deny_policy,
        notifier=notifier,
        resolver=resolver,
        heartbeat_seconds=heartbeat_seconds,
        isolation_checks=ISOLATION_CHECKS,
        host_floor_checks=HOST_FLOOR_CHECKS,
        is_cancelled=is_cancelled,
    )
