"""Unit tests for the fatal load-time flow validator.

Each test covers exactly one violation class so regressions are easy to localise. "Valid flow"
tests confirm that all three co-design flows pass the validator without violations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.flow.snapshot import load_flow
from wastech_orchestrator.core.flow.validator import (
    FlowValidationError,
    Violation,
    validate_disabled_nodes,
    validate_flow,
    validate_flow_against_config,
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


# -- report_dir: the path rule ------------------------------------------------
#
# ``flow.report_dir`` names the one directory a report flow's writing node is told to write into,
# so it is validated as a path-identity value. Each refusal below is one rule of the path rule; the
# accepted cases pin what must stay ordinary (a leading-dot directory outside the reserved roots).


def _report_flow(value: str | None, *, policy: str = "private_control_workspace_report") -> str:
    line = "" if value is None else f"  report_dir: '{value}'\n"
    return f"""\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: {policy}
  publishing: none
{line}  nodes:
    - id: work
      kind: agent
      role_file: t/work.md
    - id: out
      kind: publish
      policy: none
  edges:
    - {{ from: work, to: out }}
"""


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        (".worc/x", "must not overlap the orchestrator's control/private home"),
        (".worc", "must not overlap the orchestrator's control/private home"),
        (".WORC/x", "must not overlap the orchestrator's control/private home"),
        (".worc-io/x", "must not overlap the agent exchange"),
        (".git/x", "must not overlap git's own directory"),
        ("/abs", "must be repo-relative — an absolute path is not allowed"),
        ("C:/x", "must be repo-relative — a drive letter is not a repo-relative path"),
        ("C:\\x", "must use '/' separators"),
        ("a/../b", "must not contain a '..' segment"),
        ("a/./b", "must not contain a '.' segment"),
        ("a\\b", "must use '/' separators"),
        ("con/x", "segment 'con' is a reserved Windows device name"),
        ("docs/COM1", "segment 'COM1' is a reserved Windows device name"),
        ("docs/a:b", "segment 'a:b' is not a portable path segment"),
        ("docs/adr ", "must not carry leading or trailing whitespace"),
        ("docs/adr/", "must not contain an empty path segment"),
        ("docs//adr", "must not contain an empty path segment"),
        ("  ", "must be a non-empty repo-relative POSIX directory"),
    ],
)
def test_report_dir_refusals(value: str, rule: str, tmp_path: Path) -> None:
    vs = _violations(_report_flow(value), tmp_path)
    assert _has(vs, "ceiling", "report_dir"), vs  # the message always names the key…
    assert _has(vs, "ceiling", rule), vs  # …and the rule it broke


@pytest.mark.parametrize("policy", ["private_control_workspace_report", "repository_document"])
def test_report_dir_accepted_for_both_report_policies(policy: str, tmp_path: Path) -> None:
    # A leading-dot directory outside the reserved roots is an ordinary base — including for the
    # private policy, which is deliberately not refused an override: the "a private report never
    # enters git" invariant is carried at run time, in two places that together cover every
    # terminal — the publish node refuses a report with any git-trackable file, and the resolved
    # private directory is dropped from the code commit's staging set.
    validate_flow(_snap(_report_flow(".worc-connect/triage", policy=policy), tmp_path))
    validate_flow(_snap(_report_flow("docs/adr", policy=policy), tmp_path))


def test_report_dir_absent_is_valid(tmp_path: Path) -> None:
    validate_flow(_snap(_report_flow(None), tmp_path))


def test_report_dir_on_code_change_is_refused(tmp_path: Path) -> None:
    # code_change resolves no report directory, so the key would be accepted and inert.
    vs = _violations(_report_flow("docs/adr", policy="code_change"), tmp_path)
    assert _has(vs, "ceiling", "output_policy 'code_change' resolves no report directory")


def test_report_dir_variable_in_a_code_change_prompt_is_refused(tmp_path: Path) -> None:
    # It would render EMPTY there — turning "write {report_dir}/report.md" into "/report.md".
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "work.md").write_text("write {report_dir}/report.md", encoding="utf-8")
    vs = _violations(_report_flow(None, policy="code_change"), tmp_path)
    assert _has(vs, "ceiling", "node 'work': role prompt references {report_dir}")


def test_report_dir_conditional_block_in_a_code_change_prompt_is_refused(tmp_path: Path) -> None:
    # The conditional form drops cleanly, but a block that can never be kept is dead prose whose
    # author believes it works — refused on the same reasoning as an inert field.
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "work.md").write_text("{?report_dir}see {report_dir}{/report_dir}", "utf-8")
    vs = _violations(_report_flow(None, policy="code_change"), tmp_path)
    assert _has(vs, "ceiling", "node 'work': role prompt references {report_dir}")


def test_report_dir_variable_is_fine_in_a_report_flow_prompt(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "work.md").write_text("write {report_dir}/report.md", encoding="utf-8")
    validate_flow(_snap(_report_flow(".worc-connect/triage"), tmp_path))


# -- report_dir: the configured task lifecycle tree (config-aware layer) -------


def _config(tmp_path: Path, *, tasks_dir: str | None = None) -> OrchestratorConfig:
    paths = "" if tasks_dir is None else f"paths:\n  tasks_dir: {tasks_dir!r}\n"
    return loads_config(f"""
repo:
  url: "git@example.com:o/r.git"
  local_path: {str(tmp_path)!r}
  base_branch: "main"
  branch_prefix: "worc"
agents:
  allowed: [claude]
  max_fix_cycles: 3
  max_total_fix_iterations: 5
  decomposition:
    enabled: false
  providers:
    claude:
      command: "claude"
      permission_profile: workspace-write
      primary: true
security:
  strict_isolation: true
  allowed_environment:
    - PATH
checks:
  commands: []
  timeout_seconds: 30
git:
  create_pull_request: true
  pr_base: "main"
{paths}""").config


def _config_violations(value: str, tmp_path: Path, **kwargs: str) -> list[Violation]:
    snap = _snap(_report_flow(value), tmp_path)
    validate_flow(snap)  # the path rule itself passes; the lifecycle tree is config-aware
    with pytest.raises(FlowValidationError) as exc_info:
        validate_flow_against_config(snap, _config(tmp_path, **kwargs))
    return exc_info.value.violations


def test_report_dir_under_the_default_tasks_dir_is_refused(tmp_path: Path) -> None:
    vs = _config_violations("tasks/x", tmp_path)
    assert _has(vs, "config", "report_dir 'tasks/x': must not overlap the task lifecycle tree")


def test_report_dir_under_a_renamed_tasks_dir_is_refused(tmp_path: Path) -> None:
    # The reserved root is the CONFIGURED tree, which only this layer can see.
    vs = _config_violations("work/queue/reports", tmp_path, tasks_dir="work/queue")
    assert _has(vs, "config", "must not overlap the task lifecycle tree")


@pytest.mark.parametrize("tasks_dir", ["tasks", "./tasks", "tasks/.", "tasks/", " tasks "])
def test_report_dir_under_an_untidily_spelled_tasks_dir_is_refused(
    tasks_dir: str, tmp_path: Path
) -> None:
    # The config validator's safety rule accepts every one of these spellings, and they all name
    # the directory `tasks` resolves to. The overlap test compares the normalized root, so an
    # untidy-but-legal config cannot buy a flow write access to the real lifecycle tree.
    vs = _config_violations("tasks/x", tmp_path, tasks_dir=tasks_dir)
    assert _has(vs, "config", "report_dir 'tasks/x': must not overlap the task lifecycle tree")


def test_report_dir_containing_the_tasks_dir_is_refused(tmp_path: Path) -> None:
    # The other direction: the lifecycle tree must not end up inside the flow's deliverable.
    vs = _config_violations("work", tmp_path, tasks_dir="work/queue")
    assert _has(vs, "config", "must not overlap the task lifecycle tree")


def test_report_dir_beside_a_renamed_tasks_dir_is_accepted(tmp_path: Path) -> None:
    # 'tasks' is not intrinsically reserved — the operator's configured tree is.
    snap = _snap(_report_flow("tasks/reports"), tmp_path)
    validate_flow(snap)
    validate_flow_against_config(snap, _config(tmp_path, tasks_dir="work/queue"))
