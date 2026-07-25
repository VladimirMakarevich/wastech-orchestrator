"""Task model: id-regex accept/reject cases, tri-state flags, the clean-task schema (PRE.3)."""

from __future__ import annotations

import pytest

from wastech_orchestrator.task.model import (
    ALLOWED_TASK_KEYS,
    REQUIRED_TASK_FIELDS,
    NodeOverride,
    NormalizedTask,
    is_valid_branch_name,
    is_valid_task_id,
)


@pytest.mark.parametrize(
    "task_id",
    ["task-001", "a", "0", "task.1_2-3", "a" * 64, "com", "con2"],
)
def test_valid_task_ids(task_id: str) -> None:
    assert is_valid_task_id(task_id)


@pytest.mark.parametrize(
    "task_id",
    [
        "",  # empty
        "Task-001",  # uppercase
        "-task",  # leading separator
        ".task",  # leading dot
        "_task",  # leading underscore
        "task 001",  # whitespace
        "task/01",  # illegal char
        "tÉst",  # non-ascii
        "a" * 65,  # too long
        "task.",  # trailing dot (Windows strips it → a different on-disk name)
        "con",  # Windows device name
        "nul.txt",  # device stem + extension
        "com1",  # serial-port device
        "lpt9",  # printer device
    ],
)
def test_invalid_task_ids(task_id: str) -> None:
    assert not is_valid_task_id(task_id)


@pytest.mark.parametrize(
    "branch_name",
    [
        "feature/ABC-123-add-login",
        "customer/jira/PROJ-42",
        "release/2026.06",
        "fix_login",
    ],
)
def test_valid_branch_names(branch_name: str) -> None:
    assert is_valid_branch_name(branch_name)


@pytest.mark.parametrize(
    "branch_name",
    [
        "",
        " main",
        "feature/has space",
        "-bad",
        "feature/-bad",
        "/feature/x",
        "feature/x/",
        "feature//x",
        "feature/..",
        "feature/x.lock",
        "feature/x.",
        "feature@{x}",
        "refs/heads/feature",
        "HEAD",
        "feature:bad",
    ],
)
def test_invalid_branch_names(branch_name: str) -> None:
    assert not is_valid_branch_name(branch_name)


def test_auto_merge_is_tri_state() -> None:
    assert NormalizedTask(id="t", title="x", description="d").auto_merge is None
    assert NormalizedTask(id="t", title="x", description="d", auto_merge=True).auto_merge is True
    assert NormalizedTask(id="t", title="x", description="d", auto_merge=False).auto_merge is False


def test_prompt_audit_is_tri_state() -> None:
    assert NormalizedTask(id="t", title="x", description="d").prompt_audit is None
    t = NormalizedTask(id="t", title="x", description="d", prompt_audit=True)
    assert t.prompt_audit is True


def test_default_collections_are_independent() -> None:
    a = NormalizedTask(id="a", title="A", description="d")
    b = NormalizedTask(id="b", title="B", description="d")
    a.contacts.append("ops")
    a.node_overrides["planning"] = NodeOverride(enabled=False)
    assert b.contacts == [] and b.node_overrides == {}


def test_schema_constants() -> None:
    # A clean task carries only identity/dispatch (``task_type`` selects the flow) + the sanctioned
    # task-wins gates (PRE.3): ``nodes.<node-id>.enabled`` (disable), ``auto_merge``,
    # ``prompt_audit``, ``decomposition``, and ``trust_level``. No provider/model/reasoning/refined.
    assert {
        "id",
        "title",
        "task_type",
        "branch_name",
        "branch_mode",
        "branch_ref",
        "publish",
        "auto_merge",
        "prompt_audit",
        "decomposition",
        "trust_level",
        "contacts",
        "depends_on",
        "priority",
        "queue",
        "subtasks",
        "nodes",
    } == ALLOWED_TASK_KEYS
    assert {"id", "title"} == REQUIRED_TASK_FIELDS
    assert REQUIRED_TASK_FIELDS <= ALLOWED_TASK_KEYS


def test_disabled_nodes_reflects_enabled_false_only() -> None:
    task = NormalizedTask(
        id="t",
        title="x",
        description="d",
        node_overrides={
            "review": NodeOverride(enabled=False),
            "testing": NodeOverride(enabled=True),
            "planning": NodeOverride(),  # unset → default (runs)
        },
    )
    assert task.disabled_nodes() == frozenset({"review"})


def test_node_override_fields_default_to_none() -> None:
    ov = NodeOverride()
    assert ov.enabled is None
    assert ov.model is None
    assert ov.reasoning is None
    assert ov.provider is None


def test_node_override_carries_model_reasoning_provider() -> None:
    ov = NodeOverride(model="claude-opus-5", reasoning="high", provider="claude")
    assert (ov.model, ov.reasoning, ov.provider) == ("claude-opus-5", "high", "claude")
    # An override that only sets model/reasoning/provider never disables the node.
    assert ov.enabled is None


def test_disabled_nodes_ignores_override_only_nodes() -> None:
    # A node carrying a model/reasoning/provider override (but no ``enabled: false``) still runs.
    task = NormalizedTask(
        id="t",
        title="x",
        description="d",
        node_overrides={
            "implementation": NodeOverride(model="x", reasoning="high"),
            "review": NodeOverride(enabled=False, provider="codex"),
        },
    )
    assert task.disabled_nodes() == frozenset({"review"})
