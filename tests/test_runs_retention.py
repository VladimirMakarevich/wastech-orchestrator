"""Retention for the per-task ``runs/`` roots: what it reclaims, what it must never touch.

The module is pure filesystem policy, so it is exercised directly against a real temp private home:
the three reclaimable roots, the quarantine root that needs an explicit opt-in, the ``--keep``
ordering, containment, and the invariant that a cleanup path never becomes a way to read what it
deletes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wastech_orchestrator import runs_retention
from wastech_orchestrator.providers.artifacts import PathIdentityError
from wastech_orchestrator.runtime_layout import runs_root

_ROOTS = ("control-bundles", "instruction-bundles", "exchange-seals")


def _seed(private_home: Path, task_id: str, *, quarantine: bool = False) -> None:
    """Write one task's per-task state into every ``runs/`` root (quarantine only when asked)."""
    roots = (*_ROOTS, "exchange-quarantine") if quarantine else _ROOTS
    for root in roots:
        task_dir = runs_root(private_home) / root / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "manifest.json").write_text('{"task_id":"x"}\n', encoding="utf-8")


def test_removes_the_reclaimable_roots_and_keeps_quarantine(tmp_path: Path) -> None:
    private_home = tmp_path / ".worc"
    _seed(private_home, "t1", quarantine=True)
    removed = runs_retention.remove_task_runs(private_home, "t1")
    assert len(removed) == len(_ROOTS)
    parent = runs_root(private_home)
    for root in _ROOTS:
        assert not (parent / root / "t1").exists()
    # Quarantine is written only when mutation detection caught an agent writing the read-only
    # exchange. It is security evidence, so routine retention must never be what erases it.
    assert (parent / "exchange-quarantine" / "t1").is_dir()


def test_include_quarantine_is_an_explicit_opt_in(tmp_path: Path) -> None:
    private_home = tmp_path / ".worc"
    _seed(private_home, "t1", quarantine=True)
    removed = runs_retention.remove_task_runs(private_home, "t1", include_quarantine=True)
    assert len(removed) == len(_ROOTS) + 1
    assert not (runs_root(private_home) / "exchange-quarantine" / "t1").exists()


def test_only_the_named_task_is_touched(tmp_path: Path) -> None:
    private_home = tmp_path / ".worc"
    _seed(private_home, "t1")
    _seed(private_home, "t2")
    runs_retention.remove_task_runs(private_home, "t1")
    for root in _ROOTS:
        assert not (runs_root(private_home) / root / "t1").exists()
        assert (runs_root(private_home) / root / "t2").is_dir()


def test_absent_state_is_a_clean_no_op(tmp_path: Path) -> None:
    private_home = tmp_path / ".worc"  # runs/ never created — a task that never reached a provider
    assert runs_retention.remove_task_runs(private_home, "t1") == ()
    assert runs_retention.task_run_dirs(private_home, "t1") == ()
    assert runs_retention.run_task_ids(private_home) == ()


def test_run_task_ids_are_newest_first(tmp_path: Path) -> None:
    private_home = tmp_path / ".worc"
    for i, task_id in enumerate(("old", "middle", "new")):
        _seed(private_home, task_id)
        for root in _ROOTS:
            stamp = 1_700_000_000 + i * 100
            os.utime(runs_root(private_home) / root / task_id, (stamp, stamp))
    # Ordered by task, not by whichever root sorts first, so --keep N retains N whole tasks.
    assert runs_retention.run_task_ids(private_home) == ("new", "middle", "old")


def test_run_task_ids_ignores_quarantine_only_tasks_unless_included(tmp_path: Path) -> None:
    private_home = tmp_path / ".worc"
    quarantined = runs_root(private_home) / "exchange-quarantine" / "tainted"
    quarantined.mkdir(parents=True)
    # Listing it under the default scope would report a task whose state cleanup cannot remove.
    assert runs_retention.run_task_ids(private_home) == ()
    assert runs_retention.run_task_ids(private_home, include_quarantine=True) == ("tainted",)


def test_a_traversing_task_id_is_refused_before_anything_is_unlinked(tmp_path: Path) -> None:
    private_home = tmp_path / ".worc"
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(PathIdentityError):
        runs_retention.remove_task_runs(private_home, "../../secrets")
    assert (outside / "keep.txt").is_file()


def test_a_task_dir_symlinked_out_of_the_tree_is_refused_not_followed(tmp_path: Path) -> None:
    # Recursing through such a symlink would delete whatever it points at. The containment belt
    # resolves the path, so it never reaches the remover.
    private_home = tmp_path / ".worc"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    seals = runs_root(private_home) / "exchange-seals"
    seals.mkdir(parents=True)
    try:
        (seals / "t1").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # unprivileged Windows cannot create symlinks
        pytest.skip("symlink creation not permitted on this host")
    with pytest.raises(PathIdentityError):
        runs_retention.remove_task_runs(private_home, "t1")
    assert (outside / "keep.txt").is_file()


def test_quarantine_is_excluded_from_the_reclaimable_set_structurally() -> None:
    # A boundary the callers must not be able to forget: the opt-in flag is the only way in.
    assert runs_retention.QUARANTINE_ROOT not in runs_retention.RECLAIMABLE_ROOTS


def test_cleanup_reports_only_directory_paths_never_contents(tmp_path: Path) -> None:
    # The roots are a provider read-deny target. A cleanup path that echoed what it deleted would
    # turn deletion into a channel for reading them, so only the directory paths ever come back.
    private_home = tmp_path / ".worc"
    _seed(private_home, "t1")
    secret = runs_root(private_home) / "instruction-bundles" / "t1" / "task" / "task.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("SUPER-SECRET-PACKET-BODY\n", encoding="utf-8")
    removed = runs_retention.remove_task_runs(private_home, "t1")
    assert removed
    for reported in removed:
        assert "SUPER-SECRET-PACKET-BODY" not in reported
        assert reported.endswith("/t1")  # POSIX form, the task dir itself, nothing from inside it
