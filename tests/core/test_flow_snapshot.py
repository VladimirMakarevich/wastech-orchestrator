"""Unit tests for the flow YAML loader and snapshot resolver."""

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
    Path(__file__).parent.parent.parent / "src" / "wastech_orchestrator" / "packaged" / "flows"
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
    assert len(doc.nodes) == 8  # + documentation (after review, before publish)
    assert len(doc.edges) == 9


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
        "documentation",
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
    # review has two outgoing edges: accept → documentation (then publish), rework → fixing
    edges = impl_snap.adjacency["review"]
    outcomes = {e.outcome for e in edges}
    assert outcomes == {"accept", "rework"}
    targets = {e.to for e in edges}
    assert targets == {"documentation", "fixing"}


def test_adjacency_terminal_node_absent(impl_snap: FlowSnapshot) -> None:
    assert "publish" not in impl_snap.adjacency


# -- when predicate -----------------------------------------------------------


def test_when_fact_only_defaults_equals_true(impl_snap: FlowSnapshot) -> None:
    refinement = impl_snap.nodes_by_id["refinement"]
    assert isinstance(refinement, AgentNode)
    assert refinement.when == WhenPredicate(fact="derived.needs_refinement", equals=True)


def test_when_config_fact() -> None:
    # A real ``config.`` when-fact still parses (the deep_research external-research gate). The
    # ``config.*_enabled`` per-stage facts were removed — per-task node disable is by node id now,
    # not a fact (see ``test_planning_has_no_when_fact``).
    research = load_flow(CODESIGN / "deep_research.yaml")
    external = research.nodes_by_id["external_research"]
    assert external.when == WhenPredicate(fact="config.external_research", equals=True)


def test_planning_has_no_when_fact(impl_snap: FlowSnapshot) -> None:
    # Per-task disable moved off the ``config.planning_enabled`` fact onto the node id, so the
    # packaged ``planning`` node no longer carries a ``when`` predicate.
    planning = impl_snap.nodes_by_id["planning"]
    assert isinstance(planning, AgentNode)
    assert planning.when is None


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


def _gate_flow(defaults_gate: str | None, node_gate: str | None) -> str:
    defaults = (
        f"  defaults:\n    evaluator:\n      gate_severity: {defaults_gate}\n"
        if defaults_gate
        else ""
    )
    node_line = f"      gate_severity: {node_gate}\n" if node_gate else ""
    return (
        "flow:\n"
        "  name: test\n"
        "  task_type: test\n"
        "  permission_ceiling: workspace-write\n"
        "  output_policy: code_change\n"
        "  publishing: pull_request\n"
        f"{defaults}"
        "  nodes:\n"
        "    - id: work\n"
        "      kind: agent\n"
        "      role_file: roles/work.md\n"
        "    - id: check\n"
        "      kind: evaluator\n"
        "      role: review\n"
        "      role_file: roles/review.md\n"
        f"{node_line}"
        "  edges:\n"
        "    - { from: work, to: check }\n"
    )


@pytest.mark.parametrize(
    ("flow_file", "evaluator_ids"),
    [
        ("deep_research.yaml", ("coverage_gate", "fact_verification", "critical_review")),
        ("security_audit.yaml", ("finding_verification",)),
        ("blog_article.yaml", ("tone_style",)),
        ("blog_article_revise.yaml", ("tone_style",)),
        ("content_chapter.yaml", ("story_critic",)),
        ("content_translate.yaml", ("en_critic",)),
    ],
)
def test_packaged_quality_flows_gate_on_medium(
    flow_file: str, evaluator_ids: tuple[str, ...]
) -> None:
    # A quality evaluator judges "is this GOOD ENOUGH", which it has no natural way to
    # express as high/critical, so at the built-in `high` gate its findings were recorded and then
    # dropped. Every packaged flow whose evaluators are quality lenses must pin `medium` — whether
    # via defaults.evaluator (deep_research) or per node.
    snap = load_flow(CODESIGN / flow_file)
    for node_id in evaluator_ids:
        ev = snap.nodes_by_id[node_id]
        assert isinstance(ev, EvaluatorNode)
        assert ev.gate_severity == "medium", f"{flow_file}:{node_id}"


@pytest.mark.parametrize("role_file", ["coverage.md", "verifier.md", "critic.md"])
def test_packaged_research_evaluator_prompts_state_the_verdict_mechanism(role_file: str) -> None:
    # `verifier.md` contained no occurrence of `verdict`, `accept` or `rework` —
    # the engine derived a verdict from severities the prompt never defined. Each research evaluator
    # prompt must state the *mechanism* instead of a threshold: the flow decides which severities
    # gate, findings are filed at their true severity, and a sub-threshold one still reaches the
    # operator. A prompt that restated the configured gate (`medium`) would go stale the moment the
    # YAML changed, which is why the number lives only in the flow.
    text = (CODESIGN / "deep_research" / role_file).read_text(encoding="utf-8").lower()
    assert "you do not author the verdict" in text
    assert "the flow decides which severities force another round" in text
    assert "carried to the operator" in text
    assert "gate_severity" not in text


def test_deep_research_declares_its_own_finalize_lens() -> None:
    # With no `supervisor:` block the finalize turn fell through to the built-in
    # code-flow lens ("grounded in the actual committed change") and, applied to a research
    # deliverable, produced a summary with two false claims. The flow must declare its own lens, the
    # file must exist, and that lens must forbid first-person verification claims — the summary is
    # written by a read-only observer that opened 9 files while claiming to have spot-checked more.
    snap = load_flow(CODESIGN / "deep_research.yaml")
    assert snap.doc.supervisor is not None
    role_file = snap.doc.supervisor.finalize_role_file
    assert role_file == "deep_research/summary.md"
    text = (CODESIGN / role_file).read_text(encoding="utf-8")
    assert "Claim nothing you did not do." in text
    assert "third person" in text
    assert "No number, verdict or count you were not given." in text
    # `emit_follow_ups` is a code-oriented capability; a research flow leaves it off and still gets
    # its evaluators' findings in the summary.
    assert snap.doc.supervisor.emit_follow_ups is False


def test_packaged_verifier_prompt_watches_for_omissions() -> None:
    # The watch-list was four variants of "the report claims too much", so a
    # conservatively written report was unfalsifiable against it and `fact_verification` returned
    # zero findings. It must now also look for what is *absent*, own the whole citation manifest
    # rather than the report's inline prose, and fetch an external source instead of recalling it.
    text = (CODESIGN / "deep_research" / "verifier.md").read_text(encoding="utf-8")
    assert "**under**-claiming" in text
    # The manifest still sets the scope rather than the report's inline prose — but the prompt no
    # longer names the file or its directory: the citation verdicts say which manifest they graded.
    assert "manifest_path" in text
    assert "url" in text and "fetched" in text  # an external source is retrieved, never recalled


@pytest.mark.parametrize("role_file", ["verifier.md", "critic.md"])
def test_packaged_research_evaluators_read_the_deliverable_by_channel(role_file: str) -> None:
    # Both prompts opened `{repo}/docs/research/{task_id}/report.md` — the engine's own path
    # convention, hand-copied into a role file, which an operator flow on a different output policy
    # would silently not have. The deliverable arrives on the node-output channel instead, which the
    # writing node's `output_file` points at the report rather than at its closing message.
    text = (CODESIGN / "deep_research" / role_file).read_text(encoding="utf-8")
    assert "{synthesis_path}" in text
    assert "docs/research" not in text
    synthesis = load_flow(CODESIGN / "deep_research.yaml").nodes_by_id["synthesis"]
    assert isinstance(synthesis, AgentNode)
    assert synthesis.output_file == "report.md"


def test_structuring_node_writes_nothing_and_hands_on_its_whole_blueprint() -> None:
    # The node was *instructed* to organize its notes inside the deliverable
    # directory, so a 295-line intermediate blueprint shipped in the pull request beside the
    # report — and the two documents disagreed about coverage. It must write nothing, which means
    # its own output has to carry the blueprint in full, read by the writing node from the
    # channel.
    text = (CODESIGN / "deep_research" / "architecture_design.md").read_text(encoding="utf-8")
    assert "Write no files at all" in text
    assert "docs/research" not in text  # no deliverable-path convention left to organize notes into
    synthesis = (CODESIGN / "deep_research" / "synthesis.md").read_text(encoding="utf-8")
    assert "{architecture_design_path}" in synthesis


def test_implementation_review_keeps_the_high_gate(impl_snap: FlowSnapshot) -> None:
    # The counterpart: `review` is a correctness lens on a *blocking* node, so it stays at the
    # built-in `high`. Lowering a blocking evaluator's gate changes its exhaustion landing to
    # manual_action_required, which this change does not make.
    ev = impl_snap.nodes_by_id["review"]
    assert isinstance(ev, EvaluatorNode)
    assert ev.gate_severity == "high"
    assert ev.blocking is True


def test_evaluator_gate_severity_defaults_to_high(tmp_path: Path) -> None:
    ev = load_flow(_write(tmp_path, _gate_flow(None, None))).nodes_by_id["check"]
    assert isinstance(ev, EvaluatorNode)
    assert ev.gate_severity == "high"  # built-in default = historical behavior


def test_evaluator_gate_severity_default_applied(tmp_path: Path) -> None:
    ev = load_flow(_write(tmp_path, _gate_flow("low", None))).nodes_by_id["check"]
    assert isinstance(ev, EvaluatorNode)
    assert ev.gate_severity == "low"  # from defaults.evaluator, not the built-in high


def test_evaluator_gate_severity_node_overrides_default(tmp_path: Path) -> None:
    ev = load_flow(_write(tmp_path, _gate_flow("low", "medium"))).nodes_by_id["check"]
    assert isinstance(ev, EvaluatorNode)
    assert ev.gate_severity == "medium"  # explicit node value wins over default


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


def test_checks_node_has_checker(impl_snap: FlowSnapshot) -> None:
    testing = impl_snap.nodes_by_id["testing"]
    assert isinstance(testing, ChecksNode)
    assert testing.checker == "command_profile"


def test_checks_node_manifest_defaults_to_sources_json() -> None:
    # A flow that declares nothing keeps today's behavior — the knob's absence is not a
    # silent failure mode.
    node = load_flow(CODESIGN / "deep_research.yaml").nodes_by_id["citation_check"]
    assert isinstance(node, ChecksNode)
    assert node.manifest == "sources.json"


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


# -- fail-closed hardening ----------------------------------------------------


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


def test_agent_node_id_colliding_with_reserved_prefix_rejected(tmp_path: Path) -> None:
    # An agent id equal to a reserved core-variable prefix would make {plan_path} ambiguous with the
    # fixed core variable — fatal at load.
    body = _VALID_BODY.replace("    - id: a\n", "    - id: plan\n")
    with pytest.raises(FlowLoadError, match=r"reserved core-variable prefix"):
        load_flow(_write(tmp_path, body))


def test_agent_node_id_subtask_prefix_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace("    - id: a\n", "    - id: subtask_extra\n")
    with pytest.raises(FlowLoadError, match=r"reserved core-variable prefix"):
        load_flow(_write(tmp_path, body))


def test_agent_node_id_near_reserved_name_accepted(tmp_path: Path) -> None:
    # Only exact reserved names (and the ``subtask`` prefix) collide; ``reviewer`` / ``planning``
    # are fine, so the packaged flows' agent ids keep loading.
    body = _VALID_BODY.replace("    - id: a\n", "    - id: reviewer\n")
    assert "reviewer" in load_flow(_write(tmp_path, body)).nodes_by_id


# -- portable node-id identities ----------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "Bad",  # uppercase
        "has.dot",  # a dot cannot appear in the {id_path} token or a portable segment
        "a/b",  # path separator
        "a\\b",  # backslash
        "..",  # traversal
        "node id",  # whitespace
        "-lead",  # leading separator
        "con",  # Windows device name
        "nul",  # Windows device name
        "com1",  # Windows device name
        "lpt9",  # Windows device name
        "x" * 65,  # too long
    ],
)
def test_agent_node_id_non_portable_rejected(tmp_path: Path, bad: str) -> None:
    # An id that is not a portable single segment / prompt token fails at load, host-independently,
    # before any lookup map, artifact directory, or DB run row is built. Reject, never sanitize.
    body = _VALID_BODY.replace("    - id: a\n", f"    - id: {bad}\n")
    with pytest.raises(FlowLoadError, match=r"portable identifier"):
        load_flow(_write(tmp_path, body))


def test_non_agent_node_id_also_validated(tmp_path: Path) -> None:
    # Every node kind is validated — not only the agent/tool kinds that expose {<id>_path}. A
    # device-named publish node id fails flow load exactly like an agent one.
    body = _VALID_BODY.replace(
        "  edges: []\n",
        "    - id: CON\n      kind: publish\n      policy: pull_request\n  edges: []\n",
    )
    with pytest.raises(FlowLoadError, match=r"publish node id .* portable identifier"):
        load_flow(_write(tmp_path, body))


def test_supervisor_block_parsed(tmp_path: Path) -> None:
    block = (
        "  supervisor:\n"
        "    role_file: roles/s.md\n"
        "    finalize_role_file: roles/f.md\n"
        "    emit_follow_ups: true\n"
    )
    sup = load_flow(_write(tmp_path, _VALID_BODY.replace(_PUB, _PUB + block))).doc.supervisor
    assert sup is not None
    assert sup.role_file == "roles/s.md"
    assert sup.finalize_role_file == "roles/f.md"
    assert sup.emit_follow_ups is True


def test_supervisor_block_absent_is_none(tmp_path: Path) -> None:
    assert load_flow(_write(tmp_path, _VALID_BODY)).doc.supervisor is None


def test_unknown_supervisor_field_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(_PUB, _PUB + "  supervisor:\n    bogus: 1\n")
    with pytest.raises(FlowLoadError, match=r"unknown field.*bogus.*supervisor"):
        load_flow(_write(tmp_path, body))


def test_agent_network_access_tristate(tmp_path: Path) -> None:
    # Tri-state parse: true → True, false → False, omitted → None (inherit the flow default).
    def _na(value: str) -> str:
        return _VALID_BODY.replace(_RF, _RF + f"      network_access: {value}\n")

    for body, expected in (
        (_na("true"), True),
        (_na("false"), False),
        (_VALID_BODY, None),  # omitted ⇒ inherit the flow default
    ):
        node = load_flow(_write(tmp_path, body)).nodes_by_id["a"]
        assert isinstance(node, AgentNode)
        assert node.network_access is expected


def test_evaluator_network_access_tristate(tmp_path: Path) -> None:
    def _ev_flow(na_line: str) -> str:
        return (
            "flow:\n"
            "  name: t\n"
            "  task_type: t\n"
            "  permission_ceiling: workspace-write\n"
            "  output_policy: code_change\n"
            "  publishing: pull_request\n"
            "  nodes:\n"
            "    - id: work\n"
            "      kind: agent\n"
            "      role_file: roles/work.md\n"
            "    - id: check\n"
            "      kind: evaluator\n"
            "      role: review\n"
            "      role_file: roles/review.md\n"
            f"{na_line}"
            "  edges:\n"
            "    - { from: work, to: check }\n"
        )

    for na_line, expected in (
        ("      network_access: true\n", True),
        ("      network_access: false\n", False),
        ("", None),  # omitted ⇒ inherit the flow default
    ):
        p = tmp_path / "ev.yaml"
        p.write_text(_ev_flow(na_line))
        ev = load_flow(p).nodes_by_id["check"]
        assert isinstance(ev, EvaluatorNode)
        assert ev.network_access is expected


def test_invalid_checker_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(
        "  edges: []\n",
        "    - { id: c, kind: checks, checker: rogue_scan }\n  edges:\n    - { from: a, to: c }\n",
    )
    with pytest.raises(FlowLoadError, match=r"invalid checker 'rogue_scan'"):
        load_flow(_write(tmp_path, body))


def _manifest_flow(manifest: str) -> str:
    return _VALID_BODY.replace(
        "  edges: []\n",
        f"    - {{ id: c, kind: checks, checker: citation, manifest: {manifest} }}\n"
        "  edges:\n    - { from: a, to: c }\n",
    )


def test_checks_manifest_is_parsed(tmp_path: Path) -> None:
    node = load_flow(_write(tmp_path, _manifest_flow("citations.json"))).nodes_by_id["c"]
    assert isinstance(node, ChecksNode)
    assert node.manifest == "citations.json"


@pytest.mark.parametrize("bad", ["../outside.json", "sub/dir.json", "'/abs.json'", "'..'", "'CON'"])
def test_invalid_checks_manifest_rejected(tmp_path: Path, bad: str) -> None:
    # A flow-authored filename resolved against the report dir is a traversal surface, so it goes
    # through the same portable-segment validator the exchange uses — reject, never sanitize.
    with pytest.raises(FlowLoadError, match=r"invalid 'manifest'"):
        load_flow(_write(tmp_path, _manifest_flow(bad)))


def _output_file_flow(value: str, *, slot: str = "") -> str:
    slot_line = f"      output_artifact: {slot}\n" if slot else ""
    return _VALID_BODY.replace(_RF, _RF + f"      output_file: {value}\n" + slot_line)


def test_agent_output_file_is_parsed(tmp_path: Path) -> None:
    node = load_flow(_write(tmp_path, _output_file_flow("overview.md"))).nodes_by_id["a"]
    assert isinstance(node, AgentNode)
    assert node.output_file == "overview.md"


@pytest.mark.parametrize("bad", ["../outside.md", "sub/dir.md", "'/abs.md'", "'..'", "'CON'"])
def test_invalid_agent_output_file_rejected(tmp_path: Path, bad: str) -> None:
    # Same reasoning as `manifest`: a flow-authored filename joined onto a directory the
    # orchestrator resolved is a traversal surface — reject, never sanitize.
    with pytest.raises(FlowLoadError, match=r"invalid 'output_file'"):
        load_flow(_write(tmp_path, _output_file_flow(bad)))


def test_output_file_with_output_artifact_rejected(tmp_path: Path) -> None:
    # A slot node's channel IS its slot, so the produced file would be written and never read —
    # exactly the silent-nothing failure mode that gets declared and then trusted.
    with pytest.raises(FlowLoadError, match=r"both 'output_file' and 'output_artifact'"):
        load_flow(_write(tmp_path, _output_file_flow("overview.md", slot="plan")))


def test_invalid_network_policy_rejected(tmp_path: Path) -> None:
    body = _VALID_BODY.replace(_PUB, _PUB + "  network_policy: firehose\n")
    with pytest.raises(FlowLoadError, match=r"invalid NetworkPolicy 'firehose'"):
        load_flow(_write(tmp_path, body))


def test_duplicate_node_id_rejected(tmp_path: Path) -> None:
    # A second node with the same id would silently collapse (last-wins) into one map entry; fail
    # closed instead so the shadowed node / its edges can never vanish unnoticed.
    body = _VALID_BODY.replace(
        "  edges: []\n",
        "    - { id: a, kind: agent, role_file: roles/a.md }\n  edges: []\n",
    )
    with pytest.raises(FlowLoadError, match=r"duplicate node id 'a'"):
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


def test_unknown_evaluator_node_field_rejected(tmp_path: Path) -> None:
    body = _gate_flow(None, None).replace(
        "      role_file: roles/review.md\n",
        "      role_file: roles/review.md\n      surprise: x\n",
    )
    with pytest.raises(FlowLoadError, match=r"unknown field.*surprise"):
        load_flow(_write(tmp_path, body))


def test_invalid_gate_severity_rejected(tmp_path: Path) -> None:
    with pytest.raises(FlowLoadError, match=r"invalid 'gate_severity'.*trivial"):
        load_flow(_write(tmp_path, _gate_flow(None, "trivial")))


def test_invalid_gate_severity_default_rejected(tmp_path: Path) -> None:
    with pytest.raises(FlowLoadError, match=r"invalid 'gate_severity'.*defaults.evaluator"):
        load_flow(_write(tmp_path, _gate_flow("bogus", None)))


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


def test_agent_git_evidence_tristate(tmp_path: Path) -> None:
    # Same tri-state parse as network_access: an omitted field must stay None rather than being
    # coerced to False, so "did not ask" and "explicitly declined" remain distinguishable.
    def _ge(value: str) -> str:
        return _VALID_BODY.replace(_RF, _RF + f"      git_evidence: {value}\n")

    for body, expected in ((_ge("true"), True), (_ge("false"), False), (_VALID_BODY, None)):
        node = load_flow(_write(tmp_path, body)).nodes_by_id["a"]
        assert isinstance(node, AgentNode)
        assert node.git_evidence is expected


def test_evaluator_git_evidence_tristate(tmp_path: Path) -> None:
    def _ev_flow(line: str) -> str:
        return (
            "flow:\n"
            "  name: t\n"
            "  task_type: t\n"
            "  permission_ceiling: workspace-write\n"
            "  output_policy: code_change\n"
            "  publishing: pull_request\n"
            "  nodes:\n"
            "    - id: work\n"
            "      kind: agent\n"
            "      role_file: roles/work.md\n"
            "    - id: check\n"
            "      kind: evaluator\n"
            "      role: review\n"
            "      role_file: roles/review.md\n"
            f"{line}"
            "  edges:\n"
            "    - { from: work, to: check }\n"
        )

    for line, expected in (
        ("      git_evidence: true\n", True),
        ("      git_evidence: false\n", False),
        ("", None),
    ):
        p = tmp_path / "ev_ge.yaml"
        p.write_text(_ev_flow(line))
        ev = load_flow(p).nodes_by_id["check"]
        assert isinstance(ev, EvaluatorNode)
        assert ev.git_evidence is expected
