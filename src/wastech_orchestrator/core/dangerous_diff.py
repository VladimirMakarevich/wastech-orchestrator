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
    """Approval category and exact normalized paths for a dangerous diff."""

    risk: str
    paths: tuple[str, ...]
    deleted_paths: tuple[str, ...]
    dependency_paths: tuple[str, ...]


def classify_dangerous_diff(
    entries: tuple[ChangedPath, ...], exempt_deletions: tuple[str, ...] = ()
) -> DangerousDiff | None:
    """Return deletion/dependency risk, or ``None`` for an ordinary diff.

    ``exempt_deletions`` is an operator-configured allowlist of repo-relative globs
    (``security.deletion_approval_exempt_paths``): a deleted/renamed path matching one is dropped
    from the **deletion** set so it no longer requires approval. The dependency-manifest set is
    never filtered — a deleted manifest (e.g. ``package.json``) stays in ``dependencies`` and stays
    gated even under a ``**`` exemption, so an allowlist can never wave through a dependency change.
    """
    deleted = _deleted_paths(entries)
    if exempt_deletions:
        deleted = {p for p in deleted if not path_matches_any(p, exempt_deletions)}
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


def exempted_deletions(
    entries: tuple[ChangedPath, ...], exempt_deletions: tuple[str, ...]
) -> tuple[str, ...]:
    """The deleted/renamed paths an allowlist waved through — for the guard's audit log line only.

    Returns the sorted subset of the diff's deletions matching ``exempt_deletions`` (empty allowlist
    => ``()``). A path that is also a dependency manifest still appears here even though it stays
    gated via the dependency set; the log records what the *deletion* classification dropped.
    """
    if not exempt_deletions:
        return ()
    return tuple(
        sorted(p for p in _deleted_paths(entries) if path_matches_any(p, exempt_deletions))
    )


def _is_dependency_path(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _DEPENDENCY_PATTERNS)
