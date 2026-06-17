"""Flow prompt assembly from role_file (P1.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.core.flow.prompt import RoleFileError, read_role_file, render_role_prompt


def test_render_substitutes_allowlisted_path_vars(tmp_path: Path) -> None:
    (tmp_path / "roles").mkdir()
    (tmp_path / "roles" / "impl.md").write_text("Implement {task_path} in {repo}\n", "utf-8")
    out = render_role_prompt(
        tmp_path, "roles/impl.md", {"task_path": "/t/task.md", "repo": "/repo"}
    )
    assert out == "Implement /t/task.md in /repo\n"


def test_unknown_braces_pass_through(tmp_path: Path) -> None:
    (tmp_path / "r.md").write_text("schema {\"a\": 1} and {not_allowed}\n", "utf-8")
    out = render_role_prompt(tmp_path, "r.md", {})
    assert out == "schema {\"a\": 1} and {not_allowed}\n"


def test_role_file_traversal_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "flow").mkdir()
    (tmp_path / "secret.md").write_text("secret", "utf-8")
    with pytest.raises(RoleFileError):
        read_role_file(tmp_path / "flow", "../secret.md")


def test_missing_role_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RoleFileError):
        read_role_file(tmp_path, "nope.md")
