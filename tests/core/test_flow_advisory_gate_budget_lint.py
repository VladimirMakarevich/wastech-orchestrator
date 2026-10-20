"""The non-fatal lint for a non-blocking evaluator whose rework loop is tighter than its own budget.

A non-blocking gate accepts with ⚠️ on whichever limit comes first — ``max_rework_per_stage`` or
the budget of the loop its ``rework`` edge charges. ``journey_enrich`` declared both as 2, and with
the loop counted in verdicts rather than fix rounds the loop won after a single round and parked
the task. Both now count fix rounds, so equal numbers agree; the lint fires only when the loop is
the smaller one, which is the case where ``max_rework_per_stage`` is partly dead.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.flow.snapshot import FlowSnapshot, load_flow
from wastech_orchestrator.core.flow.validator import lint_advisory_gate_budgets

_FLOW = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  budgets:
    gate_fix: {budget}
  nodes:
    - id: work
      kind: agent
      role_file: t/work.md
    - id: gate
      kind: evaluator
      role: verifier
      role_file: t/work.md
      blocking: {blocking}
      max_rework_per_stage: {per_stage}
    - id: fix
      kind: agent
      role_file: t/work.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - {{ from: work, to: gate }}
    - {{ from: gate, to: out, outcome: accept }}
    - {{ from: gate, to: fix, outcome: rework, loop: gate_fix }}
    - {{ from: fix, to: gate }}
"""


def _load(tmp_path: Path, *, budget: int, per_stage: int, blocking: str = "false") -> FlowSnapshot:
    flow_dir = tmp_path / "flows"
    (flow_dir / "t").mkdir(parents=True)
    (flow_dir / "t.yaml").write_text(
        _FLOW.format(budget=budget, per_stage=per_stage, blocking=blocking), encoding="utf-8"
    )
    (flow_dir / "t" / "work.md").write_text("do the work", encoding="utf-8")
    return load_flow(flow_dir / "t.yaml")


def test_a_loop_tighter_than_max_rework_per_stage_is_named(tmp_path: Path) -> None:
    warnings = lint_advisory_gate_budgets(_load(tmp_path, budget=1, per_stage=2))
    assert [(w.node_id, w.loop, w.rounds, w.max_rework_per_stage) for w in warnings] == [
        ("gate", "gate_fix", 1, 2)
    ]


def test_equal_budgets_agree_and_say_nothing(tmp_path: Path) -> None:
    # The journey_enrich shape: both 2. A budget of N is N fix rounds on either side.
    assert lint_advisory_gate_budgets(_load(tmp_path, budget=2, per_stage=2)) == []


def test_the_config_ceiling_counts_when_it_is_the_tighter_number(tmp_path: Path) -> None:
    snapshot = _load(tmp_path, budget=5, per_stage=3)
    assert lint_advisory_gate_budgets(snapshot) == []
    clamped = lint_advisory_gate_budgets(snapshot, max_fix_cycles=2)
    assert [(w.loop, w.rounds) for w in clamped] == [("gate_fix", 2)]


def test_a_blocking_gate_is_not_linted(tmp_path: Path) -> None:
    # `max_rework_per_stage` is a non-blocking-only knob; a blocking gate is bounded by its loop.
    assert lint_advisory_gate_budgets(_load(tmp_path, budget=1, per_stage=3, blocking="true")) == []
