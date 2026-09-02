"""Wiring proof: the one :class:`RuntimeLayout` reaches every consumer's correct surface.

The layout here uses **distinct** control/private/exchange directories (not the coincident
``.worc`` default) so each assertion proves the consumer read the field it owns, not that the paths
merely happen to be equal today.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator import cli, composition
from wastech_orchestrator.composition import (
    build_internal_deny_policy,
    build_orchestrator,
    build_providers,
)
from wastech_orchestrator.core.flow.exchange_seal import (
    exchange_quarantine_root,
    exchange_seal_root,
)
from wastech_orchestrator.core.flow.instruction_bundle import instruction_bundle_dir
from wastech_orchestrator.notify import NullNotifier
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.runtime_layout import RuntimeLayout


def _distinct_layout(repo_root: str, tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout(
        repo_root=Path(repo_root),
        control_home=tmp_path / "ctrl",
        private_home=tmp_path / "priv",
        exchange_root=tmp_path / "xchg",
    )


def test_providers_are_rooted_at_private_home(git_repo, make_git_config, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    providers = build_providers(config, layout=layout)
    assert providers, "expected at least one configured provider"
    for provider in providers.values():
        assert provider._artifacts_root == tmp_path / "priv"


def test_providers_receive_the_internal_deny_policy(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # Wiring guard: build_providers must project the internal deny policy into EVERY
    # provider
    # so a wiring bug can never silently disable the read/write-deny projection. The provider's own
    # ``_build_argv`` reads ``self._deny_policy`` — an absent one would emit no internal denies.
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    providers = build_providers(config, layout=layout)
    assert providers, "expected at least one configured provider"
    for provider in providers.values():
        assert provider._deny_policy is not None
        assert provider._deny_policy.control_home == tmp_path / "ctrl"
        assert provider._deny_policy.private_home == tmp_path / "priv"


def test_providers_receive_read_isolation_off_flag(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    # Wiring guard: the operator's security config (incl. disable_read_isolation) flows into
    # EVERY provider, so each adapter's _build_argv reads the effective read_isolation_off (the
    # formula lives once on SecurityConfig; the adapter never recomputes it).
    base = make_git_config(git_repo.clone, checks=["pytest"])
    config = replace(base, security=replace(base.security, disable_read_isolation=True))
    layout = _distinct_layout(git_repo.clone, tmp_path)
    providers = build_providers(config, layout=layout)
    assert providers, "expected at least one configured provider"
    for provider in providers.values():
        assert provider._security.read_isolation_off is True


def test_router_receives_isolation_checks(git_repo, make_git_config, tmp_path: Path) -> None:
    # Wiring guard: the router's CAPABILITY_UNAVAILABLE fallback-eligibility gate needs the offline
    # isolation-check table; build_orchestrator must inject it (else _can_isolate fails closed for
    # every provider and a legitimate cross-provider recovery would be wrongly refused).
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    orch = build_orchestrator(config, layout=layout)
    assert orch._router._isolation_checks  # non-empty
    for pid in config.agents.providers:
        assert pid in orch._router._isolation_checks


def test_orchestrator_receives_host_floor_checks(git_repo, make_git_config, tmp_path: Path) -> None:
    # Wiring guard for the advisory twin: without the injected table the run says nothing at all
    # about a host that cannot enforce the write floor, which is the exact silence this verdict
    # exists to end. BOTH providers answer: Claude classifies its host offline, Codex says
    # that on native Windows its own CLI is the only thing that can — without both answers a
    # Codex-only fleet on that host gets no line and no preamble paragraph at all.
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    orch = build_orchestrator(config, layout=layout)
    assert set(orch._host_floor_checks) == {ProviderId.CLAUDE, ProviderId.CODEX}


def test_orchestrator_consumers_receive_the_right_field(
    git_repo, make_git_config, tmp_path: Path
) -> None:
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)
    env_file = Path("/etc/secrets/prod.env")
    orch = build_orchestrator(config, layout=layout, env_file=env_file)

    # Private surface: artifacts root.
    assert orch._layout is layout
    assert orch._artifacts_root == tmp_path / "priv"
    # Exchange surface (named by the layout, resolved by its consumers).
    assert orch._exchange_root == tmp_path / "xchg"
    # Control surface: flows/tools live under control_home, never private_home.
    assert orch._flow_registry._operator_dir == tmp_path / "ctrl" / "flows"
    assert orch._tool_registry._dir == tmp_path / "ctrl" / "tools"
    # Internal deny policy threaded through with the resolved env-file.
    assert orch._deny_policy is not None
    assert orch._deny_policy.control_home == tmp_path / "ctrl"
    assert orch._deny_policy.private_home == tmp_path / "priv"
    assert env_file in orch._deny_policy.denied_paths


def test_deny_policy_carries_no_provider_home(git_repo, tmp_path: Path, monkeypatch) -> None:
    # The assembly never feeds provider config homes into the deny set — while the orchestrator's
    # own private four stay in it.
    claude_home = tmp_path / "homes" / ".claude"
    codex_home = tmp_path / "homes" / ".codex"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    layout = _distinct_layout(git_repo.clone, tmp_path)
    env_file = tmp_path / "envs" / "prod.env"
    policy = build_internal_deny_policy(layout, env_file=env_file)
    denied = policy.denied_paths
    assert claude_home.resolve() not in denied
    assert codex_home.resolve() not in denied
    for private in (layout.control_home, layout.private_home, layout.runs_home, env_file):
        assert private in denied, private


def test_deny_policy_names_the_per_task_runs_root(git_repo, tmp_path: Path) -> None:
    # The per-task runtime root under private_home is a *named* deny target so the provider
    # projection denies it by name, not by coincidence of location — and it survives a later
    # relocation of private_home. Asserting the entry itself (not merely that it sits under
    # private_home) is what makes this fail if the entry is ever dropped.
    layout = _distinct_layout(git_repo.clone, tmp_path)
    policy = build_internal_deny_policy(layout, env_file=None)
    runs_home = layout.private_home / "runs"
    assert policy.runs_home == runs_home
    assert runs_home in policy.denied_paths
    # One named entry covers all four per-task roots because each resolver puts its root *under*
    # it — a resolver that drifted back to a private_home sibling would escape the named deny.
    resolved = (
        exchange_seal_root(layout.private_home, "t1"),
        exchange_quarantine_root(layout.private_home, "t1"),
        instruction_bundle_dir(layout.private_home, "t1"),
    )
    for path in resolved:
        assert path.is_relative_to(runs_home), path.as_posix()


def test_cli_layout_for_reproduces_default_paths(git_repo, make_git_config) -> None:
    # Path-for-path: the CLI composition boundary resolves the same home.
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = cli.layout_for(config)
    assert layout.control_home == Path(config.repo.local_path) / ".worc"
    assert layout.private_home == Path(config.repo.local_path) / ".worc"
    assert layout.exchange_root == Path(config.repo.local_path) / ".worc-io"


def test_the_notifier_gets_the_same_stop_predicate_as_the_router(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, tmp_path: Path
) -> None:
    # Anti-drift, and the reason this test exists rather than a docstring: the daemon's stop
    # predicate reaches the flow engine and the router, and for a long time reached nothing else —
    # so a blocking Telegram ask made from inside a tick (the claim gate's, up to
    # `telegram.ask_timeout_s`) could not be stopped, only killed. One predicate, every waiter.
    recorded: dict[str, object] = {}

    def spy(cfg: object, env: object = None, **kwargs: object) -> NullNotifier:
        recorded.update(kwargs)
        return NullNotifier()

    monkeypatch.setattr(composition, "build_notifier", spy)
    config = make_git_config(git_repo.clone, checks=["pytest"])
    layout = _distinct_layout(git_repo.clone, tmp_path)

    def stopping() -> bool:
        return True

    orch = build_orchestrator(config, layout=layout, is_cancelled=stopping)

    assert recorded.get("is_cancelled") is stopping
    assert orch._router._is_cancelled is stopping  # the same object, not merely an equal default
