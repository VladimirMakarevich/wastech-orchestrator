"""Audit trail + snapshots for the memory store.

Two day-one safety primitives, both deterministic and model-free:

* **Append-only, hash-chained audit log** (``audit/log.jsonl``). Every mutation through
  ``MemoryService`` writes exactly one row recording who/what/when, the affected memory ids, the
  pre/post content hashes of the touched file, and a rationale. Each row carries the previous row's
  hash (``prev_hash``) plus its own (``row_hash``), so tampering is detectable and the log can only
  grow — never an in-place rewrite.
* **Snapshots + restore.** Before a batch mutation the affected tier files are copied byte-for-byte
  into ``audit/snapshots/<label>/``; :func:`restore_snapshot` puts them back exactly, making a bad
  write cheap to undo.

Timestamps are injected by the caller (no hidden clock) so the log is deterministic under test.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from wastech_orchestrator.memory import _io
from wastech_orchestrator.memory.paths import MemoryLayout
from wastech_orchestrator.providers.redaction import redact_mapping

_LOG = logging.getLogger(__name__)

# A best-effort secondary sink: the orchestrator wires it to mirror each memory mutation
# into the existing ``evaluations`` decision trail. It receives the (redacted) audit row; a failure
# is swallowed — it must never fail the memory write. No ``state.db`` schema change is involved.
AuditMarker = Callable[[Mapping[str, Any]], None]


class AuditActor(StrEnum):
    """Who performed a memory mutation."""

    FINALIZER = "finalizer"  # the finalization write path (success or failure record)
    CLEANUP = "cleanup"  # the background cleanup job
    OPERATOR = "operator"  # an explicit `worc memory` command


class AuditAction(StrEnum):
    """What a memory mutation did."""

    APPEND = "append"
    PROMOTE = "promote"
    MERGE = "merge"
    QUARANTINE = "quarantine"
    PRUNE = "prune"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class AuditContext:
    """The caller-supplied half of an audit row (the log derives id/hashes/chain itself)."""

    timestamp: str
    actor: AuditActor = AuditActor.FINALIZER
    rationale: str = ""
    source_artifacts: tuple[str, ...] = ()
    task_id: str = ""  # the owning task (recorded on the row + used for the evaluations marker)


def content_hash(data: bytes) -> str:
    """SHA-256 hex digest of ``data`` — the content-hash primitive for the audit pre/post fields."""
    return hashlib.sha256(data).hexdigest()


def file_content_hash(path: Path) -> str | None:
    """Content hash of a file, or ``None`` if it does not exist (e.g. before the first append)."""
    if not path.exists():
        return None
    return content_hash(path.read_bytes())


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


class AuditLog:
    """The append-only, hash-chained ``audit/log.jsonl``."""

    def __init__(
        self,
        layout: MemoryLayout,
        *,
        extra_secrets: Sequence[str] = (),
        marker: AuditMarker | None = None,
    ) -> None:
        self._path = layout.audit / "log.jsonl"
        self._extra_secrets = tuple(extra_secrets)
        self._marker = marker

    @property
    def path(self) -> Path:
        return self._path

    def rows(self) -> list[dict[str, Any]]:
        return _io.read_jsonl(self._path)

    def record(
        self,
        *,
        action: AuditAction,
        affected_ids: Sequence[str],
        pre_hash: str | None,
        post_hash: str | None,
        context: AuditContext,
    ) -> dict[str, Any]:
        """Append exactly one audit row, chained to the previous one. Returns the written row.

        The row is redacted (its ``rationale`` can carry free text) before the chain hash is taken,
        so the stored ``row_hash`` covers exactly the on-disk bytes.
        """
        existing = _io.read_jsonl(self._path)
        prev_hash = existing[-1]["row_hash"] if existing else ""
        body: dict[str, Any] = redact_mapping(
            {
                "id": f"audit_{len(existing):06d}",
                "task_id": context.task_id,
                "timestamp": context.timestamp,
                "actor": str(context.actor),
                "action": str(action),
                "source_artifacts": list(context.source_artifacts),
                "affected_ids": list(affected_ids),
                "pre_hash": pre_hash,
                "post_hash": post_hash,
                "rationale": context.rationale,
                "prev_hash": prev_hash,
            },
            extra_secrets=self._extra_secrets,
        )
        body["row_hash"] = content_hash(_canonical(body))
        _io.append_jsonl(self._path, body)
        self._emit_marker(body)
        return body

    def _emit_marker(self, row: Mapping[str, Any]) -> None:
        """Best-effort secondary marker: a marker failure never fails the write."""
        if self._marker is None:
            return
        try:
            self._marker(row)
        except Exception as exc:
            _LOG.warning(
                "memory audit marker failed (best-effort, ignored)",
                extra={"error_type": type(exc).__name__},
            )

    def verify_chain(self) -> bool:
        """Recompute every row's hash + linkage; ``True`` iff the chain is intact (append-only)."""
        prev = ""
        for row in _io.read_jsonl(self._path):
            stored = row.get("row_hash")
            if row.get("prev_hash") != prev:
                return False
            body = {key: value for key, value in row.items() if key != "row_hash"}
            if content_hash(_canonical(body)) != stored:
                return False
            prev = stored
        return True


def _safe_label(label: str) -> str:
    """Make ``label`` safe as a directory name on every OS (Windows forbids ``:`` etc.)."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in label)
    return safe or "snapshot"


def take_snapshot(layout: MemoryLayout, paths: Iterable[Path], *, label: str) -> Path:
    """Copy each existing file in ``paths`` byte-for-byte into ``audit/snapshots/<label>/``.

    Files are stored under their path relative to the memory root so :func:`restore_snapshot` can
    put them back exactly. Returns the snapshot directory (created even if no file was copied).
    """
    snapshot_dir = layout.snapshots / _safe_label(label)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for src in paths:
        if not src.is_file():
            continue
        destination = snapshot_dir / src.relative_to(layout.root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(src.read_bytes())
    return snapshot_dir


def restore_snapshot(layout: MemoryLayout, snapshot_dir: Path) -> list[Path]:
    """Restore every file in ``snapshot_dir`` back under the memory root, byte-for-byte.

    Returns the restored destination paths. The append-only audit log is never part of a snapshot,
    so a restore rewinds tier content without rewinding the trail.
    """
    restored: list[Path] = []
    for src in sorted(snapshot_dir.rglob("*")):
        if not src.is_file():
            continue
        destination = layout.root / src.relative_to(snapshot_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(src.read_bytes())
        restored.append(destination)
    return restored
