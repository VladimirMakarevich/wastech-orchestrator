"""The merge gate: which conflicted paths the flow left without an observable decision.

Pure unit tests over ``_undecided_conflicts`` / ``_undecided_conflicts_reason`` /
``_format_conflict_report`` — no git, no repository, no provider. The end-to-end behavior (a real
conflicting merge, refused or committed) lives in ``tests/core/test_merge_task.py``.
"""

from __future__ import annotations

from wastech_orchestrator.core.orchestrator import (
    _format_conflict_report,
    _undecided_conflicts,
    _undecided_conflicts_reason,
)
from wastech_orchestrator.git_manager import ConflictedPath, ConflictEvidence


def _entry(
    path: str = "a.txt",
    *,
    code: str = "UU",
    description: str = "both sides modified it",
    evidence: ConflictEvidence = ConflictEvidence.WORKTREE_BYTES,
    exists: bool = True,
    digest: str | None = "sha-before",
    has_markers: bool = True,
    binary: bool = False,
) -> ConflictedPath:
    return ConflictedPath(
        path=path,
        code=code,
        description=description,
        has_base=True,
        has_ours=True,
        has_theirs=True,
        evidence=evidence,
        exists=exists,
        digest=digest,
        binary=binary,
        has_markers=has_markers,
    )


def test_unchanged_worktree_bytes_are_not_a_decision() -> None:
    before = [_entry()]

    undecided = _undecided_conflicts(before, [_entry()])

    assert [entry.path for entry, _why in undecided] == ["a.txt"]
    assert "byte-identical" in undecided[0][1]


def test_changed_bytes_are_a_decision() -> None:
    assert _undecided_conflicts([_entry()], [_entry(digest="sha-after")]) == []


def test_a_deleted_file_is_a_decision() -> None:
    # Deleting a file one side removed is a resolution like any other.
    after = _entry(exists=False, digest=None)

    assert _undecided_conflicts([_entry()], [after]) == []


def test_a_restored_file_is_a_decision() -> None:
    before = _entry(code="DU", exists=False, digest=None, has_markers=False)

    assert _undecided_conflicts([before], [_entry(code="DU", has_markers=False)]) == []


def test_both_sides_deleting_needs_no_evidence() -> None:
    # `DD`: no content can express a preference, so the absence already there is the outcome.
    gone = _entry(
        code="DD",
        description="both sides deleted it",
        evidence=ConflictEvidence.NOTHING_TO_DECIDE,
        exists=False,
        digest=None,
        has_markers=False,
    )

    assert _undecided_conflicts([gone], [gone]) == []


def test_an_out_of_band_conflict_is_refused_even_when_the_bytes_moved() -> None:
    # A submodule pointer is decided by a commit id; editing a file here cannot express it, so a
    # changed working tree is not evidence that anyone resolved it.
    before = _entry(path="vendor/lib", evidence=ConflictEvidence.OUT_OF_BAND)
    after = _entry(path="vendor/lib", evidence=ConflictEvidence.OUT_OF_BAND, digest="sha-after")

    undecided = _undecided_conflicts([before], [after])

    assert [entry.path for entry, _why in undecided] == ["vendor/lib"]
    assert "editing a file" in undecided[0][1]


def test_an_unreadable_path_fails_closed() -> None:
    after = _entry(exists=True, digest=None)  # probed but unreadable

    undecided = _undecided_conflicts([_entry()], [after])

    assert "nothing readable" in undecided[0][1]


def test_a_path_that_left_the_index_is_accepted() -> None:
    # Something staged it despite the role contract; its content is in the tree either way, and the
    # caller logs the fact rather than refusing a resolution that exists.
    assert _undecided_conflicts([_entry()], []) == []


def test_the_classifier_never_reads_a_git_code() -> None:
    # The layering pin: `code`/`description` are rendered into the refusal, never branched on. Two
    # entries whose codes are nonsense must classify exactly like their well-named twins.
    nonsense = _entry(code="ZZ", description="not a git verdict at all")
    resolved = _entry(code="ZZ", description="not a git verdict at all", digest="sha-after")

    assert _undecided_conflicts([nonsense], [nonsense]) != []
    assert _undecided_conflicts([nonsense], [resolved]) == []


def test_the_reason_names_every_undecided_path_and_caps_the_list() -> None:
    entries = [_entry(path=f"f{i:02d}.txt") for i in range(25)]
    undecided = _undecided_conflicts(entries, entries)

    reason = _undecided_conflicts_reason(undecided, total=30, base_branch="main")

    assert "25 of 30 conflicted path(s) carry no decision" in reason
    assert "f00.txt" in reason and "f19.txt" in reason
    assert "f20.txt" not in reason  # capped
    assert "(+5 more)" in reason
    assert "still open" in reason and "git merge origin/main" in reason


def test_the_report_tells_a_marker_less_conflict_apart_from_a_marked_one() -> None:
    marked = _entry(path="README.md")
    unmarked = _entry(
        path="shared.txt",
        code="UD",
        description="we modified it, base deleted it",
        has_markers=False,
    )

    report = _format_conflict_report("m1", [marked, unmarked])

    assert "## README.md — both sides modified it (`UU`)" in report
    assert "remove every conflict marker" in report
    assert "## shared.txt — we modified it, base deleted it (`UD`)" in report
    assert "no conflict markers" in report
    assert "records no decision and the merge will be refused" in report


def test_the_report_calls_out_what_cannot_be_resolved_in_the_tree() -> None:
    report = _format_conflict_report(
        "m1", [_entry(path="vendor/lib", evidence=ConflictEvidence.OUT_OF_BAND)]
    )

    assert "These you cannot resolve here" in report
    assert "- vendor/lib" in report
