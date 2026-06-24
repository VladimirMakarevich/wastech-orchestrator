"""Deterministic check-set selection by diff (no LLM).

Which command sets run for a task is a pure function of the changed paths and each set's selection
``paths`` globs — never an agent's decision. This keeps the "flow never supplies commands" ceiling
intact and makes selection reproducible and unit-testable.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache

from wastech_orchestrator.checks.model import ResolvedCheckSet


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
    return any(_compile_glob(pattern).fullmatch(path) is not None for pattern in cset.paths)


@lru_cache(maxsize=256)
def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate a repo-relative glob into an anchored regex.

    ``**`` crosses path separators (``**/`` = zero or more leading directories, so ``**/*.md``
    matches both ``README.md`` and ``docs/a/b.md``); a single ``*`` and ``?`` stay within one
    segment. Dependency-free, so it works on the ``requires-python`` floor (3.12, before
    ``PurePath.full_match``).
    """
    norm = pattern.replace("\\", "/")
    out: list[str] = []
    i, n = 0, len(norm)
    while i < n:
        c = norm[i]
        if c == "*":
            if i + 1 < n and norm[i + 1] == "*":
                if i + 2 < n and norm[i + 2] == "/":
                    out.append("(?:.*/)?")  # **/  → zero or more leading directories
                    i += 3
                else:
                    out.append(".*")  # ** → anything, including separators
                    i += 2
                continue
            out.append("[^/]*")  # * → within one path segment
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
        elif c == "/":
            out.append("/")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out))
