"""Custom tool node — schema + loader + graph validation (P5.1).

Exercises the ``tool`` node kind at load/validate time: it parses as a first-class node, enforces a
flat-scalar ``args`` contract, rejects unknown fields fail-closed, guards its id against the
``{<id>_path}`` reserved-variable namespace, and confines its edge outcomes to ``pass`` / ``fail`` /
``route:*`` (the runtime outcome contract + config-aware tool-name resolution live in the runner and
registry tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.core.flow.schema import ToolNode
from wastech_orchestrator.core.flow.snapshot import FlowLoadError, load_flow
from wastech_orchestrator.core.flow.validator import (
    FlowValidationError,
    Violation,
    validate_flow,
)


def _tool_flow(
    *, node_extra: str = "", edges: str = "    - { from: md-check, to: out, outcome: pass }"
) -> str:
    return f"""\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: md-check
      kind: tool
      tool: md-check
{node_extra}    - id: out
      kind: publish
      policy: pull_request
  edges:
{edges}
"""


def _load(content: str, tmp_path: Path):  # type: ignore[return]
    p = tmp_path / "flow.yaml"
    p.write_text(content)
    return load_flow(p)


def _violations(content: str, tmp_path: Path) -> list[Violation]:
    with pytest.raises(FlowValidationError) as exc:
        validate_flow(_load(content, tmp_path))
    return exc.value.violations


def test_tool_node_parses_and_validates(tmp_path: Path) -> None:
    snap = _load(
        _tool_flow(node_extra="      args: { min_chars: 500, strict: true, ratio: 0.5 }\n"),
        tmp_path,
    )
    node = snap.nodes_by_id["md-check"]
    assert isinstance(node, ToolNode)
    assert node.kind == "tool"
    assert node.tool == "md-check"
    assert dict(node.args) == {"min_chars": 500, "strict": True, "ratio": 0.5}
    assert node.timeout_seconds is None  # unset → resolved to the config default at run time
    validate_flow(snap)  # graph + ceiling: a tool node with a pass edge is valid


def test_tool_timeout_seconds_parsed(tmp_path: Path) -> None:
    snap = _load(_tool_flow(node_extra="      timeout_seconds: 1800\n"), tmp_path)
    node = snap.nodes_by_id["md-check"]
    assert isinstance(node, ToolNode)
    assert node.timeout_seconds == 1800


def test_tool_args_must_be_flat_scalars(tmp_path: Path) -> None:
    for bad in ("      args: { nested: { a: 1 } }\n", "      args: { list: [1, 2] }\n"):
        with pytest.raises(FlowLoadError, match="scalar"):
            _load(_tool_flow(node_extra=bad), tmp_path)


def test_reject_unknown_tool_field(tmp_path: Path) -> None:
    with pytest.raises(FlowLoadError, match="unknown field"):
        _load(_tool_flow(node_extra="      checker: command_profile\n"), tmp_path)


def test_tool_id_cannot_shadow_reserved_var(tmp_path: Path) -> None:
    # A tool node exposes {<id>_path}, so its id obeys the same reserved-name guard as an agent id.
    content = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: plan
      kind: tool
      tool: md-check
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: plan, to: out, outcome: pass }
"""
    with pytest.raises(FlowLoadError, match="reserved core-variable"):
        _load(content, tmp_path)


def test_tool_outcome_subset(tmp_path: Path) -> None:
    # An edge outcome outside {pass, fail, route:*} on a tool node is a fatal graph violation.
    vs = _violations(
        _tool_flow(edges="    - { from: md-check, to: out, outcome: accept }"), tmp_path
    )
    assert any(v.category == "graph" and "not in allowed" in v.message for v in vs)


def test_tool_route_and_fail_outcomes_accepted(tmp_path: Path) -> None:
    # pass / fail / route:* are the allowed tool outcomes — a route edge validates cleanly.
    content = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: triage
      kind: tool
      tool: triage
    - id: small
      kind: publish
      policy: pull_request
    - id: large
      kind: publish
      policy: pull_request
  edges:
    - { from: triage, to: small, outcome: "route:small" }
    - { from: triage, to: large, outcome: "route:large" }
"""
    validate_flow(_load(content, tmp_path))  # no raise
