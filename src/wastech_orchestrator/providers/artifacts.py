"""Artifact writer (spec §10).

Writes the per-attempt artifacts under
``<artifacts_root>/logs/<task-id>/stages/<stage>/run-<stage-run-id>/<attempt>-<provider>/`` (with a
``sub-<NN>/`` level for a decomposed subtask). The directory layout and the **never overwrite**
rule live here; the *content* (already redacted) is supplied by the caller — this module imports
neither :mod:`~wastech_orchestrator.providers.redaction` nor any provider syntax.

Artifacts (§10): ``request.json`` (redacted), ``stdout.log``, ``stderr.log``, ``events.jsonl``,
``result.json``. ``before.diff`` / ``after.diff`` are stamped by the pipeline in P5.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wastech_orchestrator.providers.base import AgentRunResult, Stage

_CHECKSUM_CHUNK = 65536

REQUEST_FILENAME = "request.json"
STDOUT_FILENAME = "stdout.log"
STDERR_FILENAME = "stderr.log"
EVENTS_FILENAME = "events.jsonl"
RESULT_FILENAME = "result.json"


@dataclass(frozen=True)
class ArtifactPaths:
    """Absolute paths of the artifacts for one attempt. The directory exists; files may not yet."""

    attempt_dir: str
    request_path: str
    stdout_path: str
    stderr_path: str
    events_path: str
    result_path: str


def task_artifact_dir(artifacts_root: str | Path, task_id: str) -> Path:
    """Return ``<artifacts_root>/logs/<task-id>/`` — the root of one task's artifacts (§10).

    The single source of truth for the per-task artifact location. Callers that write task-level
    artifacts (``plan.md``, ``summary.md``, ``subtasks/``, ``checks/``, ``validation_report.json``,
    …) join onto this directory rather than reconstructing the layout.
    """
    return Path(artifacts_root) / "logs" / task_id


def archive_task_artifacts(
    artifacts_root: str | Path, task_id: str, attempt: int
) -> Path | None:
    """Move a prior attempt's artifacts into ``logs/<task-id>/attempt-<N>/`` for a fresh ``rerun``.

    Everything under the task dir except existing ``attempt-*`` archives is moved, so the fresh
    attempt starts with a clean ``logs/<task-id>/`` while the failure stays fully auditable. Returns
    the archive directory, or ``None`` when there is nothing to archive. Idempotent: a name already
    present in the destination (a half-done archive from an interrupted rerun) is skipped.
    """
    task_dir = task_artifact_dir(artifacts_root, task_id)
    if not task_dir.exists():
        return None
    entries = [p for p in task_dir.iterdir() if not p.name.startswith("attempt-")]
    if not entries:
        return None
    dest = task_dir / f"attempt-{attempt}"
    dest.mkdir(parents=True, exist_ok=True)
    for path in entries:
        target = dest / path.name
        if target.exists():
            continue
        path.rename(target)
    return dest


def create_attempt_dir(
    artifacts_root: str | Path,
    task_id: str,
    stage: Stage,
    attempt: int,
    provider: str,
    *,
    stage_run_id: int,
    subtask: int | None = None,
) -> ArtifactPaths:
    """Create the attempt directory and return its :class:`ArtifactPaths`.

    The directory must not already exist — logs are never overwritten (§10). ``stage_run_id`` is
    reserved in SQLite before the provider starts, so a repeated fixing cycle or recovery run gets
    a distinct directory even though its provider attempt counter starts again at one. A collision
    raises :class:`FileExistsError`.
    """
    stage_dir = Path(artifacts_root) / "logs" / task_id / "stages" / stage.value
    if subtask is not None:
        stage_dir = stage_dir / f"sub-{subtask:02d}"
    attempt_dir = stage_dir / f"run-{stage_run_id:06d}" / f"{attempt}-{provider}"
    attempt_dir.mkdir(parents=True, exist_ok=False)
    return ArtifactPaths(
        attempt_dir=str(attempt_dir),
        request_path=str(attempt_dir / REQUEST_FILENAME),
        stdout_path=str(attempt_dir / STDOUT_FILENAME),
        stderr_path=str(attempt_dir / STDERR_FILENAME),
        events_path=str(attempt_dir / EVENTS_FILENAME),
        result_path=str(attempt_dir / RESULT_FILENAME),
    )


def write_request_artifact(paths: ArtifactPaths, redacted_request: Mapping[str, Any]) -> str:
    """Write the **already-redacted** request representation to ``request.json`` (§10)."""
    return _write_json(paths.request_path, dict(redacted_request))


def write_result_artifact(paths: ArtifactPaths, result: AgentRunResult) -> str:
    """Write the machine-readable :class:`AgentRunResult` to ``result.json`` (§10)."""
    return _write_json(paths.result_path, dataclasses.asdict(result))


def _write_json(path: str, data: Any) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)
    Path(path).write_text(text + "\n", encoding="utf-8")
    return path


def sha256_file(path: str | Path) -> str:
    """Return the hex SHA-256 of a file's bytes (artifact checksum for the SQLite registry, §10)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHECKSUM_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
