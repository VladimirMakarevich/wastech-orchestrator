"""Automatic run-artifact eviction at a terminal transition: the decision table.

The Core drops a *successful* task's own per-task ``runs/`` subtree so the ordinary operator never
has to learn those directories exist. Everything that makes it safe is a condition, so each one is
pinned here: the status, the config switch, and the exchange guard. The decision reads only config
and the store's exchange guard, so it is exercised through the real method with the collaborators it
actually touches — no git repo, no provider, no flow.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.orchestrator import RERUN_ELIGIBLE_STATUSES, Orchestrator
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.runtime_layout import runs_root

_ROOTS = ("control-bundles", "instruction-bundles", "exchange-seals")


class _GuardStore:
    """The one store call the eviction reads: the task's exchange guard flags."""

    def __init__(self, guard: tuple[int, int] | None = (0, 0)) -> None:
        self._guard = guard

    def get_exchange_guard(self, task_id: str) -> tuple[int, int]:
        if self._guard is None:
            raise KeyError(task_id)
        return self._guard


class _Evictor:
    """A stand-in carrying exactly the attributes ``_evict_run_artifacts`` touches."""

    _evict_run_artifacts = Orchestrator._evict_run_artifacts
    _log = Orchestrator._log

    def __init__(self, config: OrchestratorConfig, private_home: Path, store: _GuardStore) -> None:
        self._config = config
        self._artifacts_root = private_home
        self._store = store


def _config(*, clean: bool = True) -> OrchestratorConfig:
    text = (
        'repo:\n  url: "git@example.com:o/r.git"\n'
        "agents:\n  allowed: [codex]\n  providers:\n    codex:\n      command: codex\n"
        f"logging:\n  clean_runs_on_success: {'true' if clean else 'false'}\n"
    )
    return loads_config(text).config


def _seed(private_home: Path, task_id: str, *, quarantine: bool = False) -> Path:
    parent = runs_root(private_home)
    roots = (*_ROOTS, "exchange-quarantine") if quarantine else _ROOTS
    for root in roots:
        (parent / root / task_id).mkdir(parents=True)
    return parent


def _evict(
    tmp_path: Path,
    *,
    final: Status,
    clean: bool = True,
    guard: tuple[int, int] | None = (0, 0),
    quarantine: bool = False,
) -> Path:
    private_home = tmp_path / ".worc"
    parent = _seed(private_home, "t1", quarantine=quarantine)
    evictor = _Evictor(_config(clean=clean), private_home, _GuardStore(guard))
    evictor._evict_run_artifacts("t1", final=final)
    return parent


def test_successful_terminal_evicts_the_reclaimable_roots(tmp_path: Path) -> None:
    parent = _evict(tmp_path, final=Status.DONE, quarantine=True)
    for root in _ROOTS:
        assert not (parent / root / "t1").exists()
    # Quarantine is never auto-deleted: it exists only when an agent wrote the read-only exchange.
    assert (parent / "exchange-quarantine" / "t1").is_dir()


@pytest.mark.parametrize(
    "final", [Status.FAILED, Status.MANUAL_ACTION_REQUIRED, Status.RUNNING, Status.PREPARING]
)
def test_every_non_success_terminal_survives_untouched(tmp_path: Path, final: Status) -> None:
    # Deleting these at a failure would remove the evidence at the exact moment it is needed.
    parent = _evict(tmp_path, final=final)
    for root in _ROOTS:
        assert (parent / root / "t1").is_dir()


def test_switch_off_keeps_everything(tmp_path: Path) -> None:
    parent = _evict(tmp_path, final=Status.DONE, clean=False)
    for root in _ROOTS:
        assert (parent / root / "t1").is_dir()


@pytest.mark.parametrize("guard", [(1, 0), (0, 1)])
def test_an_unclean_exchange_terminal_keeps_everything(
    tmp_path: Path, guard: tuple[int, int]
) -> None:
    # Contaminated or unsafe means the seal / teardown did not complete: the seal may be the only
    # verified copy of a tree still sitting in the repo, and later launches are already blocked.
    parent = _evict(tmp_path, final=Status.DONE, guard=guard)
    for root in _ROOTS:
        assert (parent / root / "t1").is_dir()


def test_unknown_task_is_a_no_op(tmp_path: Path) -> None:
    parent = _evict(tmp_path, final=Status.DONE, guard=None)
    for root in _ROOTS:
        assert (parent / root / "t1").is_dir()


def test_rerun_never_accepts_a_done_task(tmp_path: Path) -> None:
    """The precondition that makes success-only eviction safe, pinned where it is relied on.

    A ``done`` task cannot be a rerun target, so nothing will ever ask to restore from the seal we
    just deleted. If ``rerun`` is ever widened to accept ``done``, this fails here instead of
    silently losing restore data at the next successful run.
    """
    assert Status.DONE not in RERUN_ELIGIBLE_STATUSES
    assert sorted(s.value for s in RERUN_ELIGIBLE_STATUSES) == [
        "failed",
        "manual_action_required",
        "running",
    ]


def test_config_default_is_cleanup_on(tmp_path: Path) -> None:
    # An absent `logging` block must still clean up: the ordinary operator never configures this.
    bare = loads_config(
        'repo:\n  url: "git@example.com:o/r.git"\n'
        "agents:\n  allowed: [codex]\n  providers:\n    codex:\n      command: codex\n"
    ).config
    assert bare.logging.clean_runs_on_success is True
    assert replace(bare.logging, clean_runs_on_success=False).clean_runs_on_success is False
