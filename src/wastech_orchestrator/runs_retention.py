"""Retention for the per-task ``runs/`` roots — the one place that deletes frozen run state.

``<private_home>/runs/`` accumulates one directory per task in each of its roots: the frozen control
bundle, the frozen instruction bundle, the sealed terminal exchanges, and (only when something went
wrong) the quarantined exchange evidence. Nothing about a finished task's own bundles is needed to
run the next one, so they are a **rerun/analysis cache**, not state — but they are also the only
surviving copy of what a finished task's agent saw, which is exactly what makes retention a policy
rather than a chore.

Two callers share this module: the Core evicts a successful task's own subtree at its terminal
transition, and the CLI's ``runs clean`` does the same on demand for an operator who turned that off
in order to analyze runs. One implementation, so the two can never disagree about what "clean"
removes.

Three boundaries are structural here rather than left to callers:

* :data:`RECLAIMABLE_ROOTS` excludes the quarantine root. Quarantine exists only when mutation
  detection caught an agent writing the read-only exchange — it is security evidence, and a
  retention path must not be the thing that erases it. Reaching it takes an explicit opt-in.
* Every path is rebuilt from the task id through the shared containment belt, so a task id that
  tries to escape ``runs/`` is refused before anything is unlinked.
* Nothing here opens a file. The roots are a provider read-deny target; a cleanup path that read
  or echoed their contents would turn deletion into a way to look inside them. Only directory
  names (task ids the operator already knows) leave this module.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from wastech_orchestrator.providers.artifacts import assert_contained_path
from wastech_orchestrator.runtime_layout import (
    CONTROL_BUNDLE_DIRNAME,
    EXCHANGE_QUARANTINE_DIRNAME,
    EXCHANGE_SEAL_DIRNAME,
    INSTRUCTION_BUNDLE_DIRNAME,
    runs_root,
)

#: The per-task roots ordinary retention may reclaim: a task's frozen inputs and its sealed
#: exchanges. Deliberately not the quarantine root — see the module docstring.
RECLAIMABLE_ROOTS: tuple[str, ...] = (
    CONTROL_BUNDLE_DIRNAME,
    INSTRUCTION_BUNDLE_DIRNAME,
    EXCHANGE_SEAL_DIRNAME,
)

#: The evidence root, reachable only through an explicit ``include_quarantine``.
QUARANTINE_ROOT: str = EXCHANGE_QUARANTINE_DIRNAME


def _roots(*, include_quarantine: bool) -> tuple[str, ...]:
    return (*RECLAIMABLE_ROOTS, QUARANTINE_ROOT) if include_quarantine else RECLAIMABLE_ROOTS


def task_run_dirs(
    private_home: str | Path, task_id: str, *, include_quarantine: bool = False
) -> tuple[Path, ...]:
    """The existing ``runs/<root>/<task_id>/`` directories for one task, in root order.

    Absent roots and absent task subdirectories are simply skipped, so a task that never reached a
    provider (or one already cleaned) yields an empty tuple rather than an error.

    Raises :class:`~wastech_orchestrator.providers.artifacts.PathIdentityError` if the built path
    does not resolve under ``runs/`` — a traversing task id, or a task directory replaced by a
    symlink pointing out of the tree. Both are refused before anything is deleted rather than
    followed.
    """
    parent = runs_root(private_home)
    found: list[Path] = []
    for root in _roots(include_quarantine=include_quarantine):
        candidate = assert_contained_path(parent, parent / root / task_id)
        if candidate.exists():
            found.append(candidate)
    return tuple(found)


def remove_task_runs(
    private_home: str | Path, task_id: str, *, include_quarantine: bool = False
) -> tuple[str, ...]:
    """Remove one task's subtree from each reclaimable ``runs/`` root; return the POSIX paths gone.

    Best-effort per directory: a locked or already-removed tree is skipped rather than raised, so a
    terminal transition is never turned into a failure by a cleanup that could not finish. The
    return value reports what actually went, which is what the caller logs.
    """
    removed: list[str] = []
    for path in task_run_dirs(private_home, task_id, include_quarantine=include_quarantine):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed.append(path.as_posix())
    return tuple(removed)


def run_task_ids(private_home: str | Path, *, include_quarantine: bool = False) -> tuple[str, ...]:
    """Task ids that own state under ``runs/``, most recently touched first.

    A task's position is its newest directory mtime across the roots in scope, so ``--keep N``
    retains the N most recent *tasks* rather than N directories of whichever root sorts first.
    """
    parent = runs_root(private_home)
    newest: dict[str, float] = {}
    for root in _roots(include_quarantine=include_quarantine):
        root_dir = parent / root
        if not root_dir.is_dir():
            continue
        for child in root_dir.iterdir():
            if not child.is_dir():
                continue
            mtime = child.stat().st_mtime
            newest[child.name] = max(newest.get(child.name, mtime), mtime)
    return tuple(sorted(newest, key=lambda task_id: (-newest[task_id], task_id)))
