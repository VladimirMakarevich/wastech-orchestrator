"""Flow prompt assembly from role_file."""

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


# Each optional context item carries its OWN heading inside its own `{?…}` block, so a
# heading can never render without its content (an orphan) and content can never render without its
# heading — no Core-side sentinel guesses at "will this section be empty". Per role file: the
# (variable, heading) pairs it ships.
_CONTEXT_ITEMS: dict[str, tuple[tuple[str, str], ...]] = {
    "planning.md": (("memory_path", "## Repository Memory"),),
    "review.md": (("memory_path", "## Repository Memory"),),
    "fixing.md": (
        ("memory_path", "## Repository Memory"),
        ("subtask_spec_path", "## Subtask Scope"),
    ),
    "implementation.md": (
        ("memory_path", "## Repository Memory"),
        ("subtask_spec_path", "## Subtask Scope"),
        ("predecessor_context", "## Predecessor Handoff"),
    ),
}

_ITEM_PATH = {
    "memory_path": "mem/packet.md",
    "subtask_spec_path": "specs/sub-1.md",
    "predecessor_context": "handoff/prev.md",
}


def _only(var: str) -> dict[str, object | None]:
    """Variables with exactly one optional context item present."""
    values: dict[str, object | None] = {var: _ITEM_PATH[var]}
    if var == "subtask_spec_path":  # the subtask clause also interpolates its N-of-M numbers
        values |= {"subtask_order": 1, "subtask_count": 2}
    return values


@pytest.mark.parametrize("role_file", sorted(_CONTEXT_ITEMS))
def test_no_optional_context_heading_when_nothing_to_show(role_file: str) -> None:
    out = render_role_prompt(_IMPL_ROLES, role_file, {})
    for _var, heading in _CONTEXT_ITEMS[role_file]:
        assert heading not in out  # no heading with nothing under it
    assert "Additional Project Context" not in out  # the old shared heading is gone for good
    assert not out.endswith("\n\n\n")  # and it leaves no trailing blank-line residue


@pytest.mark.parametrize("role_file", sorted(_CONTEXT_ITEMS))
def test_optional_context_heading_renders_only_with_its_own_item(role_file: str) -> None:
    items = _CONTEXT_ITEMS[role_file]
    for var, heading in items:
        out = render_role_prompt(_IMPL_ROLES, role_file, _only(var))
        assert out.count(heading) == 1  # its own heading renders, exactly once...
        assert _ITEM_PATH[var] in out  # ...together with its own content (never orphaned)
        for other_var, other_heading in items:  # ...and no sibling heading leaks
            if other_var != var:
                assert other_heading not in out
        assert "{?" not in out and "{/" not in out  # no unresolved block markers


def test_no_packaged_role_prompt_uses_a_core_side_context_sentinel() -> None:
    # Anti-drift: the `additional_context` sentinel was removed because a Core-hardcoded candidate
    # list cannot know what an operator puts under a shared heading — it silently orphaned content
    # for any optional variable outside that list. Keep every role prompt on the per-item pattern.
    for role_file in sorted(_IMPL_ROLES.glob("*.md")):
        assert "additional_context" not in role_file.read_text(encoding="utf-8")


def test_packaged_implementation_plan_clause_is_conditional() -> None:
    # The opening "by following the plan" clause renders only when a plan artifact exists.
    no_plan = render_role_prompt(_IMPL_ROLES, "implementation.md", {})
    assert "following the plan" not in no_plan
    assert "in the working tree. Make the smallest" in no_plan
    with_plan = render_role_prompt(_IMPL_ROLES, "implementation.md", {"plan_path": "plan.md"})
    assert "by following the plan. Make the smallest" in with_plan


def test_packaged_review_plan_clauses_are_conditional() -> None:
    # Both plan references in the reviewer prompt are gated on the plan's presence.
    no_plan = render_role_prompt(_IMPL_ROLES, "review.md", {})
    assert "against the task. Report" in no_plan
    assert "against the task and plan" not in no_plan
    assert "the plan's acceptance criteria" not in no_plan
    with_plan = render_role_prompt(_IMPL_ROLES, "review.md", {"plan_path": "plan.md"})
    assert "against the task and plan. Report" in with_plan
    assert "and the plan's acceptance criteria" in with_plan
