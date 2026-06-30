"""DerivedIndex — minimal repo introspection for staleness (design §1, plan 04.4).

Answers the only question the curation path needs of the live repo: *does this path / symbol still
exist?* That feeds `apply_delta` validation (write path) and `CleanupJob` staleness (cleanup path).

Two deliberate properties:

* **A rebuildable cache, not memory truth** (NFR3). The source of truth is the **live repo** — a
  tracked-path set (``git ls-files``) plus filesystem stat for paths, and a literal scan for
  symbols. The optional ``derived/repo_map.json`` materialization is a pure cache: deletable and
  recomputable from the current tree, carrying **no audit and no snapshots** (unlike real memory).
* **Cross-platform paths.** Every stored/compared path string is the ``as_posix()`` form (AC-X1),
  and ``git ls-files`` already emits forward slashes on every OS.

The tracked-path source is injected (``tracked_paths_provider``) so the index is unit-testable
without a real git repo; the default shells out to ``git ls-files`` (argv, never a shell string).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from wastech_orchestrator.memory._io import atomic_write_json
from wastech_orchestrator.providers.process import ProcessResult, run_process

# repo_root -> the set of tracked repo-relative POSIX paths. Best-effort: a tree with no git data
# yields an empty set (the filesystem stat in ``path_exists`` still answers correctly).
TrackedPathsProvider = Callable[[Path], frozenset[str]]

# Bounded read for the literal symbol scan — source files are small; this just caps a pathological
# binary/blob so one stale-check can never read an unbounded file into memory.
_MAX_SCAN_BYTES = 2_000_000

# The minimal env keys ``git ls-files`` needs to launch and resolve its binary. Internal, read-only
# orchestrator introspection (like ``GitManager``) — not the sandboxed agent env. ``run_process``
# replaces the whole child env, so PATH must be present or the binary cannot be found.
_GIT_ENV_KEYS = ("PATH", "SystemRoot", "HOME", "HOMEDRIVE", "HOMEPATH", "USERPROFILE")


def git_tracked_paths(
    repo_root: Path,
    *,
    runner: Callable[..., ProcessResult] = run_process,
    timeout_seconds: int = 30,
) -> frozenset[str]:
    """Default provider: the repo's tracked files via ``git ls-files`` (the safe argv runner).

    Ignore-aware and bounded for free (untracked ``node_modules``/build/vendor trees never appear).
    Routes through ``providers.process.run_process`` (argv, ``shell=False``) — the single launch
    chokepoint — capturing stdout via a temp file like ``GitManager``. Best-effort: a tree with no
    git data, a launch error, or a timeout yields an empty set rather than raising, so staleness
    degrades to filesystem stat instead of failing the cleanup pass.
    """
    env = {key: os.environ[key] for key in _GIT_ENV_KEYS if key in os.environ}
    with tempfile.TemporaryDirectory() as scratch:
        stdout_path = Path(scratch) / "stdout"
        result = runner(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            cwd=repo_root,
            env=env,
            timeout_seconds=timeout_seconds,
            stdout_path=str(stdout_path),
        )
        if result.exit_code != 0 or result.timed_out or result.launch_error:
            return frozenset()
        text = stdout_path.read_text(encoding="utf-8", errors="replace")
    return frozenset(item for item in text.split("\0") if item)


class DerivedIndex:
    """Path/symbol existence over the live repo, with an optional ``derived/`` cache plane."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        derived_dir: Path | None = None,
        tracked_paths_provider: TrackedPathsProvider = git_tracked_paths,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._derived_dir = derived_dir
        self._provider = tracked_paths_provider
        self._tracked: frozenset[str] | None = None  # lazy, recomputed on construction

    def tracked_paths(self) -> frozenset[str]:
        """The tracked repo-relative POSIX paths (computed once per instance, then memoized)."""
        if self._tracked is None:
            self._tracked = frozenset(Path(p).as_posix() for p in self._provider(self._repo_root))
        return self._tracked

    def path_exists(self, path: str) -> bool:
        """Whether ``path`` still exists in the repo — tracked by git, or present on disk.

        The filesystem check covers a path that is present but untracked (e.g. generated); together
        they answer "is this referenced path still real?" conservatively (a path missing from both
        is the only thing the cleanup treats as gone).
        """
        norm = Path(path).as_posix()
        if norm in self.tracked_paths():
            return True
        return (self._repo_root / norm).exists()

    def symbol_exists(self, symbol: str, *, paths: Sequence[str] = ()) -> bool:
        """Whether ``symbol`` still appears literally within any of ``paths`` (a minimal grep).

        Scope-bounded: a symbol is checked only against the paths it is attached to. With **no**
        paths to scan the answer is conservatively ``True`` — an unscoped symbol is never treated as
        stale (fail-closed: cleanup must not drop on a check it cannot perform).
        """
        if not symbol:
            return True
        candidates = [p for p in paths if self.path_exists(p)]
        if not candidates:
            return not paths  # no scope to check → conservatively present; all-paths-gone → absent
        for rel in candidates:
            target = self._repo_root / Path(rel).as_posix()
            if not target.is_file():
                continue
            try:
                text = target.read_text(encoding="utf-8", errors="ignore")[:_MAX_SCAN_BYTES]
            except OSError:
                continue
            if symbol in text:
                return True
        return False

    def find_by_basename(self, path: str) -> tuple[str, ...]:
        """Tracked paths sharing ``path``'s basename — rename-remap candidates for a moved file.

        Excludes ``path`` itself; returns POSIX paths sorted for determinism. Used by the cleanup
        pass to remap an entity whose file moved (same basename) before falling back to quarantine.
        """
        target = Path(path).name
        norm = Path(path).as_posix()
        return tuple(
            sorted(p for p in self.tracked_paths() if p != norm and Path(p).name == target)
        )

    def write_cache(self, *, generated_at: str) -> Path | None:
        """Materialize the tracked-path set to ``derived/repo_map.json`` (the rebuildable cache).

        A pure cache: it carries no audit row and no snapshot (distinct from durable memory) and can
        be deleted and recomputed from the live tree at any time. ``generated_at`` is injected (no
        hidden clock). Returns the written path, or ``None`` when no ``derived_dir`` was configured.
        """
        if self._derived_dir is None:
            return None
        payload: dict[str, Any] = {
            "generated_at": generated_at,
            "tracked_paths": sorted(self.tracked_paths()),
        }
        cache_path = self._derived_dir / "repo_map.json"
        atomic_write_json(cache_path, payload)
        return cache_path
