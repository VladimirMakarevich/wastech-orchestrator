"""The startup credential gate: an allowed provider that cannot log in must not start a run.

The incident this exists for had a fallback provider whose credentials had expired eight hours
earlier, discovered only when it was the last hope. ``run_preflight`` would have reported it, but
nothing in ``watch``/``run``/``rerun`` ever called it, so the gate is separate and narrow: it probes
credentials and nothing else, and it refuses rather than warning, because a daemon has no operator
to read a warning.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator import cli, preflight
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    AuthProbe,
    AuthState,
    ProviderHealth,
    ProviderId,
)
from wastech_orchestrator.state_store import StateStore, TaskRow

pytestmark = pytest.mark.slow

_LOGGED_IN = AuthProbe(state=AuthState.LOGGED_IN, method="fake-method", detail="stored credentials")
_LOGGED_OUT = AuthProbe(
    state=AuthState.LOGGED_OUT, method=None, detail="not logged in (run 'codex login')"
)
_UNKNOWN = AuthProbe(state=AuthState.UNKNOWN, method=None, detail="no recognizable answer")


class _FakeAuthProvider:
    """A provider whose only interesting answer is what its CLI says about credentials."""

    def __init__(self, provider_id: str, auth: AuthProbe | None) -> None:
        self.id = provider_id
        self._auth = auth

    def preflight(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.id,
            executable_found=True,
            version="1.2.3",
            supports_required_features=True,
            message="available",
            auth=self._auth,
        )

    def run(self, request: AgentRunRequest) -> object:  # pragma: no cover - never called
        raise NotImplementedError


def _patch_auth(
    monkeypatch: pytest.MonkeyPatch,
    *,
    claude: AuthProbe | None = _LOGGED_IN,
    codex: AuthProbe | None = _LOGGED_IN,
    omit: ProviderId | None = None,
) -> None:
    providers = {
        ProviderId.CLAUDE: _FakeAuthProvider("claude", claude),
        ProviderId.CODEX: _FakeAuthProvider("codex", codex),
    }
    if omit is not None:
        del providers[omit]
    monkeypatch.setattr(cli, "build_providers", lambda _c, *, layout: providers)


# --- the gate itself ------------------------------------------------------------------------------


def test_gate_refuses_a_logged_out_fallback(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config
) -> None:
    # The incident's own shape: the primary is fine and the fallback is dead. "A fallback will
    # cover" is precisely the assumption that failed, so this is fatal rather than advisory.
    _patch_auth(monkeypatch, codex=_LOGGED_OUT)
    with pytest.raises(preflight.ProviderNotLoggedInError) as exc:
        cli.require_provider_auth(make_git_config(git_repo.clone))
    assert "codex" in str(exc.value)
    assert "agents.allowed" in str(exc.value)


def test_gate_refuses_a_logged_out_primary(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config
) -> None:
    # Role-independent by design: the same refusal for the provider that runs every node.
    _patch_auth(monkeypatch, claude=_LOGGED_OUT)
    with pytest.raises(preflight.ProviderNotLoggedInError):
        cli.require_provider_auth(make_git_config(git_repo.clone))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"codex": _UNKNOWN},  # a probe that could not answer is not evidence of a missing login
        {"codex": None},  # an adapter with no credential verb makes no claim
        {"omit": ProviderId.CODEX},  # no adapter configured for an allowed provider
    ],
)
def test_gate_only_blocks_on_an_explicit_logged_out_answer(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config, kwargs: dict
) -> None:
    _patch_auth(monkeypatch, **kwargs)
    cli.require_provider_auth(make_git_config(git_repo.clone))  # does not raise


def test_gate_ignores_a_logged_out_provider_outside_agents_allowed(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config
) -> None:
    # Only a provider a node can actually route to matters, so removing it from the allowlist is the
    # second lever the refusal message names — and it has to actually work.
    base = make_git_config(git_repo.clone)
    claude_only = replace(
        base,
        agents=replace(
            base.agents,
            allowed=(ProviderId.CLAUDE,),
            providers={ProviderId.CLAUDE: base.agents.providers[ProviderId.CLAUDE]},
        ),
    )
    _patch_auth(monkeypatch, codex=_LOGGED_OUT)
    cli.require_provider_auth(claude_only)  # does not raise


# --- every entry point that starts work ----------------------------------------------------------


def _in_repo_config(git_repo, make_git_config) -> OrchestratorConfig:
    return make_git_config(git_repo.clone)


@pytest.mark.parametrize("poll", ["0", "5"], ids=["single-pass", "daemon"])
def test_watch_refuses_to_start_when_a_provider_is_logged_out(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    capsys: pytest.CaptureFixture[str],
    poll: str,
) -> None:
    # Both watch modes are covered by one gate call placed above the poll split — a daemon has no
    # operator to read a warning, and a single pass gains nothing by failing at the first fallback.
    config = _in_repo_config(git_repo, make_git_config)
    monkeypatch.setattr(cli, "load_config_for", lambda _args: config)
    _patch_auth(monkeypatch, codex=_LOGGED_OUT)
    started: list[int] = []
    monkeypatch.setattr(cli, "build_orchestrator", lambda *a, **k: started.append(1))
    monkeypatch.setattr(cli, "watch_loop", lambda *a, **k: started.append(1))

    rc = cli.main(["watch", "--poll-seconds", poll])

    assert rc == 2  # a clean refusal, not a traceback
    assert "not logged in" in capsys.readouterr().out
    assert started == []  # nothing was built and no tick ever ran


def test_run_refuses_to_start_and_touches_no_state(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _in_repo_config(git_repo, make_git_config)
    monkeypatch.setattr(cli, "load_config_for", lambda _args: config)
    _patch_auth(monkeypatch, codex=_LOGGED_OUT)
    started: list[int] = []
    monkeypatch.setattr(cli, "build_orchestrator", lambda *a, **k: started.append(1))
    task = tmp_path / "t.md"
    task.write_text("---\nid: t\ntitle: t\n---\n", encoding="utf-8")

    rc = cli.main(["run", str(task)])

    assert rc == 2
    assert "not logged in" in capsys.readouterr().out
    assert started == []
    # The gate sits ahead of the orchestrator, so no database is created for a run that never began.
    assert not (Path(cli.worc_home_for(config)) / "state.db").exists()


def test_rerun_refuses_to_start_when_a_provider_is_logged_out(
    monkeypatch: pytest.MonkeyPatch,
    git_repo,
    make_git_config,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # rerun is this incident's own recovery path: it re-spends real money from a checkpoint, so it
    # must not start against credentials that cannot authenticate either. The gate sits where
    # require_git_control does — after the plan is accepted, before any work begins.
    config = _in_repo_config(git_repo, make_git_config)
    # Kept outside the clone so seeding a rerunnable task does not itself dirty the working tree.
    source = tmp_path / "task-1.md"
    source.write_text("---\nid: task-1\ntitle: T\n---\n", encoding="utf-8")
    db = Path(cli.worc_home_for(config)) / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore.open(db)
    store.insert_task(TaskRow("task-1", "T", Status.FAILED, source_path=str(source)))
    store.close()
    monkeypatch.setattr(cli, "load_config_for", lambda _args: config)
    _patch_auth(monkeypatch, codex=_LOGGED_OUT)
    resumed: list[int] = []
    monkeypatch.setattr(
        "wastech_orchestrator.core.orchestrator.Orchestrator.resume",
        lambda self, **k: resumed.append(1),
    )

    rc = cli.main(["rerun", "task-1", "--yes", "--non-interactive"])

    assert rc == 2
    assert "not logged in" in capsys.readouterr().out
    assert resumed == []  # the refusal came before anything was re-driven


def test_entry_points_start_when_the_probe_cannot_answer(
    monkeypatch: pytest.MonkeyPatch, git_repo, make_git_config
) -> None:
    # A flaky or drifted probe must never stop a run; only an explicit logged-out answer does.
    config = _in_repo_config(git_repo, make_git_config)
    monkeypatch.setattr(cli, "load_config_for", lambda _args: config)
    _patch_auth(monkeypatch, codex=_UNKNOWN)
    monkeypatch.setattr(cli, "build_orchestrator", lambda *a, **k: object())
    monkeypatch.setattr(cli, "watch_loop", lambda *a, **k: [])

    assert cli.main(["watch", "--poll-seconds", "0"]) == 0
