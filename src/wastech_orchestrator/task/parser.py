"""Task parser.

Reads a task file (``.md`` with leading ``---`` front matter + body, or ``.json`` object) into the
structural pieces the validation gate needs, and writes the normalized manifest
(``task.normalized.json``). This module is deliberately *structural only*: it splits front
matter from body and folds a slug — it does **not** decide validity (that is the gate's job, which
maps each failure to a machine-readable reason).

Front-matter parsing rejects duplicate keys (both YAML and JSON) so ``frontmatter_malformed`` can be
reported deterministically rather than silently taking the last value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Hashable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from wastech_orchestrator.config.schema import BranchMode, PublishScope
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.task.model import (
    DEFAULT_QUEUE,
    NodeOverride,
    NormalizedTask,
    normalize_priority,
)

# For a ``.json`` task the body lives in this reserved key (a ``.md`` task carries it as the body
# after the front matter). It is therefore not a front-matter field for either format.
JSON_BODY_KEY = "description"

NORMALIZED_FILENAME = "task.normalized.json"

_FRONTMATTER_FENCE = "---"


@dataclass(frozen=True)
class ParsedSource:
    """The raw bytes of a task file plus its format suffix. IO only — no validation."""

    path: str
    suffix: str  # ".md" | ".json"
    raw_bytes: bytes


@dataclass(frozen=True)
class FrontmatterParse:
    """The result of splitting front matter from body.

    ``present=False, malformed=False`` => missing; ``malformed=True`` => present but unparseable
    (parse error / not a mapping / duplicate keys); otherwise ``frontmatter``/``body`` are usable.
    """

    present: bool
    malformed: bool
    frontmatter: dict[str, Any]
    body: str
    detail: str = ""


def read_task_source(path: str | Path) -> ParsedSource:
    """Read a task file as raw bytes, recording its suffix. Decoding is the gate's concern."""
    p = Path(path)
    return ParsedSource(path=str(p), suffix=p.suffix.lower(), raw_bytes=p.read_bytes())


class _UniqueKeyLoader(yaml.SafeLoader):
    """A SafeLoader that raises on duplicate mapping keys (instead of silently overwriting)."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, Hashable):
            raise yaml.YAMLError(f"unhashable key: {key!r}")
        if key in mapping:
            raise yaml.YAMLError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _json_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key: {key!r}")
        seen[key] = value
    return seen


def split_frontmatter(text: str, suffix: str) -> FrontmatterParse:
    """Split a task file's front matter from its body, per format (``.md`` / ``.json``)."""
    if suffix == ".json":
        return _split_json(text)
    return _split_md(text)


def _split_md(text: str) -> FrontmatterParse:
    # A ``.md`` task must open with a ``---`` fence on the first line (no leading blank lines).
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        return FrontmatterParse(present=False, malformed=False, frontmatter={}, body=text)

    closing_index: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_FENCE:
            closing_index = i
            break
    if closing_index is None:
        return FrontmatterParse(
            present=True,
            malformed=True,
            frontmatter={},
            body="",
            detail="front matter has no closing '---' fence",
        )

    fm_text = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1 :])
    try:
        loaded = yaml.load(fm_text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        return FrontmatterParse(
            present=True,
            malformed=True,
            frontmatter={},
            body=body,
            detail=f"front matter YAML error: {exc}",
        )
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return FrontmatterParse(
            present=True,
            malformed=True,
            frontmatter={},
            body=body,
            detail="front matter is not a mapping",
        )
    return FrontmatterParse(
        present=True, malformed=False, frontmatter={str(k): v for k, v in loaded.items()}, body=body
    )


def _split_json(text: str) -> FrontmatterParse:
    try:
        loaded = json.loads(text, object_pairs_hook=_json_no_duplicates)
    except ValueError as exc:
        return FrontmatterParse(
            present=True,
            malformed=True,
            frontmatter={},
            body="",
            detail=f"JSON error: {exc}",
        )
    if not isinstance(loaded, dict):
        # Valid JSON but not an object => no front matter (``frontmatter_missing``).
        return FrontmatterParse(present=False, malformed=False, frontmatter={}, body="")
    # The reserved body key is split out so the front-matter key set is uniform across formats.
    body_value = loaded.get(JSON_BODY_KEY, "")
    body = body_value if isinstance(body_value, str) else ""
    frontmatter = {str(k): v for k, v in loaded.items() if k != JSON_BODY_KEY}
    return FrontmatterParse(present=True, malformed=False, frontmatter=frontmatter, body=body)


_SECTION_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def extract_section(body: str, header: str) -> str | None:
    """Return the text of the ``## <header>`` markdown section (case-insensitive), or None.

    The section runs from its heading to the next heading of any level (or end of file).
    """
    target = header.strip().lower()
    matches = list(_SECTION_RE.finditer(body))
    for index, match in enumerate(matches):
        if match.group(1).strip().lower() == target:
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            return body[start:end].strip()
    return None


_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Fold a title into a branch-safe slug: lowercase, non-alphanumerics → single ``-``.

    Deterministic and documented (the only normalization the gate tolerates for the slug). Returns
    ``"task"`` for an otherwise empty result so a branch name is always well-formed.
    """
    slug = _SLUG_STRIP_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "task"


def slugify_bounded(value: str, max_len: int) -> str:
    """``slugify`` then truncate to ``max_len`` chars, dropping any trailing dash left by the cut.

    Returns ``""`` (not ``"task"``) when ``max_len <= 0`` so the branch builder can omit the slug
    segment cleanly when the prefix already fills the budget. ``rstrip`` suffices: ``slugify`` never
    emits a leading dash, so truncation can only create a trailing one.
    """
    if max_len <= 0:
        return ""
    return slugify(value)[:max_len].rstrip("-")


def write_normalized(task: NormalizedTask, artifacts_root: str | Path) -> str:
    """Write ``task.normalized.json`` under ``logs/<task-id>/`` and return its path."""
    task_dir = task_artifact_dir(artifacts_root, task.id)
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / NORMALIZED_FILENAME
    data = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "task_type": task.task_type,
        "branch_name": task.branch_name,
        "branch_mode": task.branch_mode.value if task.branch_mode is not None else None,
        "branch_ref": task.branch_ref,
        "publish": task.publish.value if task.publish is not None else None,
        "auto_merge": task.auto_merge,
        "prompt_audit": task.prompt_audit,
        "decomposition": task.decomposition,
        "contacts": list(task.contacts),
        "depends_on": list(task.depends_on),
        "priority": task.priority,
        "queue": task.queue,
        "subtasks": list(task.subtasks),
        "nodes": {node_id: _node_override_json(ov) for node_id, ov in task.node_overrides.items()},
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)


def _opt_enum[E: StrEnum](value: Any, enum_cls: type[E]) -> E | None:
    """Read a persisted enum value back, tolerating ``None``/unknown (→ ``None`` = defer)."""
    if not isinstance(value, str):
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def _node_override_json(ov: NodeOverride) -> dict[str, Any]:
    """Serialize a :class:`NodeOverride`, omitting unset (``None``) fields."""
    out: dict[str, Any] = {}
    if ov.enabled is not None:
        out["enabled"] = ov.enabled
    if ov.model is not None:
        out["model"] = ov.model
    if ov.reasoning is not None:
        out["reasoning"] = ov.reasoning
    if ov.provider is not None:
        out["provider"] = ov.provider
    return out


def load_normalized(artifacts_root: str | Path, task_id: str) -> NormalizedTask:
    """Read back a ``task.normalized.json`` written by :func:`write_normalized` (recovery)."""
    path = task_artifact_dir(artifacts_root, task_id) / NORMALIZED_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    node_overrides = {
        str(node_id): NodeOverride(
            enabled=ov.get("enabled"),
            model=ov.get("model"),
            reasoning=ov.get("reasoning"),
            provider=ov.get("provider"),
        )
        for node_id, ov in (data.get("nodes") or {}).items()
    }
    return NormalizedTask(
        id=data["id"],
        title=data["title"],
        description=data.get("description", ""),
        task_type=data.get("task_type"),
        branch_name=data.get("branch_name"),
        branch_mode=_opt_enum(data.get("branch_mode"), BranchMode),
        branch_ref=data.get("branch_ref"),
        publish=_opt_enum(data.get("publish"), PublishScope),
        auto_merge=data.get("auto_merge"),
        prompt_audit=data.get("prompt_audit"),
        decomposition=data.get("decomposition"),
        contacts=list(data.get("contacts", [])),
        depends_on=tuple(data.get("depends_on", [])),
        priority=normalize_priority(data.get("priority")),
        queue=data.get("queue") or DEFAULT_QUEUE,
        subtasks=tuple(data.get("subtasks", [])),
        node_overrides=node_overrides,
    )


@dataclass(frozen=True)
class SubtaskSpecFile:
    """An operator-authored subtask spec file (front matter + verbatim body).

    A reduced manifest, deliberately *not* a standalone task (no ``id``). ``depends_on`` lists the
    **slugs** of earlier subtasks; ``body`` is the verbatim text after the front matter, written
    into the immutable ``NN-<slug>.md`` spec the edit nodes read as ``{subtask_spec_path}``.
    """

    title: str
    slug: str
    depends_on: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    body: str


def read_subtask_spec(text: str) -> SubtaskSpecFile | None:
    """Parse an operator subtask spec file; ``None`` when malformed.

    Malformed = no/invalid front matter, a missing/blank ``title``, a non-string ``slug``, a
    ``depends_on`` that is not a list of non-empty strings, or an empty body. ``slug`` defaults to
    ``slugify(title)``. Structural only — the orchestrator maps slugs→orders and runs the shared
    linear/range gate.
    """
    parse = split_frontmatter(text, ".md")
    if not parse.present or parse.malformed:
        return None
    fm = parse.frontmatter
    raw_title = fm.get("title")
    if not isinstance(raw_title, str) or not raw_title.strip():
        return None
    raw_slug = fm.get("slug")
    if raw_slug is not None and not isinstance(raw_slug, str):
        return None
    if isinstance(raw_slug, str) and raw_slug.strip():
        slug = slugify(raw_slug)
    else:
        slug = slugify(raw_title)
    raw_deps = fm.get("depends_on", [])
    if not isinstance(raw_deps, list | tuple) or not all(
        isinstance(d, str) and d.strip() for d in raw_deps
    ):
        return None
    if not parse.body.strip():
        return None
    ac_section = extract_section(parse.body, "Acceptance criteria")
    return SubtaskSpecFile(
        title=raw_title.strip(),
        slug=slug,
        depends_on=tuple(d.strip() for d in raw_deps),
        acceptance_criteria=_parse_criteria(ac_section) if ac_section else (),
        body=parse.body,
    )


def _parse_criteria(section: str) -> tuple[str, ...]:
    """Pull bullet items out of an ``## Acceptance criteria`` section (best-effort, audit only)."""
    items: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        for marker in ("- [ ] ", "- [x] ", "- ", "* "):
            if stripped.startswith(marker):
                items.append(stripped[len(marker) :].strip())
                break
    return tuple(item for item in items if item)
