"""Unit tests for the non-fatal prompt-variable anti-drift lint (prompt-authoring contract).

The lint scans each flow's role files for ``{name}`` / ``{?name}`` tokens outside the flow-derived
valid-set (core allowlist ∪ node-output names) and reports each as rendering verbatim. It is a
warning, never fatal — a verbatim render is the safe-renderer fallback (code/JSON braces must pass
through). The valid-set is a *function of the flow graph*, so it grows as nodes are added (the seam
the node-output channel extends without reworking the lint).
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.flow.prompt_vars import valid_prompt_vars
from wastech_orchestrator.core.flow.snapshot import load_flow
from wastech_orchestrator.core.flow.validator import lint_prompt_variables

PACKAGED = (
    Path(__file__).parent.parent.parent / "src" / "wastech_orchestrator" / "packaged" / "flows"
)

_FLOW = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: implement
      kind: agent
      role_file: t/implement.md
    - id: scan
      kind: agent
      role_file: t/scan.md
  edges:
    - { from: implement, to: scan }
  budgets: {}
"""


def _load(tmp_path: Path, implement_body: str, scan_body: str = "read {repo}"):
    flow_dir = tmp_path / "flows"
    (flow_dir / "t").mkdir(parents=True)
    (flow_dir / "t.yaml").write_text(_FLOW, encoding="utf-8")
    (flow_dir / "t" / "implement.md").write_text(implement_body, encoding="utf-8")
    (flow_dir / "t" / "scan.md").write_text(scan_body, encoding="utf-8")
    return load_flow(flow_dir / "t.yaml")


def test_lint_flags_typo_variable(tmp_path: Path) -> None:
    # A typo'd variable is outside the valid-set → warned (file + token), never raised.
    warnings = lint_prompt_variables(_load(tmp_path, "Base on the plan at {plna_path}."))
    assert [(w.role_file, w.token) for w in warnings] == [("t/implement.md", "plna_path")]


def test_lint_passes_known_vars_and_conditional_blocks(tmp_path: Path) -> None:
    # Core allowlisted vars, inside a {?...} block or bare, produce no warning.
    body = "task {task_id} {?memory_path}see {memory_path}{/memory_path} at {repo}"
    assert lint_prompt_variables(_load(tmp_path, body)) == []


def test_lint_valid_set_is_flow_derived(tmp_path: Path) -> None:
    # The valid-set is a function of the flow graph: an agent node's own {<id>_path} name is
    # reachable, so referencing a sibling node by id is NOT flagged, while an id that names no node
    # is. This is the seam the node-output channel extends with zero lint rework.
    warnings = lint_prompt_variables(_load(tmp_path, "use {scan_path} but not {ghost_path}"))
    assert [(w.role_file, w.token) for w in warnings] == [("t/implement.md", "ghost_path")]


def test_valid_prompt_vars_grows_with_nodes(tmp_path: Path) -> None:
    snap = _load(tmp_path, "x")
    allowed = valid_prompt_vars(snap)
    assert {"implement_path", "scan_path"} <= allowed  # node-derived names present
    assert "task_id" in allowed and "memory_path" in allowed  # core allowlist retained


_EVAL_FLOW = """\
flow:
  name: e
  task_type: e
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: scan
      kind: agent
      role_file: e/scan.md
    - id: judge
      kind: evaluator
      role: review
      role_file: e/judge.md
  edges:
    - { from: scan, to: judge }
  budgets: {}
"""


def test_lint_evaluator_may_read_a_node_output_var(tmp_path: Path) -> None:
    # An evaluator resolves the {<id>_path} channel too (it judges an upstream node's work,
    # so it must be able to open it), so {scan_path} in an evaluator role is NOT flagged — while an
    # id naming no node still is.
    flow_dir = tmp_path / "flows"
    (flow_dir / "e").mkdir(parents=True)
    (flow_dir / "e.yaml").write_text(_EVAL_FLOW, encoding="utf-8")
    (flow_dir / "e" / "scan.md").write_text("scan {task_id}", encoding="utf-8")
    (flow_dir / "e" / "judge.md").write_text("judge {scan_path} not {ghost_path}", encoding="utf-8")

    warnings = lint_prompt_variables(load_flow(flow_dir / "e.yaml"))
    assert [(w.role_file, w.token) for w in warnings] == [("e/judge.md", "ghost_path")]


_SUPERVISOR_FLOW = """\
flow:
  name: s
  task_type: s
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: implement
      kind: agent
      role_file: s/implement.md
  edges: []
  budgets: {}
  supervisor:
    role_file: s/observe.md
    finalize_role_file: s/summary.md
"""


def test_lint_scans_supervisor_prompts_against_their_own_tiny_allowlist(tmp_path: Path) -> None:
    # The flow-local supervisor prompts are role files too, but the supervisor populates only
    # {task_id, repo, repo_path}. A node-allowlist var ({plan_path}) in a supervisor prompt renders
    # verbatim just the same, so it is flagged; {task_id}/{repo} are clean.
    flow_dir = tmp_path / "flows"
    (flow_dir / "s").mkdir(parents=True)
    (flow_dir / "s.yaml").write_text(_SUPERVISOR_FLOW, encoding="utf-8")
    (flow_dir / "s" / "implement.md").write_text("do {task_id}", encoding="utf-8")
    (flow_dir / "s" / "observe.md").write_text("observe {task_id} in {repo}", encoding="utf-8")
    (flow_dir / "s" / "summary.md").write_text("summarize using {plan_path}", encoding="utf-8")

    warnings = lint_prompt_variables(load_flow(flow_dir / "s.yaml"))
    assert [(w.role_file, w.token) for w in warnings] == [("s/summary.md", "plan_path")]


def test_lint_no_source_path_returns_empty() -> None:
    # A unit-constructed snapshot (no on-disk role files) has nothing to scan.
    snap = load_flow(PACKAGED / "implementation.yaml")
    stripped = type(snap)(
        doc=snap.doc,
        nodes_by_id=snap.nodes_by_id,
        adjacency=snap.adjacency,
        flow_fingerprint=snap.flow_fingerprint,
        source_path=None,
    )
    assert lint_prompt_variables(stripped) == []


def test_packaged_flows_lint_clean() -> None:
    # The packaged prompts are the steady state: every token they use is in the valid-set, so the
    # lint is silent on them (its value is catching operator typos, not packaged drift).
    for yaml in sorted(PACKAGED.glob("*.yaml")):
        assert lint_prompt_variables(load_flow(yaml)) == [], yaml.name
