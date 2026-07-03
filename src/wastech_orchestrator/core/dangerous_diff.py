"""Deterministic classification of diff changes that require human approval."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from wastech_orchestrator.git_manager import ChangedPath
from wastech_orchestrator.globmatch import path_matches_any

_DEPENDENCY_PATTERNS: tuple[str, ...] = (
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "requirements*.txt",
    "setup.py",
    "setup.cfg",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
    "composer.json",
    "composer.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradle.lockfile",
    "libs.versions.toml",
    "Package.swift",
    "Package.resolved",
    "Podfile",
    "Podfile.lock",
    "Cartfile",
    "Cartfile.resolved",
    "pubspec.yaml",
    "pubspec.lock",
    "mix.exs",
    "mix.lock",
    "*.csproj",
    "*.fsproj",
    "*.vbproj",
    "Directory.Packages.props",
    "packages.lock.json",
    "packages.config",
    "paket.dependencies",
    "paket.lock",
    "deps.edn",
    "project.clj",
    "build.sbt",
    "conanfile.py",
    "conanfile.txt",
    "conan.lock",
    "vcpkg.json",
    "MODULE.bazel",
    "WORKSPACE",
    "flake.nix",
    "flake.lock",
    ".terraform.lock.hcl",
)


@dataclass(frozen=True)
class DangerousDiff:
    """Approval category and exact normalized paths for a diff that requires approval."""

    risk: str
    paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]
    dependency_paths: tuple[str, ...]
    #: paths that matched the operator ``security.protected_paths`` always-ask floor (any change).
    protected_paths: tuple[str, ...] = ()


def evaluate_diff_gate(
    entries: tuple[ChangedPath, ...],
    trust_level: str,
    protected_paths: tuple[str, ...] = (),
) -> DangerousDiff | None:
    """Resolve the approval decision for a workspace-write diff, or ``None`` when nothing gates.

    Two layers, checked in order:

    * **``protected_paths`` (the floor).** Any changed path matching one of these repo-relative
      globs requires approval at *any* ``trust_level`` — matched against both the new path and a
      rename's previous path, so moving a file in or out of a protected area still gates.
    * **``trust_level`` (the threshold).** ``strict`` additionally gates any deletion/rename or
      dependency-manifest edit (:func:`classify_dangerous_diff`); ``auto`` gates nothing beyond the
      protected floor.

    The returned paths are the union of every gated category, and ``risk`` reflects which are
    present (``"protected"`` when only protected paths, ``"other"`` for a mix, else the base
    ``"deletion"``/``"dependency"`` category).
    """
    protected = _protected_hits(entries, protected_paths)
    base = classify_dangerous_diff(entries) if trust_level == "strict" else None
    if not protected and base is None:
        return None
    deleted = base.deleted_paths if base is not None else ()
    dependencies = base.dependency_paths if base is not None else ()
    return DangerousDiff(
        risk=_combined_risk(deleted, dependencies, protected),
        paths=tuple(sorted(set(deleted) | set(dependencies) | set(protected))),
        deleted_paths=deleted,
        dependency_paths=dependencies,
        protected_paths=protected,
    )


def classify_dangerous_diff(entries: tuple[ChangedPath, ...]) -> DangerousDiff | None:
    """Return deletion/dependency risk (the ``strict`` diff-shape rule), or ``None`` for an ordinary
    diff. A deletion/rename gates its removed path; any change touching a dependency manifest/lock
    (e.g. ``package.json``) gates that path. This is the level-independent base rule; the
    ``protected_paths`` floor is layered on by :func:`evaluate_diff_gate`."""
    deleted = _deleted_paths(entries)
    dependencies: set[str] = set()
    for entry in entries:
        for candidate in (entry.path, entry.previous_path):
            if candidate is not None and _is_dependency_path(candidate):
                dependencies.add(candidate)

    if not deleted and not dependencies:
        return None
    if deleted and dependencies:
        risk = "other"
    elif deleted:
        risk = "deletion"
    else:
        risk = "dependency"
    return DangerousDiff(
        risk=risk,
        paths=tuple(sorted(deleted | dependencies)),
        deleted_paths=tuple(sorted(deleted)),
        dependency_paths=tuple(sorted(dependencies)),
    )


def _protected_hits(
    entries: tuple[ChangedPath, ...], protected_paths: tuple[str, ...]
) -> tuple[str, ...]:
    """Sorted changed paths matching ``protected_paths`` (new path or a rename's previous path)."""
    if not protected_paths:
        return ()
    hits: set[str] = set()
    for entry in entries:
        for candidate in (entry.path, entry.previous_path):
            if candidate is not None and path_matches_any(candidate, protected_paths):
                hits.add(candidate)
    return tuple(sorted(hits))


def _combined_risk(
    deleted: tuple[str, ...], dependencies: tuple[str, ...], protected: tuple[str, ...]
) -> str:
    present = sum(1 for group in (deleted, dependencies, protected) if group)
    if present > 1:
        return "other"
    if protected:
        return "protected"
    if deleted:
        return "deletion"
    return "dependency"


def _deleted_paths(entries: tuple[ChangedPath, ...]) -> set[str]:
    """The set of paths a diff removes: status ``D`` deletes ``path``; a rename (``R``) deletes its
    ``previous_path``. The first character is the git name-status code; rename/copy carry a numeric
    similarity suffix (e.g. ``R100``), so match the code exactly rather than by prefix."""
    deleted: set[str] = set()
    for entry in entries:
        code = entry.status.upper()[:1]
        if code == "D":
            deleted.add(entry.path)
        elif code == "R" and entry.previous_path is not None:
            deleted.add(entry.previous_path)
    return deleted


def _is_dependency_path(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _DEPENDENCY_PATTERNS)
