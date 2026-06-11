"""Artifact writer (spec §10).

Writes the per-attempt artifacts under
``<artifacts_root>/logs/<task-id>/stages/<stage>/<attempt>-<provider>/`` (with a ``sub-<NN>/`` level
for a decomposed subtask). The directory layout and the **never overwrite** rule live here; the
*content* (already redacted) is supplied by the caller — this module imports neither
:mod:`~wastech_orchestrator.providers.redaction` nor any provider syntax.

Artifacts (§10): ``request.json`` (redacted), ``stdout.log``, ``stderr.log``, ``events.jsonl``,
``result.json``. ``before.diff`` / ``after.diff`` are stamped by the pipeline in P5.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wastech_orchestrator.providers.base import AgentRunResult, Stage

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


def create_attempt_dir(
    artifacts_root: str | Path,
    task_id: str,
    stage: Stage,
    attempt: int,
    provider: str,
    *,
    subtask: int | None = None,
) -> ArtifactPaths:
    """Create the attempt directory and return its :class:`ArtifactPaths`.

    The directory must not already exist — logs are never overwritten (§10); a re-run uses a
    distinct ``attempt`` (or ``subtask``) and therefore a distinct directory. A collision raises
    :class:`FileExistsError`.
    """
    stage_dir = Path(artifacts_root) / "logs" / task_id / "stages" / stage.value
    if subtask is not None:
        stage_dir = stage_dir / f"sub-{subtask:02d}"
    attempt_dir = stage_dir / f"{attempt}-{provider}"
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
