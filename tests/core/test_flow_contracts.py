"""Unit tests for the provider-neutral flow execution vocabulary."""

from __future__ import annotations

from wastech_orchestrator.core.flow.contracts import (
    EvaluatorRole,
    ExecutionUnit,
    NetworkPolicy,
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
    RunKind,
    SessionScope,
    fingerprint,
    resolve_network_access,
)


def test_enum_values_are_yaml_safe() -> None:
    # Avoid YAML 1.1 boolean/null tokens as enum values; 'none' is a safe pyyaml string.
    trap = {"on", "off", "yes", "no", "true", "false", "null", "~"}
    enums = (
        RunKind,
        EvaluatorRole,
        SessionScope,
        PermissionProfile,
        OutputPolicy,
        PublishingPolicy,
    )
    for enum in enums:
        for member in enum:
            assert member.value not in trap


def test_canonical_enum_values() -> None:
    assert RunKind.STAGE == "stage"
    assert RunKind.EVALUATOR == "evaluator"
    assert SessionScope.EDITING_LINEAGE == "editing_lineage"
    assert PermissionProfile.READ_ONLY == "read-only"
    assert PublishingPolicy.NONE == "none"
    assert EvaluatorRole.REVIEW == "review"


def test_execution_unit_root_vs_subtask() -> None:
    root = ExecutionUnit("task-123")
    assert root.is_root
    assert root.subtask_order is None
    sub = ExecutionUnit("task-123", 2)
    assert not sub.is_root
    assert sub.subtask_order == 2
    # frozen dataclass compares by value; the explicit None matches the default.
    assert ExecutionUnit("task-123") == ExecutionUnit("task-123", None)


def test_fingerprint_is_deterministic_and_key_order_independent() -> None:
    a = fingerprint({"name": "implementation", "nodes": [1, 2], "x": True})
    b = fingerprint({"x": True, "nodes": [1, 2], "name": "implementation"})
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)


def test_fingerprint_changes_with_payload() -> None:
    base = fingerprint({"name": "implementation"})
    assert base != fingerprint({"name": "deep_research"})
    assert base != fingerprint({"name": "implementation", "extra": 1})


# -- per-node network_access resolution ---------------------------------------


def test_resolve_network_access_node_grant_overrides_policyless_flow() -> None:
    # The operator's case: a node-level grant gives network even in a flow with no network_policy.
    assert resolve_network_access(True, None) is True


def test_resolve_network_access_node_optout_beats_granting_flow() -> None:
    # A node-level False is a real opt-out: offline even when the flow would otherwise grant it.
    assert resolve_network_access(False, NetworkPolicy.RESEARCH) is False


def test_resolve_network_access_inherits_granting_flow() -> None:
    assert resolve_network_access(None, NetworkPolicy.RESEARCH) is True


def test_resolve_network_access_inherits_policyless_flow() -> None:
    assert resolve_network_access(None, None) is False
