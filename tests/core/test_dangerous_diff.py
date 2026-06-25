"""Unit tests for the deletion-approval allowlist on the dangerous-diff classifier.

The classifier's base behavior (ordinary modify → None, deletion/dependency risk, renamed manifest)
is covered in ``tests/core/test_hitl.py``; this file focuses on
``security.deletion_approval_exempt_paths`` — the operator allowlist that exempts deletions/renames
from the approval gate without ever waving through a dependency-manifest change.
"""

from __future__ import annotations

from wastech_orchestrator.core.dangerous_diff import (
    classify_dangerous_diff,
    exempted_deletions,
)
from wastech_orchestrator.git_manager import ChangedPath


def test_exempt_md_deletion_is_not_dangerous() -> None:
    entries = (ChangedPath(status="D", path="docs/old.md"),)
    assert classify_dangerous_diff(entries, ("**/*.md",)) is None


def test_no_exemptions_reproduces_default_gating() -> None:
    entries = (ChangedPath(status="D", path="docs/old.md"),)
    result = classify_dangerous_diff(entries)
    assert result is not None and result.risk == "deletion"


def test_mixed_diff_gates_only_the_non_exempt_deletion() -> None:
    entries = (
        ChangedPath(status="D", path="docs/a.md"),
        ChangedPath(status="D", path="src/x.py"),
    )
    result = classify_dangerous_diff(entries, ("**/*.md",))
    assert result is not None
    assert result.risk == "deletion"
    assert result.paths == ("src/x.py",)
    assert result.deleted_paths == ("src/x.py",)


def test_dependency_manifest_is_never_exemptable() -> None:
    # Even a catch-all `**` exemption cannot wave through a deleted dependency manifest: it stays in
    # the dependency set, so the diff is still gated (as a `dependency` risk).
    entries = (ChangedPath(status="D", path="package.json"),)
    result = classify_dangerous_diff(entries, ("**",))
    assert result is not None
    assert result.risk == "dependency"
    assert result.deleted_paths == ()
    assert result.dependency_paths == ("package.json",)


def test_rename_of_exempt_file_is_not_dangerous() -> None:
    # A rename-away deletes the previous path; if it matches the allowlist it is exempt.
    entries = (
        ChangedPath(status="R100", path="docs/b.md", previous_path="docs/a.md"),
    )
    assert classify_dangerous_diff(entries, ("**/*.md",)) is None


def test_rename_from_source_to_md_still_gates_the_source_deletion() -> None:
    # Renaming source code to a .md path deletes the source (previous) path, which is not exempt.
    entries = (
        ChangedPath(status="R100", path="docs/a.md", previous_path="src/a.py"),
    )
    result = classify_dangerous_diff(entries, ("**/*.md",))
    assert result is not None
    assert result.risk == "deletion"
    assert result.deleted_paths == ("src/a.py",)


def test_exempted_deletions_reports_the_waved_paths() -> None:
    entries = (
        ChangedPath(status="D", path="docs/a.md"),
        ChangedPath(status="D", path="src/x.py"),
        ChangedPath(status="R100", path="docs/c.md", previous_path="docs/b.md"),
    )
    assert exempted_deletions(entries, ("**/*.md",)) == ("docs/a.md", "docs/b.md")


def test_exempted_deletions_empty_allowlist_is_empty() -> None:
    entries = (ChangedPath(status="D", path="docs/a.md"),)
    assert exempted_deletions(entries, ()) == ()
