"""Task parser (spec §5, §19.3, §10).

Reads a task file (``.md`` with leading ``---`` front matter + body, or ``.json`` object) into the
structural pieces the §19 validation gate needs, and writes the normalized manifest
(``task.normalized.json``, §10). This module is deliberately *structural only*: it splits front
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
from pathlib import Path
from typing import Any

import yaml

from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.base import Stage
from wastech_orchestrator.task.model import NormalizedTask, StageParams

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
        loaded = yaml.load(fm_text, Loader=_UniqueKeyLoader)  # noqa: S506 - hardened loader
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
        # Valid JSON but not an object => no front matter (§19.2 ``frontmatter_missing``).
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
    """Fold a title into a branch-safe slug: lowercase, non-alphanumerics → single ``-`` (§19.5).

    Deterministic and documented (the only normalization the gate tolerates for the slug). Returns
    ``"task"`` for an otherwise empty result so a branch name is always well-formed.
    """
    slug = _SLUG_STRIP_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "task"


def write_normalized(task: NormalizedTask, artifacts_root: str | Path) -> str:
    """Write ``task.normalized.json`` under ``logs/<task-id>/`` and return its path (§10)."""
    task_dir = task_artifact_dir(artifacts_root, task.id)
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / NORMALIZED_FILENAME
    data = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "task_type": task.task_type,
        "pr_title": task.pr_title,
        "auto_merge": task.auto_merge,
        "prompt_audit": task.prompt_audit,
        "contacts": list(task.contacts),
        "stages": {stage.value: _stage_params_json(sp) for stage, sp in task.stage_params.items()},
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)


def _stage_params_json(sp: StageParams) -> dict[str, Any]:
    """Serialize a :class:`StageParams`, omitting unset (``None``) fields."""
    out: dict[str, Any] = {}
    if sp.enabled is not None:
        out["enabled"] = sp.enabled
    return out


def load_normalized(artifacts_root: str | Path, task_id: str) -> NormalizedTask:
    """Read back a ``task.normalized.json`` written by :func:`write_normalized` (recovery, §13)."""
    path = task_artifact_dir(artifacts_root, task_id) / NORMALIZED_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    stage_params = {
        Stage(stage): StageParams(enabled=sp.get("enabled"))
        for stage, sp in (data.get("stages") or {}).items()
    }
    return NormalizedTask(
        id=data["id"],
        title=data["title"],
        description=data.get("description", ""),
        task_type=data.get("task_type"),
        pr_title=data.get("pr_title"),
        auto_merge=data.get("auto_merge"),
        prompt_audit=data.get("prompt_audit"),
        contacts=list(data.get("contacts", [])),
        stage_params=stage_params,
    )
