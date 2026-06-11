"""Unit tests for the task parser (§5, §19.3, §10)."""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.providers.base import ProviderId, Stage
from wastech_orchestrator.task.model import NormalizedTask
from wastech_orchestrator.task.parser import (
    extract_section,
    read_task_source,
    slugify,
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


def test_write_normalized(tmp_path: Path) -> None:
    task = NormalizedTask(
        id="task-001",
        title="T",
        description="Do it",
        agents={Stage.REVIEW: ProviderId.CODEX},
        contacts=["@lead"],
    )
    path = write_normalized(task, tmp_path)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["id"] == "task-001"
    assert data["agents"] == {"review": "codex"}
    assert data["contacts"] == ["@lead"]
    assert path.endswith("task.normalized.json")
