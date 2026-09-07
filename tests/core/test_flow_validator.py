"""Unit tests for the fatal load-time flow validator.

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
    validate_disabled_nodes,
    validate_flow,
)

CODESIGN = (
    Path(__file__).parent.parent.parent / "src" / "wastech_orchestrator" / "packaged" / "flows"
)


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


_SUPERVISOR_FLOW = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  supervisor:
{block}
  nodes:
    - id: a
      kind: agent
      role_file: t/a.md
    - id: b
      kind: publish
      policy: pull_request
  edges:
    - {{ from: a, to: b }}
"""


def test_supervisor_role_file_traversal_rejected(tmp_path: Path) -> None:
    content = _SUPERVISOR_FLOW.format(block="    role_file: ../escape.md\n")
    assert _has(_violations(content, tmp_path), "ceiling", "path traversal")


def test_supervisor_finalize_role_file_traversal_rejected(tmp_path: Path) -> None:
    content = _SUPERVISOR_FLOW.format(block="    finalize_role_file: ../../escape.md\n")
    assert _has(_violations(content, tmp_path), "ceiling", "path traversal")


def test_supervisor_handoff_role_file_traversal_rejected(tmp_path: Path) -> None:
    content = _SUPERVISOR_FLOW.format(block="    handoff_role_file: ../escape.md\n")
    assert _has(_violations(content, tmp_path), "ceiling", "path traversal")


def test_supervisor_contained_paths_accepted(tmp_path: Path) -> None:
    content = _SUPERVISOR_FLOW.format(
        block="    role_file: t/supervisor.md\n    finalize_role_file: t/summary.md\n"
    )
    validate_flow(_snap(content, tmp_path))  # flow-dir-contained paths are fine


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


def test_conflicting_provider_override_rejected_under_affinity(tmp_path: Path) -> None:
    # Durable sessions: a node cannot resume another provider's editing session, so an
    # explicit provider that differs from its lineage_affinity target's provider is rejected.
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: implementation
      kind: agent
      role_file: roles/impl.md
      session_scope: editing_lineage
      provider: claude
    - id: fixing
      kind: agent
      role_file: roles/fix.md
      session_scope: editing_lineage
      lineage_affinity: implementation
      provider: codex
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: implementation, to: fixing }
    - { from: fixing, to: out }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "conflicts with")
    assert _has(vs, "graph", "across providers")


def test_lineage_affinity_chain_rejected(tmp_path: Path) -> None:
    # multiple-editing-lineages: a lineage_affinity target must itself be a lineage owner (no
    # affinity of its own). A chain (fixing → implementation → base) is rejected — one hop only.
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: base
      kind: agent
      role_file: roles/base.md
      session_scope: editing_lineage
    - id: implementation
      kind: agent
      role_file: roles/impl.md
      session_scope: editing_lineage
      lineage_affinity: base
    - id: fixing
      kind: agent
      role_file: roles/fix.md
      session_scope: editing_lineage
      lineage_affinity: implementation
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: base, to: implementation }
    - { from: implementation, to: fixing }
    - { from: fixing, to: out }
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "lineage owner")
    assert _has(vs, "graph", "chains are not allowed")


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


def test_decomposition_region_without_entry_edge_rejected(tmp_path: Path) -> None:
    # #9: references resolve, but no edge from proposed_by lands directly in the region. The
    # partitioner would crash with StopIteration resolving region_entry; the validator rejects it.
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: planner
      kind: agent
      role_file: roles/planner.md
    - id: gate
      kind: agent
      role_file: roles/gate.md
    - id: impl
      kind: agent
      role_file: roles/impl.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: planner, to: gate }
    - { from: gate, to: impl }
    - { from: impl, to: out }
  decomposition:
    proposed_by: planner
    sub_flow: [impl]
"""
    vs = _violations(yaml, tmp_path)
    assert _has(vs, "graph", "enters the sub_flow region")


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


# -- per-task disabled-node validation (Stage-enum removal) -------------------
#
# ``validate_disabled_nodes`` is the second validation tier (the gate cannot see the flow): node
# existence + skip-outcome routing soundness against the resolved snapshot.

_CUSTOM_REVIEW_FLOW = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  budgets: { review_loop: 3 }
  nodes:
    - id: entry
      kind: agent
      role_file: roles/entry.md
    - id: build
      kind: agent
      role_file: roles/build.md
    - id: code_review
      kind: evaluator
      role: review
      role_file: roles/review.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: entry, to: build }
    - { from: build, to: code_review }
    - { from: code_review, to: out, outcome: accept }
    - { from: code_review, to: build, outcome: rework, loop: review_loop }
"""

# A router agent whose only edge is an explicit ``route:`` outcome: valid to load (``route:*`` is
# always allowed), but its skip-outcome ``done`` matches no edge — disabling it would strand.
_STRANDED_ROUTER_FLOW = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: router
      kind: agent
      role_file: roles/router.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: router, to: out, outcome: route:go }
"""


def _impl_snap():  # type: ignore[no-untyped-def]
    return load_flow(CODESIGN / "implementation.yaml")


def test_disabled_nodes_empty_is_ok() -> None:
    validate_disabled_nodes(_impl_snap(), frozenset())  # no raise


def test_disabled_real_node_ok() -> None:
    # ``review`` is a real node with a normal forward edge — disabling it validates clean.
    validate_disabled_nodes(_impl_snap(), frozenset({"review"}))


def test_disabled_custom_node_id_ok(tmp_path: Path) -> None:
    # A non-legacy node id (impossible to disable under the old ``Stage`` vocabulary) can be
    # disabled when it exists in the flow and its skip-outcome routes to a forward edge.
    snap = _snap(_CUSTOM_REVIEW_FLOW, tmp_path)
    validate_disabled_nodes(snap, frozenset({"code_review"}))


def test_disabled_terminal_node_ok(tmp_path: Path) -> None:
    # A terminal node (no outgoing edges) needs no forward edge — skipping it ends the flow DONE.
    snap = _snap(_STRANDED_ROUTER_FLOW, tmp_path)
    validate_disabled_nodes(snap, frozenset({"out"}))


def test_disabled_unknown_node_raises(tmp_path: Path) -> None:
    snap = _snap(_CUSTOM_REVIEW_FLOW, tmp_path)
    with pytest.raises(FlowValidationError) as exc:
        validate_disabled_nodes(snap, frozenset({"ghost"}))
    vs = exc.value.violations
    assert _has(vs, "graph", "ghost")
    assert _has(vs, "graph", "code_review")  # the message lists the flow's real node ids


def test_disabled_stranded_skip_outcome_raises(tmp_path: Path) -> None:
    snap = _snap(_STRANDED_ROUTER_FLOW, tmp_path)
    with pytest.raises(FlowValidationError) as exc:
        validate_disabled_nodes(snap, frozenset({"router"}))
    assert _has(exc.value.violations, "graph", "skip-outcome")


# -- read-only git evidence ----------------------------------------------------

_GIT_EVIDENCE_FLOW = """\
flow:
  name: t
  task_type: t
  permission_ceiling: {ceiling}
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: a
      kind: agent
      role_file: t/a.md
      {profile}
      git_evidence: true
    - id: b
      kind: publish
      policy: pull_request
  edges:
    - {{ from: a, to: b }}
"""


def test_git_evidence_accepted_on_a_read_only_node(tmp_path: Path) -> None:
    # The declaration is valid on its own: whether it actually grants anything is the operator's
    # security.allow_git_evidence to decide at run time, not the flow validator's.
    content = _GIT_EVIDENCE_FLOW.format(
        ceiling="workspace-write", profile="permission_profile: read-only"
    )
    validate_flow(_snap(content, tmp_path))


def test_git_evidence_accepted_under_a_read_only_ceiling(tmp_path: Path) -> None:
    # No per-node profile: the node inherits the flow's read-only ceiling, which is where the grant
    # applies, so the declaration is meaningful and accepted.
    content = """\
flow:
  name: t
  task_type: t
  permission_ceiling: read-only
  output_policy: private_control_workspace_report
  publishing: local_artifact
  nodes:
    - id: a
      kind: agent
      role_file: t/a.md
      git_evidence: true
    - id: b
      kind: publish
      policy: local_artifact
  edges:
    - { from: a, to: b }
"""
    validate_flow(_snap(content, tmp_path))


def test_git_evidence_rejected_on_a_workspace_write_node(tmp_path: Path) -> None:
    # A workspace-write node already has an unrestricted shell, so the field would do nothing there.
    # Rejected rather than ignored — a flag that silently does nothing reads as protection.
    content = _GIT_EVIDENCE_FLOW.format(
        ceiling="workspace-write", profile="permission_profile: workspace-write"
    )
    assert _has(_violations(content, tmp_path), "ceiling", "git_evidence applies only to a")


# -- continuation prompts (resume_role_file) ----------------------------------

_CONTINUATION_FLOW = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: p
      kind: agent
      role_file: t/p.md
    - id: a
      kind: agent
      role_file: t/a.md
      resume_role_file: {agent_resume}
      session_scope: {agent_scope}
    - id: r
      kind: evaluator
      role: review
      role_file: t/r.md
      resume_role_file: {eval_resume}
      session_scope: {eval_scope}
    - id: b
      kind: publish
      policy: pull_request
  edges:
    - {{ from: p, to: a }}
    - {{ from: a, to: r }}
    - {{ from: r, to: b, outcome: accept }}
    - {{ from: r, to: a, outcome: rework, budget: 2 }}
"""


def _continuation_flow(
    *,
    agent_scope: str = "editing_lineage",
    eval_scope: str = "resume_own_lineage",
    agent_resume: str = "t/a.continue.md",
    eval_resume: str = "t/r.continue.md",
) -> str:
    return _CONTINUATION_FLOW.format(
        agent_scope=agent_scope,
        eval_scope=eval_scope,
        agent_resume=agent_resume,
        eval_resume=eval_resume,
    )


def test_continuation_prompt_accepted_on_the_scopes_that_resume(tmp_path: Path) -> None:
    # editing_lineage for an author, resume_own_lineage for an evaluator: the two scopes where a
    # session actually survives to be continued.
    validate_flow(_snap(_continuation_flow(), tmp_path))


def test_continuation_prompt_rejected_where_nothing_resumes(tmp_path: Path) -> None:
    # A field that silently does nothing reads as protection, so it is refused rather than ignored.
    vs = _violations(_continuation_flow(agent_scope="fresh_disposable"), tmp_path)
    assert _has(vs, "ceiling", "agent 'a': resume_role_file requires session_scope editing_lineage")

    vs = _violations(_continuation_flow(eval_scope="fresh_disposable"), tmp_path)
    assert _has(
        vs, "ceiling", "evaluator 'r': resume_role_file requires session_scope resume_own_lineage"
    )


def test_continuation_prompt_rejected_on_an_agents_own_lineage(tmp_path: Path) -> None:
    # An author resumes across node runs only through the editing lineage — resume_own_lineage
    # hands its runner no session, so the second prompt would be near-dead weight.
    vs = _violations(_continuation_flow(agent_scope="resume_own_lineage"), tmp_path)
    assert _has(vs, "ceiling", "agent 'a': resume_role_file requires session_scope editing_lineage")


def test_continuation_prompt_path_traversal_is_fatal(tmp_path: Path) -> None:
    # Same containment as any role file, and the message names the field the operator wrote.
    vs = _violations(_continuation_flow(agent_resume="../escape.md"), tmp_path)
    assert _has(vs, "ceiling", "node 'a': resume_role_file '../escape.md' contains path traversal")

    vs = _violations(_continuation_flow(eval_resume="/etc/passwd"), tmp_path)
    assert _has(vs, "ceiling", "node 'r': resume_role_file '/etc/passwd' contains path traversal")
