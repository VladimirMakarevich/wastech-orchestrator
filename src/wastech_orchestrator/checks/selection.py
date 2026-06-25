"""Deterministic check-set selection by diff (no LLM).

Which command sets run for a task is a pure function of the changed paths and each set's selection
``paths`` globs — never an agent's decision. This keeps the "flow never supplies commands" ceiling
intact and makes selection reproducible and unit-testable.
"""

from __future__ import annotations

from collections.abc import Sequence

from wastech_orchestrator.checks.model import ResolvedCheckSet
from wastech_orchestrator.globmatch import path_matches_any


def select_check_sets(
    sets: Sequence[ResolvedCheckSet], changed_paths: Sequence[str] | None
) -> tuple[ResolvedCheckSet, ...]:
    """Return the command sets to run for a diff (the union of matching sets), deterministically.

    ``changed_paths`` is tri-state:

    * ``None`` — the diff could not be computed (e.g. git is not wired in a unit harness) → run
      **all** sets (conservative: a subset cannot be proven safe).
    * ``[]`` — the diff is empty (the task changed no code) → run **nothing**; the node then passes
      vacuously (there is nothing to check).
    * a non-empty list — match each set's ``paths`` globs against the changed paths and run the
      **union** of the matching sets. A set with no ``paths`` always runs (on any non-empty diff),
      so a catch-all set is how an operator covers shared / root files. A changed path claimed by no
      set simply matches nothing and runs no set on its account — it does **not** fall back to
      running every set (that "fail-safe to full" turned any unclaimed root/docs edit into a
      full-repo run on real monorepos, e.g. pulling an unrunnable backend gate into a frontend job).

    The result preserves ``sets`` order and de-dups by set name.
    """
    if changed_paths is None:
        return tuple(sets)
    if not changed_paths:
        return ()
    paths = [p.replace("\\", "/") for p in changed_paths]
    selected: list[ResolvedCheckSet] = []
    seen: set[str] = set()
    for cset in sets:
        runs = not cset.paths or any(_set_matches_path(cset, path) for path in paths)
        if runs and cset.name not in seen:
            seen.add(cset.name)
            selected.append(cset)
    return tuple(selected)


def _set_matches_path(cset: ResolvedCheckSet, path: str) -> bool:
    return path_matches_any(path, cset.paths)
