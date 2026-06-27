"""Unit tests for the task parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wastech_orchestrator.task.model import NodeOverride, NormalizedTask
from wastech_orchestrator.task.parser import (
    extract_section,
    load_normalized,
    read_subtask_spec,
    read_task_source,
    slugify,
    slugify_bounded,
    split_frontmatter,
    write_normalized,
)

_MD = """---
id: task-001
title: "Add a thing"
refined: false
---

## Description

Do the thing.

## Acceptance criteria

- [ ] it works
"""


def test_read_task_source(tmp_path: Path) -> None:
    f = tmp_path / "task-001.md"
    f.write_bytes(_MD.encode("utf-8"))  # exact bytes (avoid Windows CRLF translation)
    source = read_task_source(f)
    assert source.suffix == ".md"
    assert source.raw_bytes == _MD.encode("utf-8")


def test_split_md_frontmatter_and_body() -> None:
    parse = split_frontmatter(_MD, ".md")
    assert parse.present and not parse.malformed
    assert parse.frontmatter["id"] == "task-001"
    assert parse.frontmatter["title"] == "Add a thing"
    assert "## Description" in parse.body


def test_md_missing_frontmatter() -> None:
    parse = split_frontmatter("just a body, no fence", ".md")
    assert parse.present is False
    assert parse.malformed is False


def test_md_unterminated_frontmatter_is_malformed() -> None:
    parse = split_frontmatter("---\nid: x\nno closing fence\n", ".md")
    assert parse.present is True
    assert parse.malformed is True


def test_md_duplicate_keys_is_malformed() -> None:
    text = "---\nid: a\nid: b\n---\n\nbody\n"
    parse = split_frontmatter(text, ".md")
    assert parse.malformed is True


def test_md_non_mapping_frontmatter_is_malformed() -> None:
    text = "---\n- just\n- a\n- list\n---\n\nbody\n"
    parse = split_frontmatter(text, ".md")
    assert parse.malformed is True


def test_json_object_splits_description_as_body() -> None:
    text = json.dumps({"id": "task-001", "title": "T", "description": "Do it"})
    parse = split_frontmatter(text, ".json")
    assert parse.present and not parse.malformed
    assert parse.frontmatter == {"id": "task-001", "title": "T"}
    assert parse.body == "Do it"


def test_json_non_object_is_missing() -> None:
    parse = split_frontmatter("[1, 2, 3]", ".json")
    assert parse.present is False
    assert parse.malformed is False


def test_json_parse_error_is_malformed() -> None:
    parse = split_frontmatter("{not json", ".json")
    assert parse.malformed is True


def test_json_duplicate_keys_is_malformed() -> None:
    parse = split_frontmatter('{"id": "a", "id": "b"}', ".json")
    assert parse.malformed is True


def test_extract_section() -> None:
    body = "## Description\n\nThe desc.\n\n## Acceptance criteria\n\n- one\n"
    assert extract_section(body, "Description") == "The desc."
    assert extract_section(body, "Acceptance criteria") == "- one"
    assert extract_section(body, "Constraints") is None


def test_slugify() -> None:
    assert slugify("Add login form validation") == "add-login-form-validation"
    assert slugify("  Trim & Fold!!  ") == "trim-fold"
    assert slugify("***") == "task"


def test_slugify_bounded() -> None:
    # Full fit returns the slug unchanged.
    assert slugify_bounded("Add login form", 50) == "add-login-form"
    # Truncated to the budget, and the dash the cut would leave is stripped.
    assert slugify_bounded("Add login form validation", 10) == "add-login"
    # A cut landing mid-word truncates without a trailing dash to strip.
    assert slugify_bounded("authentication", 4) == "auth"
    # Idempotent on an already-slugified input.
    assert slugify_bounded("add-login-form", 50) == "add-login-form"
    # No budget → empty (so the branch builder omits the slug segment entirely).
    assert slugify_bounded("anything", 0) == ""
    assert slugify_bounded("anything", -5) == ""


def test_write_normalized(tmp_path: Path) -> None:
    task = NormalizedTask(
        id="task-001",
        title="T",
        description="Do it",
        contacts=["@lead"],
        node_overrides={"review": NodeOverride(enabled=False)},
    )
    path = write_normalized(task, tmp_path)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["id"] == "task-001"
    assert data["contacts"] == ["@lead"]
    assert data["nodes"] == {"review": {"enabled": False}}
    assert path.endswith("task.normalized.json")


@pytest.mark.parametrize("value", ["deep_research", "security_audit", None])
def test_task_type_round_trips(tmp_path: Path, value: str | None) -> None:
    # Restart-safety: a resumed task must dispatch to the same flow it was created with — a lost
    # task_type would silently fall back to the implementation flow on recovery.
    task = NormalizedTask(id="task-001", title="T", description="Do it", task_type=value)
    write_normalized(task, tmp_path)
    assert load_normalized(tmp_path, "task-001").task_type == value


@pytest.mark.parametrize("value", [True, False, None])
def test_auto_merge_round_trips(tmp_path: Path, value: bool | None) -> None:
    # Restart-safety: the resumed task must carry the exact tri-state it was parsed with, or a
    # crash mid-publish could flip the auto-merge decision (e.g. a `false` opt-out → global true).
    task = NormalizedTask(id="task-001", title="T", description="Do it", auto_merge=value)
    write_normalized(task, tmp_path)
    assert load_normalized(tmp_path, "task-001").auto_merge is value


@pytest.mark.parametrize("value", [True, False, None])
def test_prompt_audit_round_trips(tmp_path: Path, value: bool | None) -> None:
    # Restart-safety: a resumed task must keep its exact prompt-audit tri-state across recovery.
    task = NormalizedTask(id="task-001", title="T", description="Do it", prompt_audit=value)
    write_normalized(task, tmp_path)
    assert load_normalized(tmp_path, "task-001").prompt_audit is value


@pytest.mark.parametrize("value", [True, False, None])
def test_decomposition_round_trips(tmp_path: Path, value: bool | None) -> None:
    # Restart-safety: a resumed task must keep its exact decomposition tri-state, or a crash before
    # the planning gate could flip whether a split is permitted on recovery.
    task = NormalizedTask(id="task-001", title="T", description="Do it", decomposition=value)
    write_normalized(task, tmp_path)
    assert load_normalized(tmp_path, "task-001").decomposition is value


@pytest.mark.parametrize("value", ["feature/ABC-123-custom", None])
def test_branch_name_round_trips(tmp_path: Path, value: str | None) -> None:
    # Restart-safety: a custom branch_name must survive a crash-resume, or a task resumed before
    # publish would silently fall back to the default branch naming policy.
    task = NormalizedTask(id="task-001", title="T", description="Do it", branch_name=value)
    write_normalized(task, tmp_path)
    assert load_normalized(tmp_path, "task-001").branch_name == value


@pytest.mark.parametrize("value", ["backend", "default"])
def test_queue_round_trips(tmp_path: Path, value: str) -> None:
    # Restart-safety: a resumed task must keep its queue tag, so the same instance still owns it.
    task = NormalizedTask(id="task-001", title="T", description="Do it", queue=value)
    write_normalized(task, tmp_path)
    assert load_normalized(tmp_path, "task-001").queue == value


def test_queue_absent_in_normalized_loads_default(tmp_path: Path) -> None:
    # A pre-queue normalized file (no `queue` key) loads as the default queue, never None/empty.
    task = NormalizedTask(id="task-001", title="T", description="Do it")
    path = write_normalized(task, tmp_path)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    del data["queue"]
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    assert load_normalized(tmp_path, "task-001").queue == "default"


def test_node_overrides_round_trip(tmp_path: Path) -> None:
    # Restart-safety: the per-node disable toggle and the model/reasoning/provider overrides must
    # survive a resume, or a crash could lose a disable (re-running a turned-off node) or silently
    # revert an override to the flow's declared default mid-task.
    task = NormalizedTask(
        id="task-001",
        title="T",
        description="Do it",
        node_overrides={
            "planning": NodeOverride(enabled=False),
            "implementation": NodeOverride(model="claude-opus-4-8", reasoning="high"),
            "testing": NodeOverride(enabled=False),
            "review": NodeOverride(provider="codex"),
        },
    )
    write_normalized(task, tmp_path)
    loaded = load_normalized(tmp_path, "task-001")
    assert loaded.node_overrides == task.node_overrides
    assert loaded.disabled_nodes() == frozenset({"planning", "testing"})


def test_node_overrides_absent_in_normalized_loads_empty(tmp_path: Path) -> None:
    task_dir = tmp_path / "logs" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "task.normalized.json").write_text(
        json.dumps({"id": "task-001", "title": "T", "description": "d"}), encoding="utf-8"
    )
    loaded = load_normalized(tmp_path, "task-001")
    assert loaded.node_overrides == {}


@pytest.mark.parametrize("value", [("task-002", "task-003"), ()])
def test_depends_on_round_trips(tmp_path: Path, value: tuple[str, ...]) -> None:
    # Restart-safety: a resumed task must preserve its exact depends_on tuple across recovery.
    task = NormalizedTask(id="task-001", title="T", description="Do it", depends_on=value)
    write_normalized(task, tmp_path)
    assert load_normalized(tmp_path, "task-001").depends_on == value


def test_depends_on_absent_in_legacy_normalized_loads_empty(tmp_path: Path) -> None:
    task_dir = tmp_path / "logs" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "task.normalized.json").write_text(
        json.dumps({"id": "task-001", "title": "T", "description": "d"}), encoding="utf-8"
    )
    assert load_normalized(tmp_path, "task-001").depends_on == ()


@pytest.mark.parametrize("value", ["low", "mid", "high"])
def test_priority_round_trips(tmp_path: Path, value: str) -> None:
    # Restart-safety: a resumed task must keep its scheduling priority across recovery.
    task = NormalizedTask(id="task-001", title="T", description="Do it", priority=value)
    write_normalized(task, tmp_path)
    assert load_normalized(tmp_path, "task-001").priority == value


def test_priority_absent_in_legacy_normalized_loads_mid(tmp_path: Path) -> None:
    task_dir = tmp_path / "logs" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "task.normalized.json").write_text(
        json.dumps({"id": "task-001", "title": "T", "description": "d"}), encoding="utf-8"
    )
    assert load_normalized(tmp_path, "task-001").priority == "mid"


@pytest.mark.parametrize("value", [("sub/01-a.md", "sub/02-b.md"), ()])
def test_subtasks_round_trips(tmp_path: Path, value: tuple[str, ...]) -> None:
    # Restart-safety: a resumed operator-decomposed task must preserve its subtasks references.
    task = NormalizedTask(id="task-001", title="T", description="Do it", subtasks=value)
    write_normalized(task, tmp_path)
    assert load_normalized(tmp_path, "task-001").subtasks == value


def test_read_subtask_spec_parses_title_slug_deps_and_body() -> None:
    text = (
        '---\ntitle: "Add the cart model"\nslug: cart\ndepends_on: ["payment"]\n---\n\n'
        "## Acceptance criteria\n\n- [ ] models exist\n- works\n"
    )
    spec = read_subtask_spec(text)
    assert spec is not None
    assert spec.title == "Add the cart model"
    assert spec.slug == "cart"
    assert spec.depends_on == ("payment",)
    assert spec.acceptance_criteria == ("models exist", "works")
    assert "## Acceptance criteria" in spec.body  # body kept verbatim (incl. leading blank line)


def test_read_subtask_spec_defaults_slug_from_title() -> None:
    spec = read_subtask_spec('---\ntitle: "Payment Step"\n---\n\nDo the payment step.\n')
    assert spec is not None
    assert spec.slug == "payment-step"
    assert spec.depends_on == ()


@pytest.mark.parametrize(
    "text",
    [
        "no front matter\n",  # missing front matter
        "---\ndepends_on: []\n---\n\nbody\n",  # missing title
        '---\ntitle: "T"\n---\n\n',  # empty body
        '---\ntitle: "T"\ndepends_on: "nope"\n---\n\nbody\n',  # depends_on not a list
    ],
)
def test_read_subtask_spec_malformed_returns_none(text: str) -> None:
    assert read_subtask_spec(text) is None


def test_auto_merge_absent_in_legacy_normalized_loads_as_none(tmp_path: Path) -> None:
    # A task.normalized.json written before this field existed must load as None (defer to config).
    task_dir = tmp_path / "logs" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "task.normalized.json").write_text(
        json.dumps({"id": "task-001", "title": "T", "description": "d"}), encoding="utf-8"
    )
    assert load_normalized(tmp_path, "task-001").auto_merge is None
