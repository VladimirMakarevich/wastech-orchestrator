"""Unit tests for the flow registry (flow-engine P0.4).

Covers: built-in resolution (from a delivered ``.worc/flows/``), default task_type, unknown/missing
raises (no packaged fallback), operator custom flows, task_type mismatch, and validate_flow
integration. Built-ins are resolved by pointing ``operator_flows_dir`` at the packaged flows tree —
what ``worc install`` copies into ``.worc/flows/`` — since the registry never reads the packaged
tree itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import BUILTIN_FLOWS_DIR

from wastech_orchestrator.core.flow.registry import (
    DEFAULT_TASK_TYPE,
    FlowRegistry,
    FlowResolutionError,
)
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.flow.validator import FlowValidationError

# -- built-in resolution (from a delivered .worc/flows/) ----------------------


def _builtin_registry() -> FlowRegistry:
    # A registry pointed at the packaged flows tree — what `worc install` delivers into
    # `.worc/flows/`. The registry itself has no packaged fallback, so built-ins resolve only
    # because they are present in the operator dir here.
    return FlowRegistry(operator_flows_dir=BUILTIN_FLOWS_DIR)


def test_resolve_none_defaults_to_implementation() -> None:
    snap = _builtin_registry().resolve(None)
    assert snap.doc.task_type == "implementation"


def test_resolve_implementation_explicit() -> None:
    snap = _builtin_registry().resolve("implementation")
    assert snap.doc.name == "implementation"
    assert snap.doc.task_type == "implementation"


def test_resolve_deep_research() -> None:
    snap = _builtin_registry().resolve("deep_research")
    assert snap.doc.task_type == "deep_research"


def test_resolve_security_audit() -> None:
    snap = _builtin_registry().resolve("security_audit")
    assert snap.doc.task_type == "security_audit"


def test_resolve_all_builtins_produce_snapshots() -> None:
    registry = _builtin_registry()
    for name in ("implementation", "deep_research", "security_audit"):
        snap = registry.resolve(name)
        assert isinstance(snap, FlowSnapshot)
        assert snap.doc.task_type == name


# -- default and unknown ------------------------------------------------------


def test_default_task_type_constant() -> None:
    assert DEFAULT_TASK_TYPE == "implementation"


def test_resolve_unknown_raises_resolution_error() -> None:
    with pytest.raises(FlowResolutionError) as exc_info:
        _builtin_registry().resolve("no_such_flow")
    assert "no_such_flow" in str(exc_info.value)


def test_resolution_error_message_lists_operator_flows(tmp_path: Path) -> None:
    # The "unknown task_type" hint lists the operator's own .worc/flows/ names (not packaged stems)
    # and points at the missing file + the fix.
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "custom.yaml").write_text(_MINIMAL_YAML.replace("implementation", "custom"))
    with pytest.raises(FlowResolutionError) as exc_info:
        FlowRegistry(operator_flows_dir=flows_dir).resolve("unknown")
    msg = str(exc_info.value)
    assert "unknown.yaml" in msg  # names the missing file
    assert "custom" in msg  # lists the operator's flows
    assert "worc install" in msg  # points at the fix


# -- snapshot properties ------------------------------------------------------


def test_returned_snapshot_has_fingerprint() -> None:
    snap = _builtin_registry().resolve("implementation")
    assert len(snap.flow_fingerprint) == 64
    assert all(c in "0123456789abcdef" for c in snap.flow_fingerprint)


def test_returned_snapshot_is_immutable() -> None:
    snap = _builtin_registry().resolve("implementation")
    with pytest.raises(AttributeError):
        snap.flow_fingerprint = "changed"  # type: ignore[misc]


def test_returned_snapshot_source_path_set() -> None:
    snap = _builtin_registry().resolve("implementation")
    assert snap.source_path is not None
    assert snap.source_path.name == "implementation.yaml"


# -- operator flow resolution -------------------------------------------------

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


def test_operator_flow_resolves_from_worc_flows(tmp_path: Path) -> None:
    # The operator's own .worc/flows/implementation.yaml is the sole source: its 2-node shape wins
    # (there is no packaged fallback to a different built-in shape).
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "implementation.yaml").write_text(_MINIMAL_YAML)
    snap = FlowRegistry(operator_flows_dir=flows_dir).resolve("implementation")
    assert len(snap.doc.nodes) == 2


def test_operator_dir_missing_flow_raises(tmp_path: Path) -> None:
    # Operator dir exists but has no implementation.yaml → hard error, NOT a silent packaged
    # fallback. `.worc/` is the whole truth; a missing flow is a real "not found".
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    with pytest.raises(FlowResolutionError) as exc_info:
        FlowRegistry(operator_flows_dir=flows_dir).resolve("implementation")
    assert "implementation.yaml" in str(exc_info.value)


def test_no_operator_dir_raises() -> None:
    # No operator layer at all → nothing resolves (the packaged tree is delivery-only, never read).
    with pytest.raises(FlowResolutionError):
        FlowRegistry(operator_flows_dir=None).resolve("implementation")


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


# -- P4.1: operator flows on the live path ------------------------------------


def test_operator_flow_resolves_and_executes(tmp_path: Path) -> None:
    # A custom task_type dropped in .worc/flows/ resolves to a validated, engine-drivable snapshot
    # (single entry, reachable terminal — proven by resolve() running the full validator). The
    # engine driving it end-to-end is covered by test_flow_engine_driver.py.
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "my_flow.yaml").write_text(_MINIMAL_YAML.replace("implementation", "my_flow"))
    snap = FlowRegistry(operator_flows_dir=flows_dir).resolve("my_flow")
    assert snap.doc.task_type == "my_flow"
    assert {n.id for n in snap.doc.nodes} == {"work", "out"}


# -- validate-flow: operator_flow_names + check_flows -------------------------


def test_operator_flow_names_lists_only_operator_flows(tmp_path: Path) -> None:
    # Discovery scope for `worc validate-flow`: only the operator's own `.worc/flows/`. Built-ins
    # are covered by the orchestrator's own test suite, not against a target repo's config.
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "custom.yaml").write_text(_MINIMAL_YAML.replace("implementation", "custom"))
    (flows_dir / "other.yaml").write_text(_MINIMAL_YAML.replace("implementation", "other"))
    names = FlowRegistry(operator_flows_dir=flows_dir).operator_flow_names()
    assert names == ["custom", "other"]  # sorted, no packaged built-ins
    assert "implementation" not in names


def test_operator_flow_names_empty_without_operator_dir() -> None:
    assert FlowRegistry().operator_flow_names() == []


def test_check_flows_reports_ok_for_valid_operator_flow(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "custom.yaml").write_text(_MINIMAL_YAML.replace("implementation", "custom"))
    checks = FlowRegistry(operator_flows_dir=flows_dir).check_flows(["custom"])
    assert len(checks) == 1
    assert checks[0].name == "custom"
    assert checks[0].error is None


def test_check_flows_flags_broken_flow_without_raising(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "broken.yaml").write_text("flow:\n  name: broken\n")  # malformed
    (flows_dir / "custom.yaml").write_text(_MINIMAL_YAML.replace("implementation", "custom"))
    results = FlowRegistry(operator_flows_dir=flows_dir).check_flows(["broken", "custom"])
    checks = {c.name: c for c in results}
    assert checks["broken"].error is not None  # an error string, not None (no raise)
    assert checks["custom"].error is None  # other flows unaffected
