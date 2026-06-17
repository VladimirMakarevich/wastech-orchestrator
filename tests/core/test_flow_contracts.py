"""Unit tests for the provider-neutral flow execution vocabulary (flow-engine P0.1)."""

from __future__ import annotations

from wastech_orchestrator.core.flow.contracts import (
    QUALITY_ACTION_EFFECT,
    EvaluationKind,
    EvaluatorRole,
    ExecutionUnit,
    LifecycleEffect,
    OutputPolicy,
    PermissionProfile,
    PublishingPolicy,
    QualityAction,
    RunKind,
    SessionScope,
    fingerprint,
    quality_action_effect,
)
from wastech_orchestrator.core.state_machine import (
    ACTIVE,
    ALLOWED_TRANSITIONS,
    Status,
    can_transition,
)


def test_quality_action_effect_is_total() -> None:
    for action in QualityAction:
        assert action in QUALITY_ACTION_EFFECT
        assert quality_action_effect(action) is QUALITY_ACTION_EFFECT[action]


def test_quality_action_lifecycle_mapping() -> None:
    # The exact foundation mapping (each action -> canonical state-machine behavior).
    eff = quality_action_effect
    assert eff(QualityAction.CONTINUE) == LifecycleEffect("advance")
    assert eff(QualityAction.REPEAT_STAGE) == LifecycleEffect("reenter_same")
    assert eff(QualityAction.ENTER_FIXING) == LifecycleEffect("goto", Status.FIXING)
    assert eff(QualityAction.STOP_MANUAL) == LifecycleEffect("goto", Status.MANUAL_ACTION_REQUIRED)
    assert eff(QualityAction.FAIL) == LifecycleEffect("goto", Status.FAILED)


def test_goto_targets_are_real_and_reachable() -> None:
    # FIXING is a real status reachable in the current machine (e.g. from testing). The
    # implementing -> fixing edge that enter_fixing models is added by the supervisor program (P2).
    assert can_transition(Status.TESTING, Status.FIXING)
    # stop_manual / fail are universal bailouts from every active status.
    for active in ACTIVE:
        assert can_transition(active, Status.MANUAL_ACTION_REQUIRED)
        assert can_transition(active, Status.FAILED)
    # every goto target is a real status present in the transition table.
    for effect in QUALITY_ACTION_EFFECT.values():
        if effect.kind == "goto":
            assert effect.target is not None
            assert effect.target in ALLOWED_TRANSITIONS


def test_non_goto_effects_have_no_target() -> None:
    assert quality_action_effect(QualityAction.CONTINUE).target is None
    assert quality_action_effect(QualityAction.REPEAT_STAGE).target is None


def test_lifecycle_effect_is_hashable_and_value_equal() -> None:
    # frozen dataclasses are hashable and compare by value.
    assert LifecycleEffect("advance") == LifecycleEffect("advance")
    assert LifecycleEffect("goto", Status.FIXING) != LifecycleEffect("goto", Status.FAILED)
    assert {LifecycleEffect("advance"), LifecycleEffect("advance")} == {LifecycleEffect("advance")}


def test_enum_values_are_yaml_safe() -> None:
    # Avoid YAML 1.1 boolean/null tokens as enum values; 'none' is a safe pyyaml string.
    trap = {"on", "off", "yes", "no", "true", "false", "null", "~"}
    enums = (
        RunKind,
        EvaluatorRole,
        EvaluationKind,
        SessionScope,
        PermissionProfile,
        OutputPolicy,
        PublishingPolicy,
        QualityAction,
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
    assert EvaluationKind.FINAL_HANDOFF == "final_handoff"


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
