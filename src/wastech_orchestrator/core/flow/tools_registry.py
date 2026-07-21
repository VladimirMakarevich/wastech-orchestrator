"""Tool registry — resolves an operator ``tool`` name → a registered executable path (P5).

Modeled on :class:`~wastech_orchestrator.core.flow.registry.FlowRegistry` (NOT on a checker
mechanism — there is no checker registry, only a closed ``Literal``). Operator tools live under
``<repo>/.worc/tools/`` and are **file-trusted**, exactly like operator flows and ``config.yaml``:
the operator owns the directory. The registry's job is the fail-closed boundary — a flow may name
only a real, contained, executable file, never an arbitrary path:

* **containment** — the resolved real path (symlinks followed) must live inside ``tools_dir``; a
  traversal (``../x``) or a symlink pointing out is rejected;
* **existence** — the name must resolve to a regular file;
* **executability** — POSIX: the ``+x`` bit; Windows: a launchable suffix (``.exe``/``.bat``/…),
  since Windows has no execute bit (cross-platform rule).

Trust is *file-based*; a signature / hash registry is a deferred follow-up (same decision as flows,
security-ceiling.md §8). The registry never launches anything and holds no state beyond the
directory path.
"""

from __future__ import annotations

import os
from pathlib import Path

# Windows has no execute bit; a file is "launchable" by its suffix. Kept small and honest — these
# are the extensions the safe runner can start directly without a shell. A Python/other tool on
# Windows ships as one of these (e.g. a ``.bat`` wrapper) — the same limitation the docs state. On
# POSIX the ``+x`` bit + shebang cover every language.
_WINDOWS_EXECUTABLE_SUFFIXES = frozenset({".exe", ".bat", ".cmd", ".com"})


class ToolResolutionError(Exception):
    """Raised when a tool name cannot be resolved to a registered executable (fail-closed)."""


class ToolRegistry:
    """Resolve an operator ``tool`` name → a validated executable :class:`~pathlib.Path`.

    Constructed with the operator ``.worc/tools/`` directory. A missing directory is not an error at
    construction — every :meth:`resolve` simply fails closed, so a repo with no tools loads fine and
    a flow that references one is rejected at validation.
    """

    def __init__(self, tools_dir: Path) -> None:
        self._dir = tools_dir

    @property
    def tools_dir(self) -> Path:
        return self._dir

    def resolve(self, name: str) -> Path:
        """Resolve *name* to its executable path, or raise :class:`ToolResolutionError`.

        The returned path is the resolved (symlink-followed) real path, guaranteed inside
        ``tools_dir``, a regular file, and executable on the host OS.

        A packaged flow names one fixed tool (e.g. ``check_chapter``) that must resolve on every OS,
        but a POSIX tool is an extensionless ``+x`` script while a Windows tool needs a launchable
        suffix. So on Windows the bare name is also tried with each launcher suffix appended
        (``check_chapter`` → ``check_chapter.cmd``); every candidate passes the identical
        containment/existence/executability checks, so the fail-closed boundary is unchanged.
        """
        if not name or not name.strip():
            raise ToolResolutionError("tool name is empty")
        base = self._dir.resolve()
        # The operator-supplied name must stay inside the tools dir; a traversal/symlink escape is
        # fatal regardless of any launcher suffix appended below (the suffixed variants share this
        # parent, so checking the bare name is sufficient and keeps the message precise).
        bare = (self._dir / name).resolve()
        if not _is_within(bare, base):
            raise ToolResolutionError(
                f"tool {name!r} resolves outside the tools directory {base.as_posix()!r} "
                "(path traversal or symlink escape) — rejected fail-closed"
            )
        for candidate in _candidate_names(name):
            path = (self._dir / candidate).resolve()
            if path.is_file() and _is_executable(path):
                return path
        # Nothing launchable found: report against the bare name (the common case) with the same
        # actionable guidance as before.
        if not bare.is_file():
            raise ToolResolutionError(
                f"tool {name!r} not found under {base.as_posix()!r} "
                f"(expected an executable file at .worc/tools/{name})"
            )
        raise ToolResolutionError(
            f"tool {name!r} at {bare.as_posix()!r} is not executable "
            "(POSIX: set the +x bit; Windows: use a launchable suffix such as .exe/.bat/.cmd)"
        )


def _candidate_names(name: str) -> list[str]:
    """The file names to try for a tool, in priority order.

    POSIX resolves the bare name (an extensionless ``+x`` script). Windows has no execute bit, so a
    bare name is launchable only if it already carries a suffix; otherwise each known launcher
    suffix is appended so one fixed tool name in a packaged flow finds its ``.cmd``/``.exe``
    sibling. Sorted for deterministic resolution order.
    """
    if os.name != "nt" or Path(name).suffix.lower() in _WINDOWS_EXECUTABLE_SUFFIXES:
        return [name]
    return [name, *(name + suffix for suffix in sorted(_WINDOWS_EXECUTABLE_SUFFIXES))]


def _is_within(path: Path, base: Path) -> bool:
    """Whether *path* is *base* or lives inside it (component-wise, not string-prefix)."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _is_executable(path: Path) -> bool:
    """Whether *path* is launchable on the host OS (POSIX ``+x`` bit / Windows suffix)."""
    if os.name == "nt":
        return path.suffix.lower() in _WINDOWS_EXECUTABLE_SUFFIXES
    return os.access(path, os.X_OK)
