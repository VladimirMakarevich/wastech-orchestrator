"""Unit tests for the flow YAML loader and snapshot resolver (flow-engine P0.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.core.flow.contracts import (
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
    SessionScope,
)
from wastech_orchestrator.core.flow.schema import (
    AgentNode,
    ChecksNode,
    EvaluatorNode,
    PublishNode,
    WhenPredicate,
)
from wastech_orchestrator.core.flow.snapshot import FlowLoadError, FlowSnapshot, load_flow

CODESIGN = (
    Path(__file__).parent.parent.parent
    / "src"
    / "wastech_orchestrator"
    / "core"
    / "flow"
    / "packaged"
)


@pytest.fixture(scope="module")
def impl_snap() -> FlowSnapshot:
    return load_flow(CODESIGN / "implementation.yaml")


@pytest.fixture(scope="module")
def audit_snap() -> FlowSnapshot:
    return load_flow(CODESIGN / "security_audit.yaml")


# -- basic loading ------------------------------------------------------------


def test_load_implementation_yaml(impl_snap: FlowSnapshot) -> None:
    doc = impl_snap.doc
    assert doc.name == "implementation"
    assert doc.task_type == "implementation"
    assert doc.permission_ceiling == PermissionProfile.WORKSPACE_WRITE
    assert doc.output_policy == OutputPolicy.CODE_CHANGE
    assert doc.publishing == PublishingPolicy.PULL_REQUEST
    assert len(doc.nodes) == 7
    assert len(doc.edges) == 8


def test_load_deep_research_yaml() -> None:
    snap = load_flow(CODESIGN / "deep_research.yaml")
    assert snap.doc.name == "deep_research"
    assert snap.doc.publishing == PublishingPolicy.DOCUMENTATION_PULL_REQUEST


def test_load_security_audit_yaml(audit_snap: FlowSnapshot) -> None:
    assert audit_snap.doc.name == "security_audit"
    assert audit_snap.doc.publishing == PublishingPolicy.NONE
    assert audit_snap.doc.network_policy == "advisories"


# -- fingerprint --------------------------------------------------------------


def test_fingerprint_is_stable(impl_snap: FlowSnapshot) -> None:
    snap2 = load_flow(CODESIGN / "implementation.yaml")
    assert impl_snap.flow_fingerprint == snap2.flow_fingerprint


def test_fingerprint_format(impl_snap: FlowSnapshot) -> None:
    fp = impl_snap.flow_fingerprint
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_differs_across_flows(
    impl_snap: FlowSnapshot, audit_snap: FlowSnapshot
) -> None:
    assert impl_snap.flow_fingerprint != audit_snap.flow_fingerprint


# -- nodes_by_id lookup -------------------------------------------------------


def test_nodes_by_id_all_reachable(impl_snap: FlowSnapshot) -> None:
    expected_ids = {
        "refinement",
        "planning",
        "implementation",
        "testing",
        "review",
        "fixing",
        "publish",
    }
    assert set(impl_snap.nodes_by_id.keys()) == expected_ids


def test_nodes_by_id_kinds(impl_snap: FlowSnapshot) -> None:
    assert isinstance(impl_snap.nodes_by_id["implementation"], AgentNode)
    assert isinstance(impl_snap.nodes_by_id["review"], EvaluatorNode)
    assert isinstance(impl_snap.nodes_by_id["testing"], ChecksNode)
    assert isinstance(impl_snap.nodes_by_id["publish"], PublishNode)


# -- adjacency ----------------------------------------------------------------


def test_adjacency_multi_outcome_node(impl_snap: FlowSnapshot) -> None:
    # review has two outgoing edges: accept → publish, rework → fixing
    edges = impl_snap.adjacency["review"]
    outcomes = {e.outcome for e in edges}
    assert outcomes == {"accept", "rework"}
    targets = {e.to for e in edges}
    assert targets == {"publish", "fixing"}


def test_adjacency_terminal_node_absent(impl_snap: FlowSnapshot) -> None:
    assert "publish" not in impl_snap.adjacency


# -- when predicate -----------------------------------------------------------


def test_when_fact_only_defaults_equals_true(impl_snap: FlowSnapshot) -> None:
    refinement = impl_snap.nodes_by_id["refinement"]
    assert isinstance(refinement, AgentNode)
    assert refinement.when == WhenPredicate(fact="derived.needs_refinement", equals=True)


def test_when_config_fact(impl_snap: FlowSnapshot) -> None:
    planning = impl_snap.nodes_by_id["planning"]
    assert isinstance(planning, AgentNode)
    assert planning.when == WhenPredicate(fact="config.planning_enabled", equals=True)


def test_no_when_is_none(impl_snap: FlowSnapshot) -> None:
    implementation = impl_snap.nodes_by_id["implementation"]
    assert isinstance(implementation, AgentNode)
    assert implementation.when is None


# -- defaults application -----------------------------------------------------


def test_evaluator_defaults_applied(tmp_path: Path) -> None:
    content = """
flow:
  name: test
  task_type: test
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  defaults:
    evaluator:
      session_scope: fresh_disposable
      permission_profile: read-only
      max_rework_per_stage: 5
  nodes:
    - id: work
      kind: agent
      role_file: roles/work.md
    - id: check
      kind: evaluator
      role: review
      role_file: roles/review.md
  edges:
    - { from: work, to: check }
"""
    p = tmp_path / "test.yaml"
    p.write_text(content)
    snap = load_flow(p)
    ev = snap.nodes_by_id["check"]
    assert isinstance(ev, EvaluatorNode)
    assert ev.session_scope == SessionScope.FRESH_DISPOSABLE
    assert ev.permission_profile == PermissionProfile.READ_ONLY
    assert ev.max_rework_per_stage == 5  # from defaults, not the built-in default of 1


def test_evaluator_node_overrides_default(tmp_path: Path) -> None:
    content = """
flow:
  name: test
  task_type: test
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  defaults:
    evaluator:
      max_rework_per_stage: 5
  nodes:
    - id: work
      kind: agent
      role_file: roles/work.md
    - id: check
      kind: evaluator
      role: review
      role_file: roles/review.md
      max_rework_per_stage: 2
  edges:
    - { from: work, to: check }
"""
    p = tmp_path / "test.yaml"
    p.write_text(content)
    snap = load_flow(p)
    ev = snap.nodes_by_id["check"]
    assert isinstance(ev, EvaluatorNode)
    assert ev.max_rework_per_stage == 2  # explicit node value wins over default


# -- specific node fields -----------------------------------------------------


def test_agent_node_editing_lineage(impl_snap: FlowSnapshot) -> None:
    impl = impl_snap.nodes_by_id["implementation"]
    assert isinstance(impl, AgentNode)
    assert impl.session_scope == SessionScope.EDITING_LINEAGE
    assert impl.permission_profile == PermissionProfile.WORKSPACE_WRITE


def test_agent_node_lineage_affinity(impl_snap: FlowSnapshot) -> None:
    fixing = impl_snap.nodes_by_id["fixing"]
    assert isinstance(fixing, AgentNode)
    assert fixing.lineage_affinity == "implementation"


def test_agent_node_hitl(impl_snap: FlowSnapshot) -> None:
    planning = impl_snap.nodes_by_id["planning"]
    assert isinstance(planning, AgentNode)
    assert planning.hitl is not None
    assert planning.hitl.allow_question is True
    assert planning.hitl.allow_approval is True


def test_checks_node_discovery(impl_snap: FlowSnapshot) -> None:
    testing = impl_snap.nodes_by_id["testing"]
    assert isinstance(testing, ChecksNode)
    assert testing.checker == "command_profile"
    assert testing.discovery is not None
    assert testing.discovery.mode == "auto"
    assert testing.discovery.approve_command_changes is True


# -- budgets and decomposition ------------------------------------------------


def test_budgets_accessible(impl_snap: FlowSnapshot) -> None:
    assert impl_snap.doc.budgets["global_fix_iterations"] == 30
    assert impl_snap.doc.budgets["test_fix"] == 15
    assert impl_snap.doc.budgets["review_fix"] == 15


def test_research_audit_global_budget_uses_reserved_key(audit_snap: FlowSnapshot) -> None:
    # #6: the research/audit global ceiling must use the reserved key the engine's _global_cap
    # reads (FlowRunState.GLOBAL_FIX_KEY), not the inert "global_revision_iterations" key.
    assert audit_snap.doc.budgets["global_fix_iterations"] == 8
    research = load_flow(CODESIGN / "deep_research.yaml")
    assert research.doc.budgets["global_fix_iterations"] == 12


def test_budgets_readonly(impl_snap: FlowSnapshot) -> None:
    with pytest.raises(TypeError):
        impl_snap.doc.budgets["new_budget"] = 1  # type: ignore[index]


def test_decomposition_parsed(impl_snap: FlowSnapshot) -> None:
    dec = impl_snap.doc.decomposition
    assert dec is not None
    assert dec.proposed_by == "planning"
    assert "implementation" in dec.sub_flow
    assert "fixing" in dec.sub_flow
    assert dec.shared_budget == "global_fix_iterations"


def test_no_decomposition_for_audit(audit_snap: FlowSnapshot) -> None:
    assert audit_snap.doc.decomposition is None


# -- error handling -----------------------------------------------------------


def test_missing_flow_key_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("name: not_a_flow\n")
    with pytest.raises(FlowLoadError, match="flow"):
        load_flow(p)


def test_missing_required_field_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        "flow:\n  name: x\n  task_type: x\n  permission_ceiling: workspace-write\n"
        "  output_policy: code_change\n  nodes:\n    - id: n\n      kind: agent\n"
        "      role_file: r.md\n"
    )
    with pytest.raises(FlowLoadError, match="publishing"):
        load_flow(p)


def test_unknown_node_kind_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(
        "flow:\n  name: x\n  task_type: x\n  permission_ceiling: workspace-write\n"
        "  output_policy: code_change\n  publishing: pull_request\n"
        "  nodes:\n    - id: n\n      kind: unknown_kind\n      role_file: r.md\n"
        "  edges: []\n"
    )
    with pytest.raises(FlowLoadError, match="unknown node kind"):
        load_flow(p)


# -- fail-closed hardening (P0.5) ---------------------------------------------


def _write(tmp_path: Path, flow_body: str) -> Path:
    """Write a flow YAML whose ``flow:`` body is *flow_body* (already indented)."""
    p = tmp_path / "f.yaml"
    p.write_text("flow:\n" + flow_body)
    return p


_VALID_BODY = """\
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: a
      kind: agent
      role_file: roles/a.md
  edges: []
"""


_PUB = "  publishing: pull_request\n"  # marker the negative tests anchor their mutation on
_RF = "      role_file: roles/a.md\n"


def test_valid_minimal_loads(tmp_path: Path) -> None:
    # Sanity: the body the negative tests mutate is itself valid.
    assert load_flow(_write(tmp_path, _VALID_BODY)).doc.name == "t"


def test_unknown_flow_field_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(_PUB, _PUB + "  bogus: 1\n")
    with pytest.raises(FlowLoadError, match=r"unknown field.*bogus.*fail-closed"):
        load_flow(_write(tmp_path, body))


def test_unknown_agent_node_field_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(_RF, _RF + "      surprise: x\n")
    with pytest.raises(FlowLoadError, match=r"unknown field.*surprise"):
        load_flow(_write(tmp_path, body))


def test_unknown_edge_field_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace("  edges: []\n", "  edges:\n    - { from: a, to: a, weight: 3 }\n")
    with pytest.raises(FlowLoadError, match=r"unknown field.*weight"):
        load_flow(_write(tmp_path, body))


def test_unknown_when_field_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(_RF, _RF + "      when: { fact: config.x, unless: true }\n")
    with pytest.raises(FlowLoadError, match=r"unknown field.*unless"):
        load_flow(_write(tmp_path, body))


def test_bare_when_fact_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(_RF, _RF + "      when: { fact: hybrid_testing }\n")
    with pytest.raises(FlowLoadError, match=r"when.fact.*namespaced"):
        load_flow(_write(tmp_path, body))


def test_namespaced_when_fact_accepted(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(_RF, _RF + "      when: { fact: derived.needs_refinement }\n")
    node = load_flow(_write(tmp_path, body)).nodes_by_id["a"]
    assert isinstance(node, AgentNode)
    assert node.when is not None and node.when.fact == "derived.needs_refinement"


def test_output_artifact_slot_accepted(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(_RF, _RF + "      output_artifact: plan\n")
    node = load_flow(_write(tmp_path, body)).nodes_by_id["a"]
    assert isinstance(node, AgentNode)
    assert node.output_artifact == "plan"


def test_invalid_output_artifact_slot_rejected(tmp_path: Path) -> None:
    # The slot vocabulary is core-fixed; an invented slot fails closed at load.
    body = _VALID_BODY.replace(_RF, _RF + "      output_artifact: bogus\n")
    with pytest.raises(FlowLoadError, match=r"invalid output_artifact.*bogus"):
        load_flow(_write(tmp_path, body))


def test_invalid_checker_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(
        "  edges: []\n",
        "    - { id: c, kind: checks, checker: rogue_scan }\n  edges:\n    - { from: a, to: c }\n",
    )
    with pytest.raises(FlowLoadError, match=r"invalid checker 'rogue_scan'"):
        load_flow(_write(tmp_path, body))


def test_invalid_network_policy_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(_PUB, _PUB + "  network_policy: firehose\n")
    with pytest.raises(FlowLoadError, match=r"invalid NetworkPolicy 'firehose'"):
        load_flow(_write(tmp_path, body))


def test_invalid_enum_wrapped_in_flow_load_error(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(_PUB, "  publishing: telepathy\n")
    with pytest.raises(FlowLoadError, match=r"invalid PublishingPolicy 'telepathy'"):
        load_flow(_write(tmp_path, body))


def test_unknown_evaluator_default_field_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(
        "  nodes:\n",
        "  defaults:\n    evaluator: { session_scope: fresh_disposable, bogus: 1 }\n  nodes:\n",
    )
    with pytest.raises(FlowLoadError, match=r"unknown field.*bogus.*defaults.evaluator"):
        load_flow(_write(tmp_path, body))


# -- immutability -------------------------------------------------------------


def test_snapshot_fields_are_frozen(impl_snap: FlowSnapshot) -> None:
    with pytest.raises(AttributeError):
        impl_snap.flow_fingerprint = "changed"  # type: ignore[misc]


def test_nodes_by_id_is_readonly(impl_snap: FlowSnapshot) -> None:
    node = impl_snap.nodes_by_id["refinement"]
    with pytest.raises(TypeError):
        impl_snap.nodes_by_id["injected"] = node  # type: ignore[index]


def test_source_path_recorded(impl_snap: FlowSnapshot) -> None:
    assert impl_snap.source_path is not None
    assert impl_snap.source_path.name == "implementation.yaml"
