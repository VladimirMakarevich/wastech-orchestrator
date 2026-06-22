"""Task decomposition: deterministic acceptance + artifacts.

Decomposition is a flag-gated sub-phase of ``planning``, **off by default**. The planning agent may
*recommend* a split in its structured output, but the Core decides whether to accept it by a
deterministic rule — the agent never gets to weaken ``max_subtasks``, routes, or security.

A split is accepted **only** when all hold:

* the gate is on for this task (config ``agents.decomposition.enabled`` — resolved by the caller and
  passed as ``gate_on``);
* the agent recommends a split with ``2 <= n <= max_subtasks``;
* every subtask declares ``order``, ``title``, ``slug``, ``acceptance_criteria`` and ``depends_on``;
* ``order`` values are exactly ``1..n`` and each ``depends_on`` references **only earlier orders**
  (linear; no forward or cyclic dependency).

Otherwise the task runs as a single unit. The accept/reject decision, ``n``, and the reason are
returned for the caller to persist and audit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wastech_orchestrator.providers.artifacts import task_artifact_dir

# Reason codes for the accept/reject decision (persisted/audited).
REASON_GATE_OFF = "gate_off"
REASON_NOT_RECOMMENDED = "not_recommended"
REASON_N_OUT_OF_RANGE = "n_out_of_range"
REASON_MALFORMED_SUBTASK = "malformed_subtask"
REASON_NON_LINEAR_DEPENDENCIES = "non_linear_dependencies"
REASON_ACCEPTED = "accepted"

# Subtask lifecycle status persisted in ``subtasks/index.json`` and the State Store.
SUBTASK_PENDING = "pending"
SUBTASK_IN_PROGRESS = "in_progress"
SUBTASK_COMMITTED = "committed"

INDEX_FILENAME = "index.json"


@dataclass(frozen=True)
class SubtaskSpec:
    """One subtask of an accepted decomposition."""

    order: int
    title: str
    slug: str
    acceptance_criteria: tuple[str, ...]
    depends_on: tuple[int, ...]


@dataclass(frozen=True)
class DecompositionDecision:
    """The Core's deterministic accept/reject decision for a planning split."""

    accepted: bool
    reason: str
    n: int
    subtasks: tuple[SubtaskSpec, ...] = ()


def _single_unit(reason: str) -> DecompositionDecision:
    return DecompositionDecision(accepted=False, reason=reason, n=1, subtasks=())


def _parse_subtask(raw: Any) -> SubtaskSpec | None:
    """Parse one subtask mapping defensively. Returns None if any required field is malformed."""
    if not isinstance(raw, Mapping):
        return None
    order = raw.get("order")
    title = raw.get("title")
    slug = raw.get("slug")
    acceptance = raw.get("acceptance_criteria")
    depends_on = raw.get("depends_on")

    # ``bool`` is a subclass of ``int`` — reject it explicitly for the integer ``order``.
    if not isinstance(order, int) or isinstance(order, bool):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(slug, str) or not slug.strip():
        return None
    if not isinstance(acceptance, Sequence) or isinstance(acceptance, str | bytes):
        return None
    if not acceptance or not all(isinstance(c, str) and c.strip() for c in acceptance):
        return None
    if not isinstance(depends_on, Sequence) or isinstance(depends_on, str | bytes):
        return None
    if not all(isinstance(d, int) and not isinstance(d, bool) for d in depends_on):
        return None

    return SubtaskSpec(
        order=order,
        title=title,
        slug=slug,
        acceptance_criteria=tuple(acceptance),
        depends_on=tuple(depends_on),
    )


def decide_decomposition(
    structured_output: Mapping[str, Any] | None,
    *,
    gate_on: bool,
    max_subtasks: int,
) -> DecompositionDecision:
    """Apply the deterministic acceptance rule to the planning agent's structured output."""
    if not gate_on:
        return _single_unit(REASON_GATE_OFF)
    if not isinstance(structured_output, Mapping):
        return _single_unit(REASON_NOT_RECOMMENDED)

    if structured_output.get("decompose") is not True:
        return _single_unit(REASON_NOT_RECOMMENDED)
    raw_subtasks = structured_output.get("subtasks")
    if not isinstance(raw_subtasks, Sequence) or isinstance(raw_subtasks, str | bytes):
        return _single_unit(REASON_NOT_RECOMMENDED)

    n = len(raw_subtasks)
    if n < 2 or n > max_subtasks:
        return _single_unit(REASON_N_OUT_OF_RANGE)

    specs: list[SubtaskSpec] = []
    for raw in raw_subtasks:
        spec = _parse_subtask(raw)
        if spec is None:
            return _single_unit(REASON_MALFORMED_SUBTASK)
        specs.append(spec)

    specs.sort(key=lambda s: s.order)
    # Orders must be exactly 1..n, and dependencies may reference only strictly-earlier orders
    # (linear; no forward or cyclic dependency).
    for index, spec in enumerate(specs, start=1):
        if spec.order != index:
            return _single_unit(REASON_NON_LINEAR_DEPENDENCIES)
        for dep in spec.depends_on:
            if dep < 1 or dep >= spec.order:
                return _single_unit(REASON_NON_LINEAR_DEPENDENCIES)

    return DecompositionDecision(accepted=True, reason=REASON_ACCEPTED, n=n, subtasks=tuple(specs))


def _subtasks_dir(artifacts_root: str | Path, task_id: str) -> Path:
    return task_artifact_dir(artifacts_root, task_id) / "subtasks"


def subtask_spec_path(artifacts_root: str | Path, task_id: str, order: int, slug: str) -> Path:
    """Path to one subtask's immutable ``NN-<slug>.md`` spec.

    The single source of the per-subtask spec filename, shared by :func:`write_subtask_artifacts`
    (which writes them) and the orchestrator's decomposition fan-out (which injects the active one
    as ``{subtask_spec_path}`` into the edit nodes).
    """
    return _subtasks_dir(artifacts_root, task_id) / f"{order:02d}-{slug}.md"


def _index_entry(spec: SubtaskSpec) -> dict[str, Any]:
    return {
        "order": spec.order,
        "slug": spec.slug,
        "title": spec.title,
        "depends_on": list(spec.depends_on),
        "status": SUBTASK_PENDING,
        "commit_sha": None,
    }


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON via a temp file + atomic replace so the index is never left half-written."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_subtask_artifacts(
    decision: DecompositionDecision, artifacts_root: str | Path, task_id: str
) -> None:
    """Write ``subtasks/index.json`` and one immutable ``NN-<slug>.md`` spec per subtask.

    All under ``logs/<task-id>/`` — never in the target repository. The per-subtask ``.md`` files
    are written once and never overwritten; ``index.json`` is updated transactionally as each
    subtask commits (see :func:`update_subtask_index`).
    """
    if not decision.accepted:
        return
    sub_dir = _subtasks_dir(artifacts_root, task_id)
    sub_dir.mkdir(parents=True, exist_ok=True)

    index = [_index_entry(spec) for spec in decision.subtasks]
    _write_json_atomic(sub_dir / INDEX_FILENAME, index)

    for spec in decision.subtasks:
        spec_path = subtask_spec_path(artifacts_root, task_id, spec.order, spec.slug)
        if spec_path.exists():
            continue  # immutable — never overwrite
        criteria = "\n".join(f"- {c}" for c in spec.acceptance_criteria)
        depends = ", ".join(str(d) for d in spec.depends_on) if spec.depends_on else "none"
        spec_path.write_text(
            f"# Subtask {spec.order:02d}: {spec.title}\n\n"
            f"slug: {spec.slug}\n"
            f"depends_on: {depends}\n\n"
            f"## Acceptance criteria\n\n{criteria}\n",
            encoding="utf-8",
        )


def update_subtask_index(
    artifacts_root: str | Path,
    task_id: str,
    order: int,
    *,
    status: str,
    commit_sha: str | None = None,
) -> None:
    """Update one subtask's ``status``/``commit_sha`` in ``index.json`` atomically.

    Mirrors the State Store update so the on-disk index and SQLite agree as each subtask commits.
    """
    index_path = _subtasks_dir(artifacts_root, task_id) / INDEX_FILENAME
    index = json.loads(index_path.read_text(encoding="utf-8"))
    for entry in index:
        if entry["order"] == order:
            entry["status"] = status
            if commit_sha is not None:
                entry["commit_sha"] = commit_sha
            break
    else:
        raise KeyError(f"subtask order {order} not found in {index_path}")
    _write_json_atomic(index_path, index)
