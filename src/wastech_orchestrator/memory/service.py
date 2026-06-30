"""MemoryService — the deterministic, model-free owner of the canonical store (design §1).

This is the storage core: redacted, atomic write primitives, a read API the ``PacketBuilder`` will
use later, and the audit/snapshot safety net. Two defining invariants:

* **No write bypasses redaction.** Every persisted row passes :meth:`_redact` (AC-SF1).
* **No mutation bypasses the audit log.** Every mutating method appends an audit row capturing the
  action, affected ids, and the touched file's pre/post content hashes (AC-SF3).

The service is pure and deterministic — ids and timestamps ride on the records / the injected
:class:`AuditContext`, never read from a clock here, and no method calls a model.

Out of scope here (later phases): ``apply_delta`` promotion/merge/quarantine *logic* (this exposes
the audited primitives it will use) and retrieval/packet building.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from wastech_orchestrator.memory import _io
from wastech_orchestrator.memory.audit import (
    AuditAction,
    AuditContext,
    AuditLog,
    file_content_hash,
    restore_snapshot,
    take_snapshot,
)
from wastech_orchestrator.memory.paths import MemoryLayout
from wastech_orchestrator.memory.records import (
    EntityRecord,
    EpisodeRecord,
    LongTermKind,
    LongTermRecord,
    MemoryRecord,
    as_row,
    record_id,
)
from wastech_orchestrator.providers.redaction import redact_mapping

# Long-term lessons are split one file per kind (design §3 layout).
_LONG_TERM_FILES: dict[LongTermKind, str] = {
    LongTermKind.SEMANTIC: "semantic.jsonl",
    LongTermKind.PROCEDURAL: "procedural.jsonl",
    LongTermKind.REVIEWER: "reviewer.jsonl",
    LongTermKind.FAILURE: "failures.jsonl",
}
_RECENT_FILE = "recent.jsonl"
_ENTITIES_FILE = "entities.jsonl"


class MemoryService:
    """Owns the canonical ``.worc/memory/`` store: redacted atomic writes, reads, audit + snapshots.

    ``extra_secrets`` are literal values the caller already knows (e.g. harvested from denied-read
    files) to redact alongside the structural token/assignment patterns — applied to records *and*
    audit rows.
    """

    def __init__(self, layout: MemoryLayout, *, extra_secrets: Sequence[str] = ()) -> None:
        self._layout = layout
        self._extra_secrets = tuple(extra_secrets)
        self._audit = AuditLog(layout, extra_secrets=self._extra_secrets)

    @property
    def audit(self) -> AuditLog:
        return self._audit

    # --- writes (redacted + atomic + audited) ---

    def append(self, record: MemoryRecord, *, audit: AuditContext) -> None:
        """Append one record to its tier file (redacted, atomic) and write one audit row.

        The single record-write entry point, so redaction is structurally unbypassable. Parent
        directories are created on demand — an explicit :func:`ensure_store` is not required first.
        """
        path = self._path_for(record)
        pre = file_content_hash(path)
        _io.append_jsonl(path, self._redact(record))
        self._audit.record(
            action=AuditAction.APPEND,
            affected_ids=[record_id(record)],
            pre_hash=pre,
            post_hash=file_content_hash(path),
            context=audit,
        )

    def replace_all(
        self, records: Iterable[MemoryRecord], *, action: AuditAction, audit: AuditContext
    ) -> None:
        """Atomically rewrite each touched tier file to exactly the given records (redacted), one
        audit row per touched file.

        The *update* counterpart to :meth:`append`, used by promotion/merge/quarantine edits (later
        phases). Records are grouped by target file; **each touched file is fully replaced**, so the
        caller passes the complete desired contents (read-all → edit → write-all). A file no record
        maps to is left untouched.
        """
        grouped: dict[Path, tuple[list[dict[str, Any]], list[str]]] = {}
        for record in records:
            rows, ids = grouped.setdefault(self._path_for(record), ([], []))
            rows.append(self._redact(record))
            ids.append(record_id(record))
        for path, (rows, ids) in grouped.items():
            pre = file_content_hash(path)
            _io.atomic_write_jsonl(path, rows)
            self._audit.record(
                action=action,
                affected_ids=ids,
                pre_hash=pre,
                post_hash=file_content_hash(path),
                context=audit,
            )

    # --- snapshot / restore (reversibility primitives; design §7) ---

    def tier_files(self) -> list[Path]:
        """The canonical tier files that currently exist (snapshot targets for a batch mutation)."""
        candidates = [
            self._layout.short_term / _RECENT_FILE,
            self._layout.entities / _ENTITIES_FILE,
            *(self._layout.long_term / name for name in _LONG_TERM_FILES.values()),
        ]
        return [path for path in candidates if path.is_file()]

    def snapshot(self, paths: Iterable[Path], *, label: str) -> Path:
        """Copy ``paths`` byte-for-byte into ``audit/snapshots/<label>/`` before a batch."""
        return take_snapshot(self._layout, paths, label=label)

    def restore(self, snapshot_dir: Path, *, audit: AuditContext) -> list[Path]:
        """Restore tier files from a snapshot byte-for-byte and log a ``rollback`` audit row."""
        restored = restore_snapshot(self._layout, snapshot_dir)
        self._audit.record(
            action=AuditAction.ROLLBACK,
            affected_ids=[],
            pre_hash=None,
            post_hash=None,
            context=audit,
        )
        return restored

    # --- reads (for the PacketBuilder, later) ---

    def read_episodes(self) -> list[dict[str, Any]]:
        return _io.read_jsonl(self._layout.short_term / _RECENT_FILE)

    def read_long_term(self, kind: LongTermKind) -> list[dict[str, Any]]:
        return _io.read_jsonl(self._layout.long_term / _LONG_TERM_FILES[kind])

    def read_entities(self) -> list[dict[str, Any]]:
        return _io.read_jsonl(self._layout.entities / _ENTITIES_FILE)

    # --- internals ---

    def _path_for(self, record: MemoryRecord) -> Path:
        if isinstance(record, EpisodeRecord):
            return self._layout.short_term / _RECENT_FILE
        if isinstance(record, EntityRecord):
            return self._layout.entities / _ENTITIES_FILE
        if isinstance(record, LongTermRecord):
            return self._layout.long_term / _LONG_TERM_FILES[record.kind]
        raise TypeError(f"unknown memory record type: {type(record).__name__}")

    def _redact(self, record: MemoryRecord) -> dict[str, Any]:
        """The sole record-redaction chokepoint — every persisted row passes through here first."""
        return redact_mapping(as_row(record), extra_secrets=self._extra_secrets)
