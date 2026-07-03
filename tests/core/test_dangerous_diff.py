"""Unit tests for the dangerous-diff policy resolver (``evaluate_diff_gate``).

The level-independent base rule (``classify_dangerous_diff``: ordinary modify → None,
deletion/dependency risk, renamed manifest) is covered in ``tests/core/test_hitl.py``; this file
focuses on ``evaluate_diff_gate`` — the ``trust_level`` × diff-shape × ``security.protected_paths``
matrix that decides which changes require approval.
"""

from __future__ import annotations

from wastech_orchestrator.core.dangerous_diff import evaluate_diff_gate
from wastech_orchestrator.git_manager import ChangedPath

_DELETION = (ChangedPath(status="D", path="src/x.py"),)
_DEPENDENCY = (ChangedPath(status="M", path="package.json"),)
_PLAIN_EDIT = (ChangedPath(status="M", path="src/app.py"),)


# -- strict: gate every deletion / dependency, protected floor is redundant but harmless -----------


def test_strict_gates_a_deletion() -> None:
    result = evaluate_diff_gate(_DELETION, "strict")
    assert result is not None
    assert result.risk == "deletion"
    assert result.paths == ("src/x.py",)
    assert result.protected_paths == ()


def test_strict_gates_a_dependency_edit() -> None:
    result = evaluate_diff_gate(_DEPENDENCY, "strict")
    assert result is not None
    assert result.risk == "dependency"
    assert result.dependency_paths == ("package.json",)


def test_strict_ignores_a_plain_edit() -> None:
    assert evaluate_diff_gate(_PLAIN_EDIT, "strict") is None


# -- auto: the diff-shape gate is off; only a protected_paths match asks ---------------------------


def test_auto_does_not_gate_a_deletion() -> None:
    assert evaluate_diff_gate(_DELETION, "auto") is None


def test_auto_does_not_gate_a_dependency_edit() -> None:
    assert evaluate_diff_gate(_DEPENDENCY, "auto") is None


def test_auto_does_not_gate_a_plain_edit() -> None:
    assert evaluate_diff_gate(_PLAIN_EDIT, "auto") is None


# -- protected_paths: the always-ask floor, at any level -------------------------------------------


def test_auto_gates_a_protected_plain_edit() -> None:
    # A protected path flags ANY change, including a plain modification the base rule ignores.
    result = evaluate_diff_gate(_PLAIN_EDIT, "auto", ("src/**",))
    assert result is not None
    assert result.risk == "protected"
    assert result.paths == ("src/app.py",)
    assert result.protected_paths == ("src/app.py",)
    assert result.deleted_paths == ()
    assert result.dependency_paths == ()


def test_auto_protected_no_match_still_proceeds() -> None:
    assert evaluate_diff_gate(_PLAIN_EDIT, "auto", ("config/**",)) is None


def test_protected_matches_a_rename_source_and_target() -> None:
    # Both the new path and a rename's previous path are matched against the floor.
    entries = (ChangedPath(status="R100", path="src/new.py", previous_path="lib/old.py"),)
    result = evaluate_diff_gate(entries, "auto", ("lib/**",))
    assert result is not None
    assert result.risk == "protected"
    assert result.protected_paths == ("lib/old.py",)


def test_strict_deletion_plus_protected_edit_is_mixed_risk() -> None:
    entries = (
        ChangedPath(status="D", path="src/x.py"),
        ChangedPath(status="M", path="config/app.yaml"),
    )
    result = evaluate_diff_gate(entries, "strict", ("config/**",))
    assert result is not None
    assert result.risk == "other"
    assert result.paths == ("config/app.yaml", "src/x.py")
    assert result.deleted_paths == ("src/x.py",)
    assert result.protected_paths == ("config/app.yaml",)


def test_protected_deletion_is_the_union_not_double_counted() -> None:
    # A protected path that is also deleted appears once in `paths`, in both subsets, risk `other`.
    entries = (ChangedPath(status="D", path="src/security/keys.py"),)
    result = evaluate_diff_gate(entries, "strict", ("src/security/**",))
    assert result is not None
    assert result.risk == "other"
    assert result.paths == ("src/security/keys.py",)
    assert result.deleted_paths == ("src/security/keys.py",)
    assert result.protected_paths == ("src/security/keys.py",)
