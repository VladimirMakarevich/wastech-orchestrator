"""Artifact writer.

Writes the per-attempt artifacts under
``<artifacts_root>/logs/<task-id>/stages/<stage>/run-<stage-run-id>/<attempt>-<provider>/`` (with a
``sub-<NN>/`` level for a decomposed subtask). The directory layout and the **never overwrite**
rule live here; the *content* (already redacted) is supplied by the caller — this module imports
neither :mod:`~wastech_orchestrator.providers.redaction` nor any provider syntax.

Artifacts: ``request.json`` (redacted), ``stdout.log``, ``stderr.log``, ``events.jsonl``,
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

from wastech_orchestrator.providers.base import AgentRunResult

_CHECKSUM_CHUNK = 65536

REQUEST_FILENAME = "request.json"
STDOUT_FILENAME = "stdout.log"
STDERR_FILENAME = "stderr.log"
EVENTS_FILENAME = "events.jsonl"
RESULT_FILENAME = "result.json"

# Custom tool-node (P5) artifact filenames under ``<task_dir>/tools/<node_id>/``. The stdout file is
# the one exposed downstream as ``{<node_id>_path}`` (both redacted before they are written).
TOOL_STDOUT_FILENAME = "stdout.txt"
TOOL_STDERR_FILENAME = "stderr.txt"

# Which per-attempt files survive at each ``logging.artifacts`` level. ``full`` (or any unknown
# level) keeps everything. ``result.json`` is always kept — it is the machine-readable outcome and
# carries the exit code + normalized error class even on failure.
_ARTIFACT_KEEP: dict[str, set[str]] = {
    "minimal": {RESULT_FILENAME},
    "standard": {RESULT_FILENAME, STDOUT_FILENAME, STDERR_FILENAME},
}


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
    """Return ``<artifacts_root>/logs/<task-id>/`` — the root of one task's artifacts.

    The single source of truth for the per-task artifact location. Callers that write task-level
    artifacts (``plan.md``, ``summary.md``, ``subtasks/``, ``checks/``, ``validation_report.json``,
    …) join onto this directory rather than reconstructing the layout.
    """
    return Path(artifacts_root) / "logs" / task_id


def tool_node_dir(artifacts_root: str | Path, task_id: str, node_id: str) -> Path:
    """Return ``<task_dir>/tools/<node_id>/`` — a custom tool node's artifact directory (P5).

    The single source of truth for a ``tool`` node's stdout/stderr artifact location, shared by the
    tool runner (which writes the redacted streams there) and the ``{<node_id>_path}`` resolver
    (which points downstream nodes at the stdout file). Both join onto this rather than
    reconstructing the layout.
    """
    return task_artifact_dir(artifacts_root, task_id) / "tools" / node_id


def task_artifact_relpath(artifacts_root: str | Path, task_id: str, repo_root: str | Path) -> str:
    """The task artifact dir as a **repo-relative POSIX** string (e.g. ``.worc/logs/<task-id>``).

    Memory episodes store this instead of the absolute host path so a memory record carries no
    ``/Users/<name>/…`` prefix (F36): the absolute prefix was both a privacy leak and the source of
    non-deterministic redaction (a run-harvested secret literal occasionally matched the prefix of
    an otherwise-harmless path). Falls back to the absolute POSIX form only when the artifact dir is
    not under ``repo_root`` (unusual). Both sides are resolved so symlinks match consistently.
    """
    artifact_dir = task_artifact_dir(artifacts_root, task_id).resolve()
    try:
        return artifact_dir.relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return artifact_dir.as_posix()


def archive_task_artifacts(artifacts_root: str | Path, task_id: str, attempt: int) -> Path | None:
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
    node_id: str,
    attempt: int,
    provider: str,
    *,
    node_run_id: int,
    subtask: int | None = None,
) -> ArtifactPaths:
    """Create the attempt directory and return its :class:`ArtifactPaths`.

    The directory must not already exist — logs are never overwritten. ``node_run_id`` is
    reserved in SQLite before the provider starts, so a repeated fixing cycle or recovery run gets
    a distinct directory even though its provider attempt counter starts again at one. A collision
    raises :class:`FileExistsError`.
    """
    stage_dir = Path(artifacts_root) / "logs" / task_id / "stages" / node_id
    if subtask is not None:
        stage_dir = stage_dir / f"sub-{subtask:02d}"
    attempt_dir = stage_dir / f"run-{node_run_id:06d}" / f"{attempt}-{provider}"
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
    """Write the **already-redacted** request representation to ``request.json``."""
    return _write_json(paths.request_path, dict(redacted_request))


def write_result_artifact(paths: ArtifactPaths, result: AgentRunResult) -> str:
    """Write the machine-readable :class:`AgentRunResult` to ``result.json``."""
    return _write_json(paths.result_path, dataclasses.asdict(result))


def prune_attempt_artifacts(paths: ArtifactPaths, level: str) -> None:
    """Delete the per-attempt files not retained at ``level`` (``logging.artifacts``).

    Called at the very end of a run — after the stream has been parsed into memory and
    ``result.json`` written — so removing ``stdout.log``/``events.jsonl``/etc. is always safe; the
    in-memory :class:`AgentRunResult` keeps its path strings and the authoritative state lives in
    ``state.db``. ``full`` (and any unknown level) is a no-op. Iterating the directory also prunes
    provider-specific extras (e.g. ``output-schema.json``) below ``full``.
    """
    keep = _ARTIFACT_KEEP.get(level)
    if keep is None:
        return
    for entry in Path(paths.attempt_dir).iterdir():
        if entry.is_file() and entry.name not in keep:
            entry.unlink()


def _write_json(path: str, data: Any) -> str:
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)
    Path(path).write_text(text + "\n", encoding="utf-8")
    return path


def sha256_file(path: str | Path) -> str:
    """Return the hex SHA-256 of a file's bytes (artifact checksum for the SQLite registry)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHECKSUM_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
