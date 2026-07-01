"""Flow prompt assembly from role_file (P1.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

import wastech_orchestrator
from wastech_orchestrator.core.flow.prompt import RoleFileError, read_role_file, render_role_prompt

_IMPL_ROLES = Path(wastech_orchestrator.__file__).parent / "packaged" / "flows" / "implementation"


def test_render_substitutes_allowlisted_path_vars(tmp_path: Path) -> None:
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "impl.md").write_text("Implement {task_path} in {repo}\n", "utf-8")
    out = render_role_prompt(
        tmp_path, "roles/impl.md", {"task_path": "/t/task.md", "repo": "/repo"}
    )
    assert out == "Implement /t/task.md in /repo\n"


def test_unknown_braces_pass_through(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text('schema {"a": 1} and {not_allowed}\n', "utf-8")
    out = render_role_prompt(tmp_path, "r.md", {})
    assert out == 'schema {"a": 1} and {not_allowed}\n'


def test_role_file_traversal_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "flow").mkdir()
    (tmp_path / "secret.md").write_text("secret", "utf-8")
    with pytest.raises(RoleFileError):
        read_role_file(tmp_path / "flow", "../secret.md")


def test_missing_role_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RoleFileError):
        read_role_file(tmp_path, "nope.md")


@pytest.mark.parametrize("role_file", ["implementation.md", "fixing.md"])
def test_packaged_role_subtask_clause_is_conditional(role_file: str) -> None:
    # Not decomposed: the subtask clause is dropped entirely — no dangling "subtask" fragment.
    not_decomposed = render_role_prompt(_IMPL_ROLES, role_file, {})
    assert "subtask" not in not_decomposed.lower()

    # Decomposed: the clause renders with the real subtask numbers and spec path.
    decomposed = render_role_prompt(
        _IMPL_ROLES,
        role_file,
        {"subtask_order": 2, "subtask_count": 3, "subtask_spec_path": "specs/sub-2.md"},
    )
    assert "subtask 2 of 3" in decomposed
    assert "specs/sub-2.md" in decomposed
    assert "{subtask" not in decomposed  # no unsubstituted placeholders left
