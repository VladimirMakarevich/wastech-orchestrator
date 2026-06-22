"""Deterministic classification of diff changes that require human approval."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from wastech_orchestrator.git_manager import ChangedPath

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


def classify_dangerous_diff(entries: tuple[ChangedPath, ...]) -> DangerousDiff | None:
    """Return deletion/dependency risk, or ``None`` for an ordinary diff."""
    deleted: set[str] = set()
    dependencies: set[str] = set()
    for entry in entries:
        # The first character is the git name-status code; rename/copy carry a numeric similarity
        # score suffix (e.g. ``R100``), so match the code exactly rather than by prefix.
        code = entry.status.upper()[:1]
        if code == "D":
            deleted.add(entry.path)
        elif code == "R" and entry.previous_path is not None:
            deleted.add(entry.previous_path)
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


def _is_dependency_path(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _DEPENDENCY_PATTERNS)
