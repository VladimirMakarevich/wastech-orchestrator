"""Unit tests for the fatal load-time flow validator (flow-engine P0.3).

Each test covers exactly one violation class so regressions are easy to localise. "Valid flow"
tests confirm that all three co-design flows pass the validator without violations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.core.flow.snapshot import load_flow
from wastech_orchestrator.core.flow.validator import (
    FlowValidationError,
    Violation,
    validate_flow,
)

CODESIGN = Path(__file__).parent.parent.parent / "docs" / "backlog" / "flows" / "co-design"


# -- helpers ------------------------------------------------------------------


def _snap(content: str, tmp_path: Path):  # type: ignore[return]
    p = tmp_path / "test.yaml"
    p.write_text(content)
    return load_flow(p)


def _violations(content: str, tmp_path: Path) -> list[Violation]:
    with pytest.raises(FlowValidationError) as exc_info:
        validate_flow(_snap(content, tmp_path))
    return exc_info.value.violations


def _has(vs: list[Violation], category: str, fragment: str) -> bool:
    return any(v.category == category and fragment in v.message for v in vs)


# -- valid flows pass validation ----------------------------------------------


def test_validate_implementation_yaml_passes() -> None:
    validate_flow(load_flow(CODESIGN / "implementation.yaml"))


def test_validate_deep_research_yaml_passes() -> None:
    validate_flow(load_flow(CODESIGN / "deep_research.yaml"))


def test_validate_security_audit_yaml_passes() -> None:
    validate_flow(load_flow(CODESIGN / "security_audit.yaml"))


# -- graph: edge resolution ---------------------------------------------------


def test_edge_to_unknown_node(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: out }
    - { from: entry, to: ghost }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "ghost")


# -- graph: outcome subset ----------------------------------------------------


def test_wrong_outcome_on_agent_node(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: out, outcome: accept }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "'entry'")
    assert _has(vs, "graph", "'accept'")


def test_wrong_outcome_on_final_handoff_evaluator(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: summary
      kind: evaluator
      role: supervisor
      role_file: roles/supervisor.md
      evaluation_kind: final_handoff
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: summary }
    - { from: summary, to: out, outcome: accept }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "'accept'")
    assert _has(vs, "graph", "summary")


# -- graph: bounded loops -----------------------------------------------------


def test_rework_edge_without_budget(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: check
      kind: evaluator
      role: review
      role_file: roles/review.md
    - id: fix
      kind: agent
      role_file: roles/fix.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: check }
    - { from: check, to: out, outcome: accept }
    - { from: check, to: fix, outcome: rework }
    - { from: fix, to: check }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "unbounded")
    assert _has(vs, "graph", "rework")


def test_fail_edge_without_budget(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: tests
      kind: checks
      checker: command_profile
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: tests }
    - { from: tests, to: out, outcome: pass }
    - { from: tests, to: entry, outcome: fail }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "unbounded")
    assert _has(vs, "graph", "fail")


def test_loop_name_not_in_budgets(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: tests
      kind: checks
      checker: command_profile
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: tests }
    - { from: tests, to: out, outcome: pass }
    - { from: tests, to: entry, outcome: fail, loop: missing_budget }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "missing_budget")


# -- graph: entry / reachability / terminal -----------------------------------


def test_multiple_entry_nodes(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: a
      kind: agent
      role_file: roles/a.md
    - id: b
      kind: agent
      role_file: roles/b.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: a, to: out }
    - { from: b, to: out }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "exactly one entry node")


def test_unreachable_node(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: out
      kind: publish
      policy: pull_request
    - id: orphan1
      kind: agent
      role_file: roles/orphan1.md
    - id: orphan2
      kind: agent
      role_file: roles/orphan2.md
  edges:
    - { from: entry, to: out }
    - { from: orphan1, to: orphan2 }
    - { from: orphan2, to: orphan1 }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "unreachable")
    assert _has(vs, "graph", "orphan")


def test_no_terminal_node(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  budgets:
    fix_loop: 5
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: work
      kind: evaluator
      role: review
      role_file: roles/review.md
    - id: fix
      kind: agent
      role_file: roles/fix.md
  edges:
    - { from: entry, to: work }
    - { from: work, to: fix, outcome: rework, loop: fix_loop }
    - { from: fix, to: work }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "no terminal")


def test_node_cannot_reach_terminal(tmp_path: Path) -> None:
    # entry reaches the terminal (out), but trap1/trap2 form a cycle with no exit to a terminal.
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: trap1
      kind: agent
      role_file: roles/trap1.md
    - id: trap2
      kind: agent
      role_file: roles/trap2.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: out }
    - { from: entry, to: trap1 }
    - { from: trap1, to: trap2 }
    - { from: trap2, to: trap1 }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "cannot reach any terminal")


# -- graph: lineage_affinity and decomposition --------------------------------


def test_lineage_affinity_to_non_editing_lineage_node(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: worker
      kind: agent
      role_file: roles/worker.md
      session_scope: fresh_disposable
    - id: fixer
      kind: agent
      role_file: roles/fixer.md
      lineage_affinity: worker
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: worker }
    - { from: worker, to: fixer }
    - { from: fixer, to: out }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "lineage_affinity")
    assert _has(vs, "graph", "editing_lineage")


def test_decomposition_proposed_by_unknown(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: out }
  decomposition:
    proposed_by: ghost_planner
    sub_flow: [entry]
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "ghost_planner")


# -- ceiling: evaluator invariants --------------------------------------------


def test_evaluator_session_scope_editing_lineage(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: check
      kind: evaluator
      role: review
      role_file: roles/review.md
      session_scope: editing_lineage
  edges:
    - { from: entry, to: check }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "ceiling", "editing_lineage")
    assert _has(vs, "ceiling", "'check'")


def test_evaluator_non_readonly_permission_profile(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: check
      kind: evaluator
      role: review
      role_file: roles/review.md
      permission_profile: workspace-write
  edges:
    - { from: entry, to: check }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "ceiling", "read-only")
    assert _has(vs, "ceiling", "'check'")


# -- ceiling: permission_profile ≤ ceiling ------------------------------------


def test_agent_permission_profile_exceeds_ceiling(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: read-only
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
      permission_profile: workspace-write
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: out }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "ceiling", "exceeds")
    assert _has(vs, "ceiling", "workspace-write")


# -- ceiling: extra_args ------------------------------------------------------


def test_extra_args_forbidden_flag(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
      extra_args:
        - "--dangerously-bypass-approvals-and-sandbox"
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: out }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "ceiling", "extra_args")
    assert _has(vs, "ceiling", "dangerously")


# -- ceiling: role_file path traversal ----------------------------------------


def test_role_file_path_traversal(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: "../../etc/passwd"
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: out }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "ceiling", "path traversal")
    assert _has(vs, "ceiling", "'entry'")


def test_absolute_role_file_path(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: /etc/passwd
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: out }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "ceiling", "path traversal")


# -- error structure ----------------------------------------------------------


def test_validation_error_collects_multiple_violations(tmp_path: Path) -> None:
    # Two ceiling violations in one flow: evaluator has editing_lineage AND
    # non-readonly permission_profile.  Both must appear in the collected list.
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: check
      kind: evaluator
      role: review
      role_file: roles/review.md
      session_scope: editing_lineage
      permission_profile: workspace-write
  edges:
    - { from: entry, to: check }
"""
    vs = _violations(yaml, tmp_path)
    assert len(vs) >= 2
    assert _has(vs, "ceiling", "editing_lineage")
    assert _has(vs, "ceiling", "read-only")


def test_validation_error_message_lists_violations(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: check
      kind: evaluator
      role: review
      role_file: roles/review.md
      session_scope: editing_lineage
  edges:
    - { from: entry, to: check }
"""
    with pytest.raises(FlowValidationError) as exc_info:
        validate_flow(_snap(yaml, tmp_path))
    msg = str(exc_info.value)
    assert "violation" in msg
    assert "[ceiling]" in msg


def test_violation_is_frozen() -> None:
    v = Violation("graph", "some message")
    with pytest.raises(AttributeError):
        v.message = "changed"  # type: ignore[misc]


def test_validate_flow_returns_none_on_valid(tmp_path: Path) -> None:
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: out }
"""
    result = validate_flow(_snap(yaml, tmp_path))
    assert result is None
