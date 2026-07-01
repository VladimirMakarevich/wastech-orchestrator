"""Memory store layout (01.1): tree creation, idempotent seeding, gitignore, POSIX paths."""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.git_manager import RUNTIME_GITIGNORE_LINES, append_runtime_excludes
from wastech_orchestrator.memory import (
    MEMORY_SCHEMA_VERSION,
    MemoryLayout,
    build_manifest,
    ensure_store,
)


def test_ensure_tree_creates_all_canonical_dirs(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    layout.ensure_tree()
    for directory in (
        layout.long_term,
        layout.short_term,
        layout.entities,
        layout.audit,
        layout.quarantine,
        layout.derived,
    ):
        assert directory.is_dir()
    # The store lives under the .worc home, not under a per-task artifact dir.
    assert layout.root == tmp_path / ".worc" / "memory"


def test_ensure_store_seeds_manifest_and_readme(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    ensure_store(layout, created_at="2026-06-30T00:00:00Z")
    manifest = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
    assert manifest["memory_schema_version"] == MEMORY_SCHEMA_VERSION
    assert manifest["created_at"] == "2026-06-30T00:00:00Z"
    assert "tiers" in manifest
    assert layout.readme_path.is_file()


def test_ensure_store_is_idempotent_and_never_clobbers(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    ensure_store(layout, created_at="2026-06-30T00:00:00Z")
    # A second run with a different stamp must preserve the original manifest (no clobber).
    ensure_store(layout, created_at="2099-01-01T00:00:00Z")
    manifest = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
    assert manifest["created_at"] == "2026-06-30T00:00:00Z"


def test_memory_tree_is_covered_by_existing_worc_gitignore(tmp_path: Path) -> None:
    # AC-S2: no new rule — the whole `.worc/` home is already ignored wholesale, which covers both
    # the canonical store and the per-task packets under `.worc/logs/`. Assert coverage, not a rule.
    assert ".worc/" in RUNTIME_GITIGNORE_LINES
    append_runtime_excludes(tmp_path)
    ignored = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".worc/" in ignored
    layout = MemoryLayout.for_repo(tmp_path)
    # Everything in the store is under the ignored `.worc/` prefix.
    assert layout.root.relative_to(tmp_path).parts[0] == ".worc"


def test_stored_path_strings_use_posix_form(tmp_path: Path) -> None:
    # AC-X1: stored/compared path strings are POSIX (forward slashes) on every OS.
    layout = MemoryLayout.for_repo(tmp_path)
    root_posix = layout.as_posix()
    assert "\\" not in root_posix
    assert root_posix.endswith(".worc/memory")
    assert layout.long_term.as_posix().endswith(".worc/memory/long_term")


def test_build_manifest_is_pure_and_injects_created_at() -> None:
    manifest = build_manifest(created_at="2026-06-30T12:00:00Z")
    assert manifest["created_at"] == "2026-06-30T12:00:00Z"
    assert manifest["memory_schema_version"] == MEMORY_SCHEMA_VERSION
