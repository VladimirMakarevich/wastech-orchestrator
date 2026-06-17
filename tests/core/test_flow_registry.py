"""Unit tests for the flow registry (flow-engine P0.4).

Covers: built-in resolution, default task_type, unknown raises, operator priority,
fallback to packaged, task_type mismatch, and validate_flow integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.core.flow.registry import (
    DEFAULT_TASK_TYPE,
    FlowRegistry,
    FlowResolutionError,
)
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.flow.validator import FlowValidationError

# -- built-in resolution ------------------------------------------------------


def test_resolve_none_defaults_to_implementation() -> None:
    snap = FlowRegistry().resolve(None)
    assert snap.doc.task_type == "implementation"


def test_resolve_implementation_explicit() -> None:
    snap = FlowRegistry().resolve("implementation")
    assert snap.doc.name == "implementation"
    assert snap.doc.task_type == "implementation"


def test_resolve_deep_research() -> None:
    snap = FlowRegistry().resolve("deep_research")
    assert snap.doc.task_type == "deep_research"


def test_resolve_security_audit() -> None:
    snap = FlowRegistry().resolve("security_audit")
    assert snap.doc.task_type == "security_audit"


def test_resolve_all_builtins_produce_snapshots() -> None:
    registry = FlowRegistry()
    for name in ("implementation", "deep_research", "security_audit"):
        snap = registry.resolve(name)
        assert isinstance(snap, FlowSnapshot)
        assert snap.doc.task_type == name


# -- default and unknown ------------------------------------------------------


def test_default_task_type_constant() -> None:
    assert DEFAULT_TASK_TYPE == "implementation"


def test_resolve_unknown_raises_resolution_error() -> None:
    with pytest.raises(FlowResolutionError) as exc_info:
        FlowRegistry().resolve("no_such_flow")
    assert "no_such_flow" in str(exc_info.value)


def test_resolution_error_message_lists_builtins() -> None:
    with pytest.raises(FlowResolutionError) as exc_info:
        FlowRegistry().resolve("unknown")
    msg = str(exc_info.value)
    assert "implementation" in msg


# -- snapshot properties ------------------------------------------------------


def test_returned_snapshot_has_fingerprint() -> None:
    snap = FlowRegistry().resolve("implementation")
    assert len(snap.flow_fingerprint) == 64
    assert all(c in "0123456789abcdef" for c in snap.flow_fingerprint)


def test_returned_snapshot_is_immutable() -> None:
    snap = FlowRegistry().resolve("implementation")
    with pytest.raises(AttributeError):
        snap.flow_fingerprint = "changed"  # type: ignore[misc]


def test_returned_snapshot_source_path_set() -> None:
    snap = FlowRegistry().resolve("implementation")
    assert snap.source_path is not None
    assert snap.source_path.name == "implementation.yaml"


# -- operator flow priority ---------------------------------------------------

_MINIMAL_YAML = """\
flow:
  name: implementation
  task_type: implementation
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: work
      kind: agent
      role_file: roles/work.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: work, to: out }
"""


def test_operator_flow_takes_priority_over_builtin(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "implementation.yaml").write_text(_MINIMAL_YAML)
    snap = FlowRegistry(operator_flows_dir=flows_dir).resolve("implementation")
    # Operator flow has 2 nodes; packaged implementation has 11.
    assert len(snap.doc.nodes) == 2


def test_operator_dir_no_matching_file_falls_back_to_packaged(tmp_path: Path) -> None:
    # Operator dir exists but has no implementation.yaml → falls back to packaged.
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    snap = FlowRegistry(operator_flows_dir=flows_dir).resolve("implementation")
    assert len(snap.doc.nodes) == 11  # packaged implementation


def test_no_operator_dir_uses_packaged_only() -> None:
    snap = FlowRegistry(operator_flows_dir=None).resolve("implementation")
    assert snap.doc.task_type == "implementation"


def test_operator_custom_task_type_resolved(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    custom_yaml = """\
flow:
  name: my_flow
  task_type: my_flow
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: work
      kind: agent
      role_file: roles/work.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: work, to: out }
"""
    (flows_dir / "my_flow.yaml").write_text(custom_yaml)
    snap = FlowRegistry(operator_flows_dir=flows_dir).resolve("my_flow")
    assert snap.doc.task_type == "my_flow"


# -- task_type mismatch guard -------------------------------------------------


def test_task_type_mismatch_raises_resolution_error(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    # File named implementation.yaml but YAML declares a different task_type.
    mismatch_yaml = """\
flow:
  name: other
  task_type: other
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: work
      kind: agent
      role_file: roles/work.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: work, to: out }
"""
    (flows_dir / "implementation.yaml").write_text(mismatch_yaml)
    with pytest.raises(FlowResolutionError) as exc_info:
        FlowRegistry(operator_flows_dir=flows_dir).resolve("implementation")
    msg = str(exc_info.value)
    assert "mismatch" in msg
    assert "implementation" in msg
    assert "other" in msg


# -- validate_flow is called --------------------------------------------------


def test_invalid_operator_flow_raises_validation_error(tmp_path: Path) -> None:
    # Operator flow that passes load_flow but fails validate_flow (no terminal node).
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    invalid_yaml = """\
flow:
  name: bad
  task_type: bad
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  budgets:
    loop: 5
  nodes:
    - id: a
      kind: evaluator
      role: review
      role_file: roles/a.md
    - id: b
      kind: agent
      role_file: roles/b.md
  edges:
    - { from: a, to: b, outcome: rework, loop: loop }
    - { from: b, to: a }
"""
    (flows_dir / "bad.yaml").write_text(invalid_yaml)
    # FlowValidationError (not FlowResolutionError) for a structurally invalid flow.
    with pytest.raises(FlowValidationError):
        FlowRegistry(operator_flows_dir=flows_dir).resolve("bad")
