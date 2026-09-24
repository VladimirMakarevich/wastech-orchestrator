"""Unit tests for the non-fatal tool-budget lint (the authoring-time half of the loop contract).

A ``tool`` node's declared fix budget means one thing when the tool emits a top-level ``findings``
array and another when it does not: with findings the number is a plan of fix rounds, without them
it is the number of byte-identical failures that ends the loop. Nothing in the flow can decide which
it will be — that is a property of the operator's own program — so the lint states the condition
once, where the number is written, and stays silent where no number was promised.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.flow.snapshot import load_flow
from wastech_orchestrator.core.flow.validator import lint_tool_loop_budgets

_FLOW = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  budgets:
{budgets}
  nodes:
    - id: work
      kind: agent
      role_file: t/work.md
    - id: gate
      kind: tool
      tool: check_form
    - id: fix
      kind: agent
      role_file: t/work.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - {{ from: work, to: gate }}
    - {{ from: gate, to: out, outcome: pass }}
    - {{ from: gate, to: fix, outcome: fail, {bound} }}
    - {{ from: fix, to: gate }}
"""


def _load(tmp_path: Path, *, budgets: str, bound: str):
    flow_dir = tmp_path / "flows"
    (flow_dir / "t").mkdir(parents=True)
    (flow_dir / "t.yaml").write_text(_FLOW.format(budgets=budgets, bound=bound), encoding="utf-8")
    (flow_dir / "t" / "work.md").write_text("do the work", encoding="utf-8")
    return load_flow(flow_dir / "t.yaml")


def test_lint_names_the_node_and_the_number_the_author_wrote(tmp_path: Path) -> None:
    snapshot = _load(tmp_path, budgets="    form_fix: 6", bound="loop: form_fix")
    warnings = lint_tool_loop_budgets(snapshot)
    assert [(w.node_id, w.budget) for w in warnings] == [("gate", 6)]


def test_lint_reads_an_inline_budget_as_the_same_statement(tmp_path: Path) -> None:
    # `budget: 4` on the edge and `loop:` + a `budgets` entry are two spellings of one promise.
    snapshot = _load(tmp_path, budgets="    unused: 1", bound="budget: 4")
    assert [(w.node_id, w.budget) for w in lint_tool_loop_budgets(snapshot)] == [("gate", 4)]


def test_lint_is_silent_when_no_number_was_promised(tmp_path: Path) -> None:
    # A named loop with no entry in `budgets` bounds the loop by the config cap alone; there is no
    # authored number to be read two ways, so there is nothing to warn about.
    snapshot = _load(tmp_path, budgets="    other: 3", bound="loop: form_fix")
    assert lint_tool_loop_budgets(snapshot) == []


def test_lint_ignores_a_budget_that_belongs_to_another_node_kind(tmp_path: Path) -> None:
    # An evaluator's loop ends on its own verdict, so its budget means exactly what it says.
    flow_dir = tmp_path / "flows"
    (flow_dir / "t").mkdir(parents=True)
    (flow_dir / "t.yaml").write_text(
        _FLOW.format(budgets="    form_fix: 6", bound="loop: form_fix").replace(
            "      kind: tool\n      tool: check_form\n",
            "      kind: evaluator\n      role: reviewer\n      role_file: t/work.md\n",
        ),
        encoding="utf-8",
    )
    (flow_dir / "t" / "work.md").write_text("judge the work", encoding="utf-8")
    assert lint_tool_loop_budgets(load_flow(flow_dir / "t.yaml")) == []
