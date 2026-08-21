"""Where a variable assigned through ``security.extra_environment`` may point.

The key exists to redirect a toolchain's root or cache — ``NUGET_PACKAGES``, ``CARGO_HOME``,
``npm_config_cache``. Inside the clone is the useful destination under strict isolation; advanced
mode may also use writable paths outside it. In either mode one class of value is dangerous in a
way no name check catches: a path landing on the orchestrator's own control surface. A build
writing into ``.worc/`` corrupts the run that launched it; one writing into ``.git/`` corrupts the
repository; one pointed at the exchange rewrites what the next node is told.

The check is split in two halves that answer to different rules:

* **lexical** — no filesystem access at all, so one config file gets one verdict on every machine.
  It belongs to config validation.
* **canonical** — resolves symlinks, ``~`` and the case/UNC aliases of a single path, which needs
  the filesystem *this* host has. It belongs to ``worc preflight``, where a host-specific verdict is
  the norm and where the alternative would be a config that validates on macOS and not on Windows.

Both halves share one notion of "this value contains a path" (:func:`assigned_path_elements`) and
one containment rule: a collision counts in **either** direction, because a value may be neither a
protected directory's parent nor something inside it.
"""

from __future__ import annotations

import contextlib
import platform
import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from wastech_orchestrator.globmatch import path_matches_any
from wastech_orchestrator.runtime_layout import RuntimeLayout

if TYPE_CHECKING:
    from wastech_orchestrator.config.schema import OrchestratorConfig
    from wastech_orchestrator.runtime_layout import InternalDenyPolicy


@dataclass(frozen=True)
class ProtectedPath:
    """One path an assigned variable may not reach, with the name an operator would recognize.

    The label is load-bearing rather than decorative: "collides with a protected path" leaves the
    operator to guess which of a dozen roots was hit, and the two most likely hits — the runtime
    home and the git directory — are fixed by completely different edits.
    """

    label: str
    path: Path


def assigned_path_elements(value: str, *, include_unsplit: bool = True) -> tuple[str, ...]:
    """The path-looking parts of an assigned value, in order (empty when it holds no path).

    List-shaped values are split on both ``:`` and ``;`` on every host, while a Windows drive colon
    (``C:\\`` / ``C:/``) stays part of its element. This keeps the lexical verdict host-independent:
    a Windows list is still understood on POSIX and a POSIX list on Windows. The unsplit value is
    retained by default as a conservative collision candidate for a single foreign-platform path;
    callers that create one ignore rule per real list element set ``include_unsplit=False`` so a
    whole list can never become a synthetic `.git/info/exclude` entry.

    A candidate counts as a path when it is absolute under *either* platform's rules or starts
    with ``~``. Anything else — ``"1"``, ``"C.UTF-8"``, a bare name — is not a path and is neither
    checked nor resolved: guessing what the operator meant would turn ordinary values into spurious
    errors.
    """
    candidates = _split_path_list(value)
    if include_unsplit:
        candidates.append(value)
    seen: set[str] = set()
    elements: list[str] = []
    for candidate in candidates:
        if candidate in seen or not candidate:
            continue
        seen.add(candidate)
        if candidate.startswith("~") or _is_absolute_anywhere(candidate):
            elements.append(candidate)
    return tuple(elements)


def _split_path_list(value: str) -> list[str]:
    """Split a path-list using both platform separators without tearing Windows drive prefixes."""
    parts: list[str] = []
    current: list[str] = []
    for index, char in enumerate(value):
        drive_colon = (
            char == ":"
            and len(current) == 1
            and current[0].isalpha()
            and index + 1 < len(value)
            and value[index + 1] in ("/", "\\")
        )
        if char in (":", ";") and not drive_colon:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def internal_protected_paths(config: OrchestratorConfig) -> tuple[ProtectedPath, ...]:
    """The protected paths derivable from the config alone — the set the lexical half compares to.

    Everything here follows from ``repo.local_path`` and two config keys, so it is the same on every
    machine. Deliberately *not* here: the provider config/credential homes (``$CODEX_HOME``,
    ``$CLAUDE_CONFIG_DIR``, ``~/.claude``) and an explicit ``--env-file``, which are resolved from
    the environment and the command line — they are host state, so they join the deny set only in
    ``worc preflight``, which is allowed to be host-specific.

    ``denied_read_paths`` entries are handled separately by :func:`denied_read_path_collision` so
    their real glob semantics are preserved instead of broadening a pattern to its fixed prefix.

    A **relative** ``repo.local_path`` yields relative protected paths, which then match no assigned
    value at all — an assigned value only counts as a path when it is absolute. That is deliberate,
    not an oversight: resolving the clone path here would resolve it against this process's working
    directory and make the verdict depend on where the operator happened to stand. ``worc install``
    writes an absolute path (it resolves the git root), so the case arises only for the shipped
    template's placeholder, and ``worc preflight`` resolves both sides anyway.
    """
    layout = RuntimeLayout.default(config.repo.local_path)
    root = Path(config.repo.local_path)
    candidates = [
        ProtectedPath("the repository's git directory", root / ".git"),
        ProtectedPath("the orchestrator's control home", layout.control_home),
        ProtectedPath("the orchestrator's private runtime home", layout.private_home),
        ProtectedPath("the agent-facing exchange", layout.exchange_root),
        ProtectedPath("the per-task runtime roots", layout.runs_home),
        ProtectedPath("the committed task lifecycle tree", root / config.paths.tasks_dir),
    ]
    return _dedupe_by_path(candidates)


def host_protected_paths(
    config: OrchestratorConfig, deny_policy: InternalDenyPolicy
) -> tuple[ProtectedPath, ...]:
    """The fixed protected paths for host-aware preflight/run checks, with accurate labels."""
    entries = list(internal_protected_paths(config))
    if deny_policy.env_file is not None:
        entries.append(ProtectedPath("the orchestrator environment file", deny_policy.env_file))
    entries.extend(
        ProtectedPath("a provider's own config or credential home", path)
        for path in deny_policy.provider_homes
    )
    return _dedupe_by_path(entries)


def denied_read_path_collision(
    value: str,
    repo_root: Path,
    patterns: Sequence[str],
    *,
    canonical: bool,
    system: str | None = None,
) -> ProtectedPath | None:
    """The first assigned element that actually matches a repo-relative deny glob.

    Matching the concrete assigned directory avoids both errors caused by reducing a glob to its
    wildcard-free prefix: leading-``**`` patterns remain effective, while ``conf/*.yaml`` no longer
    rejects an unrelated cache directory under ``conf/``. This deliberately does not speculate
    about files a toolchain might create below a directory; it evaluates the configured value.
    """
    fold = _folds_case(system)
    normalize = (lambda raw: _canonical(raw, fold=fold)) if canonical else _lexical
    root = normalize(str(repo_root))
    effective_patterns = tuple(pattern.casefold() for pattern in patterns) if fold else patterns
    for element in assigned_path_elements(value):
        candidate = normalize(element)
        if not _is_within(candidate, root):
            continue
        relative = candidate.relative_to(root).as_posix()
        probe = relative.casefold() if fold else relative
        for original, pattern in zip(patterns, effective_patterns, strict=True):
            if path_matches_any(probe, (pattern,)):
                return ProtectedPath(
                    f"a denied_read_paths target ({original})", repo_root / original
                )
    return None


def lexical_collision(value: str, protected: Sequence[ProtectedPath]) -> ProtectedPath | None:
    """The first protected path any part of *value* collides with by plain comparison, or ``None``.

    No filesystem access and no ``~`` expansion — expanding it would read ``HOME`` and make the
    verdict depend on the machine. A ``~``-relative element therefore only collides here when the
    configured clone path is itself ``~``-relative; on an absolute clone path it is the canonical
    half that catches it.
    """
    return _first_collision(value, protected, _lexical)


def canonical_collision(
    value: str, protected: Sequence[ProtectedPath], *, system: str | None = None
) -> ProtectedPath | None:
    """The first protected path a part of *value* collides with after canonicalization, or ``None``.

    Resolves ``~`` and follows symlinks/junctions on both sides, which is what makes a value pointed
    at a *link* to a protected directory as detectable as one naming it outright. On Windows the
    comparison also folds case, because two spellings of one path address one directory there and a
    case-sensitive comparison would miss the alias; the branch is explicit rather than inferred from
    the strings so both sides are testable on any host.
    """
    fold = _folds_case(system)
    return _first_collision(value, protected, lambda raw: _canonical(raw, fold=fold))


def is_inside(value_element: str, root: Path, *, system: str | None = None) -> bool:
    """Whether a canonicalized *value_element* sits inside *root* (a directory, itself resolved).

    Used to decide the two halves of the cache recipe that only ``worc preflight`` can decide: a
    path inside the clone has to be excluded from git before a build fills it with thousands of
    files, and a path outside it merits the unwritable-path warning only under strict isolation.
    """
    fold = _folds_case(system)
    return _is_within(_canonical(value_element, fold=fold), _canonical(str(root), fold=fold))


def _first_collision(
    value: str,
    protected: Sequence[ProtectedPath],
    normalize: Callable[[str], PurePosixPath],
) -> ProtectedPath | None:
    """Compare every path element against every protected path, both directions; first hit wins.

    Only the protected path is returned, never the element that hit it. The caller reports the
    variable's *name* and what it collided with, which is enough to act on — the operator reads the
    value in their own config file — and it keeps a value that holds something secret against the
    guide's advice from gaining a second surface to leak from, the same rule that keeps assigned
    values out of the preflight report.
    """
    for element in assigned_path_elements(value):
        candidate = normalize(element)
        for entry in protected:
            target = normalize(str(entry.path))
            if _is_within(candidate, target) or _is_within(target, candidate):
                return entry
    return None


def _is_within(inner: PurePosixPath, outer: PurePosixPath) -> bool:
    """Whether *inner* is *outer* or lies under it. Equality counts — naming the root is the worst
    case of pointing inside it, not an exception to it."""
    return inner == outer or inner.is_relative_to(outer)


def _lexical(raw: str) -> PurePosixPath:
    """Normalize for comparison without touching the filesystem.

    Backslashes become separators so a Windows-style value is compared component-wise rather than as
    one opaque name — the same normalization the config validator already applies to a role-file
    path, and the reason ``C:\\repo\\.worc`` is caught on a POSIX host too.

    ``..`` is collapsed, because otherwise it is a one-character bypass of this whole level:
    ``<clone>/.toolcache/../.worc`` shares no component prefix with ``<clone>/.worc`` and would be
    accepted at load, leaving only the canonical level to catch it. Collapsing is done textually,
    which is the *wrong* answer when a component is a symlink (``/a/link/../b`` is not ``/a/b``
    then) — and exactly the right answer here, because a level that must give the same verdict on
    every machine cannot ask the filesystem which components are links. The canonical half resolves
    the same value properly.
    """
    return PurePosixPath(posixpath.normpath(raw.replace("\\", "/")))


def _canonical(raw: str, *, fold: bool) -> PurePosixPath:
    """Expand ``~``, resolve links, and normalize to one comparable form.

    ``Path.resolve()`` is strict about nothing: a cache directory that does not exist yet resolves
    lexically against its existing ancestors, which is exactly what is needed — the operator writes
    the path before the toolchain creates it.
    """
    resolved = Path(raw).expanduser()
    # A path the OS refuses to even inspect (a dead mount, a permission wall) must not take the
    # whole gate down: the lexical half already had its say, and the unresolved form is still worth
    # comparing against.
    with contextlib.suppress(OSError):
        resolved = resolved.resolve()
    text = resolved.as_posix()
    return PurePosixPath(text.casefold() if fold else text)


def _folds_case(system: str | None) -> bool:
    """Whether path comparison ignores case, i.e. whether this is Windows."""
    name = system if system is not None else platform.system()
    return name == "Windows"


def _is_absolute_anywhere(element: str) -> bool:
    """Whether *element* is an absolute path under POSIX **or** Windows rules.

    Both flavors are consulted so the answer does not depend on the host that happens to read the
    config: ``/srv/cache`` is not absolute to Windows and ``C:\\cache`` is not absolute to POSIX,
    yet each is unmistakably a path and each must be checked.

    A leading separator counts on its own, which ``PureWindowsPath.is_absolute()`` denies for a
    drive-less path like ``\\repo\\cache``: Windows itself treats that as absolute (relative to the
    current drive) and would happily resolve it onto a protected directory, so the stricter reading
    is the correct one here.
    """
    return (
        element.startswith(("/", "\\"))
        or PurePosixPath(element).is_absolute()
        or PureWindowsPath(element).is_absolute()
    )


def _dedupe_by_path(entries: list[ProtectedPath]) -> tuple[ProtectedPath, ...]:
    """Order-preserving de-duplication: the control and private homes are one directory today, and
    reporting the same collision twice would read as two problems."""
    seen: set[Path] = set()
    unique: list[ProtectedPath] = []
    for entry in entries:
        if entry.path not in seen:
            seen.add(entry.path)
            unique.append(entry)
    return tuple(unique)
