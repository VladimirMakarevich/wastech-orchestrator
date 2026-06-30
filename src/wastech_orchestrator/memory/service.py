"""MemoryService — the deterministic, model-free owner of the canonical store (design §1).

The storage core plus the **write-path funnel** (``apply_delta``). Defining invariants:

* **No write bypasses redaction.** Every persisted row passes :meth:`_redact` (AC-SF1).
* **No mutation bypasses the audit log.** Every mutating row write appends an audit row with the
  touched file's pre/post content hashes (AC-SF3).
* **Pure & deterministic.** Ids and timestamps ride on the records / the injected
  :class:`AuditContext`; nothing here reads a clock or calls a model.

``apply_delta`` is the single funnel both write seams feed (success delta + deterministic failure
record): validate → assign trust → merge/dedup → promote-or-quarantine → audit (design §2/§5/§7).
Retrieval/packets and ``DerivedIndex``-backed path/symbol validation are later phases.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import MemoryConfig
from wastech_orchestrator.memory import _io
from wastech_orchestrator.memory.audit import (
    AuditAction,
    AuditContext,
    AuditLog,
    AuditMarker,
    content_hash,
    file_content_hash,
    restore_snapshot,
    take_snapshot,
)
from wastech_orchestrator.memory.delta import (
    CandidateDelta,
    CandidateEntity,
    CandidateFailure,
    CandidateLesson,
)
from wastech_orchestrator.memory.lifecycle import (
    assign_entity_trust,
    assign_trust,
    normalize_subject,
    should_promote,
)
from wastech_orchestrator.memory.paths import MemoryLayout
from wastech_orchestrator.memory.records import (
    EntityRecord,
    EpisodeRecord,
    Evidence,
    LongTermKind,
    LongTermRecord,
    MemoryRecord,
    Scope,
    as_row,
    record_id,
)
from wastech_orchestrator.memory.trust import DURABLE_TRUST_LEVELS
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
_PENDING_FILE = "pending.jsonl"


class WriteSource(StrEnum):
    """Which seam produced a delta — gates long-term promotion (design §2)."""

    SUCCESS = "success"  # supervisor finalize delta — full pipeline
    FAILURE = "failure"  # deterministic terminal-failure/manual record — short-term only (AC-W3)


@dataclass(frozen=True)
class ApplyResult:
    """A summary of one ``apply_delta`` run (counts + episode id) — for logging/tests."""

    episode_id: str
    promoted: int = 0
    quarantined: int = 0
    merged: int = 0
    entities: int = 0


class MemoryService:
    """Owns the canonical ``.worc/memory/`` store: redacted+audited writes, reads, the write funnel.

    ``extra_secrets`` are literal values the caller already knows (redacted alongside the structural
    patterns). ``config`` supplies the promotion thresholds (Q3); it defaults to ``MemoryConfig()``.
    """

    def __init__(
        self,
        layout: MemoryLayout,
        *,
        extra_secrets: Sequence[str] = (),
        config: MemoryConfig | None = None,
        marker: AuditMarker | None = None,
    ) -> None:
        self._layout = layout
        self._extra_secrets = tuple(extra_secrets)
        self._config = config or MemoryConfig()
        self._audit = AuditLog(layout, extra_secrets=self._extra_secrets, marker=marker)

    @property
    def audit(self) -> AuditLog:
        return self._audit

    # --- write funnel (design §2/§5/§7) ---------------------------------------

    def apply_delta(
        self,
        delta: CandidateDelta | None,
        *,
        episode: EpisodeRecord,
        source: WriteSource,
        audit: AuditContext,
    ) -> ApplyResult:
        """The single deterministic funnel both write seams feed.

        Always appends the per-task ``episode`` (short-term). Then, for a SUCCESS delta, runs each
        lesson/failure/entity through validate → assign-trust → merge/dedup → promote-or-quarantine.
        A FAILURE source writes only the episode and never promotes to long-term (AC-W3). The
        mutation time is ``audit.timestamp`` (injected). Returns a summary of what happened.
        """
        if not audit.task_id:
            audit = replace(audit, task_id=episode.task_id)
        self.append(episode, audit=audit)
        result = ApplyResult(episode_id=record_id(episode))
        if delta is None:
            return result
        now = audit.timestamp
        task_id = episode.task_id
        for lesson in delta.lessons:
            result = self._ingest_lesson(
                lesson, source=source, audit=audit, now=now, task_id=task_id, r=result
            )
        for failure in delta.failures:
            result = self._ingest_failure(
                failure, source=source, audit=audit, now=now, task_id=task_id, r=result
            )
        for entity in delta.entities:
            result = self._ingest_entity(entity, source=source, audit=audit, r=result)
        return result

    def _ingest_lesson(
        self,
        cand: CandidateLesson,
        *,
        source: WriteSource,
        audit: AuditContext,
        now: str,
        task_id: str,
        r: ApplyResult,
    ) -> ApplyResult:
        return self._ingest_long_term(
            kind=cand.kind,
            subject=cand.subject,
            statement=cand.statement,
            rationale=cand.rationale,
            scope=cand.scope,
            evidence=cand.evidence,
            remedy=None,
            explained_failure=False,
            source=source,
            audit=audit,
            now=now,
            task_id=task_id,
            r=r,
        )

    def _ingest_failure(
        self,
        cand: CandidateFailure,
        *,
        source: WriteSource,
        audit: AuditContext,
        now: str,
        task_id: str,
        r: ApplyResult,
    ) -> ApplyResult:
        # A failure that ships a remedy "explained a recurring failure" (§5 auto-promote).
        return self._ingest_long_term(
            kind=LongTermKind.FAILURE,
            subject=cand.signature,
            statement=cand.remedy or cand.signature,
            rationale=None,
            scope=Scope(paths=cand.paths),
            evidence=cand.evidence,
            remedy=cand.remedy,
            explained_failure=cand.remedy is not None,
            source=source,
            audit=audit,
            now=now,
            task_id=task_id,
            r=r,
        )

    def _ingest_long_term(
        self,
        *,
        kind: LongTermKind,
        subject: str,
        statement: str,
        rationale: str | None,
        scope: Scope,
        evidence: tuple[Evidence, ...],
        remedy: str | None,
        explained_failure: bool,
        source: WriteSource,
        audit: AuditContext,
        now: str,
        task_id: str,
        r: ApplyResult,
    ) -> ApplyResult:
        trust = assign_trust(evidence)
        memory_id = _derive_id(kind, subject)
        pending = self._read_pending()
        prior = _find(pending, "memory_id", memory_id)
        seen = _distinct([*_as_str_list(prior.get("seen_task_ids") if prior else None), task_id])
        first_seen = (prior.get("first_seen_at") if prior else None) or now

        def build(status: str) -> LongTermRecord:
            return LongTermRecord(
                memory_id=memory_id,
                kind=kind,
                subject=subject,
                statement=statement,
                trust_level=trust,
                rationale=rationale,
                scope=scope,
                evidence=evidence,
                remedy=remedy,
                status=status,
                last_verified_at=now,
                usage_count=len(seen),
                first_seen_at=first_seen,
                seen_task_ids=tuple(seen),
            )

        # Quarantine (never long-term): a failure-seam source, missing evidence (AC-W2), or a
        # non-durable trust level (external-untrusted / agent-inferred — AC-SF2/AC-W4).
        if source is WriteSource.FAILURE or not evidence or trust not in DURABLE_TRUST_LEVELS:
            self._put_pending(pending, build("quarantined"), audit=audit)
            return _bump(r, quarantined=1)

        # Merge into an existing active record with the same kind + normalized subject (design §5).
        active = self.read_long_term(kind)
        match_at = _index_by_subject(active, normalize_subject(subject))
        if match_at is not None:
            rows = list(active)
            rows[match_at] = _merge_long_term(active[match_at], statement, evidence, now, task_id)
            self._replace_rows(
                self._long_term_path(kind),
                rows,
                ids=_ids(rows),
                action=AuditAction.MERGE,
                audit=audit,
            )
            return _bump(r, merged=1)

        # Promote when the §5/Q3 gate passes; otherwise hold in pending (short-term).
        within = _within_window(first_seen, now, self._config.promote_window_days)
        recurrence = len(seen) if within else 1
        if should_promote(
            trust=trust,
            has_evidence=True,
            recurrence_tasks=recurrence,
            min_tasks=self._config.promote_min_tasks,
            explained_failure=explained_failure,
        ):
            self.append(build("active"), audit=audit)
            if prior is not None:
                self._drop_pending(pending, memory_id, audit=audit)
            return _bump(r, promoted=1)
        self._put_pending(pending, build("quarantined"), audit=audit)
        return _bump(r, quarantined=1)

    def _ingest_entity(
        self, cand: CandidateEntity, *, source: WriteSource, audit: AuditContext, r: ApplyResult
    ) -> ApplyResult:
        trust = assign_entity_trust(cand.paths)
        record = EntityRecord(
            entity_id=cand.entity_id,
            entity_type=cand.entity_type,
            canonical_name=cand.paths[0] if cand.paths else cand.entity_id,
            paths=cand.paths,
            symbols=cand.symbols,
            summary=cand.summary,
            relationships=cand.relationships,
            risk_notes=cand.risk_notes,
            trust_level=trust,
            status="active",
        )
        if source is WriteSource.FAILURE or trust not in DURABLE_TRUST_LEVELS:
            self._put_pending(
                self._read_pending(), replace(record, status="quarantined"), audit=audit
            )
            return _bump(r, quarantined=1)
        entities = self.read_entities()
        at = _index_by(entities, "entity_id", cand.entity_id)
        if at is not None:  # upsert: the latest card wins (field-union merge is a later refinement)
            rows = list(entities)
            rows[at] = as_row(record)
            self._replace_rows(
                self._entities_path(), rows, ids=_ids(rows), action=AuditAction.MERGE, audit=audit
            )
            return _bump(r, merged=1)
        self.append(record, audit=audit)
        return _bump(r, entities=1)

    # --- typed writes (redacted + atomic + audited) ---------------------------

    def append(self, record: MemoryRecord, *, audit: AuditContext) -> None:
        """Append one record to its tier file (redacted, atomic) and write one audit row."""
        self._append_row(
            self._path_for(record),
            as_row(record),
            affected_id=record_id(record),
            action=AuditAction.APPEND,
            audit=audit,
        )

    def replace_all(
        self, records: Iterable[MemoryRecord], *, action: AuditAction, audit: AuditContext
    ) -> None:
        """Atomically rewrite each touched tier file to exactly the given records (redacted), one
        audit row per touched file. Each file is fully replaced (read-all → edit → write-all).
        """
        grouped: dict[Path, list[dict[str, Any]]] = {}
        for record in records:
            grouped.setdefault(self._path_for(record), []).append(as_row(record))
        for path, rows in grouped.items():
            self._replace_rows(path, rows, ids=_ids(rows), action=action, audit=audit)

    # --- snapshot / restore (design §7) ---------------------------------------

    def tier_files(self) -> list[Path]:
        """The canonical tier files that currently exist (snapshot targets for a batch mutation)."""
        candidates = [
            self._layout.short_term / _RECENT_FILE,
            self._layout.entities / _ENTITIES_FILE,
            self._layout.quarantine / _PENDING_FILE,
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

    # --- reads (for the PacketBuilder, later) ---------------------------------

    def read_episodes(self) -> list[dict[str, Any]]:
        return _io.read_jsonl(self._layout.short_term / _RECENT_FILE)

    def read_long_term(self, kind: LongTermKind) -> list[dict[str, Any]]:
        return _io.read_jsonl(self._long_term_path(kind))

    def read_entities(self) -> list[dict[str, Any]]:
        return _io.read_jsonl(self._entities_path())

    def read_quarantine(self) -> list[dict[str, Any]]:
        return self._read_pending()

    # --- internals ------------------------------------------------------------

    def _long_term_path(self, kind: LongTermKind) -> Path:
        return self._layout.long_term / _LONG_TERM_FILES[kind]

    def _entities_path(self) -> Path:
        return self._layout.entities / _ENTITIES_FILE

    def _pending_path(self) -> Path:
        return self._layout.quarantine / _PENDING_FILE

    def _read_pending(self) -> list[dict[str, Any]]:
        return _io.read_jsonl(self._pending_path())

    def _put_pending(
        self, pending: Sequence[Mapping[str, Any]], record: MemoryRecord, *, audit: AuditContext
    ) -> None:
        """Upsert one quarantined record into ``pending.jsonl`` (replacing any same-id prior)."""
        ident = record_id(record)
        others = [row for row in pending if _id_of(row) != ident]
        self._replace_rows(
            self._pending_path(),
            [*others, as_row(record)],
            ids=[ident],
            action=AuditAction.QUARANTINE,
            audit=audit,
        )

    def _drop_pending(
        self, pending: Sequence[Mapping[str, Any]], memory_id: str, *, audit: AuditContext
    ) -> None:
        """Remove a record from ``pending.jsonl`` (on promotion to long-term)."""
        others = [row for row in pending if _id_of(row) != memory_id]
        self._replace_rows(
            self._pending_path(), others, ids=[memory_id], action=AuditAction.PRUNE, audit=audit
        )

    def _path_for(self, record: MemoryRecord) -> Path:
        if isinstance(record, EpisodeRecord):
            return self._layout.short_term / _RECENT_FILE
        if isinstance(record, EntityRecord):
            return self._entities_path()
        if isinstance(record, LongTermRecord):
            return self._long_term_path(record.kind)
        raise TypeError(f"unknown memory record type: {type(record).__name__}")

    def _append_row(
        self,
        path: Path,
        row: Mapping[str, Any],
        *,
        affected_id: str,
        action: AuditAction,
        audit: AuditContext,
    ) -> None:
        pre = file_content_hash(path)
        _io.append_jsonl(path, self._redact(row))
        self._audit.record(
            action=action,
            affected_ids=[affected_id],
            pre_hash=pre,
            post_hash=file_content_hash(path),
            context=audit,
        )

    def _replace_rows(
        self,
        path: Path,
        rows: Sequence[Mapping[str, Any]],
        *,
        ids: Sequence[str],
        action: AuditAction,
        audit: AuditContext,
    ) -> None:
        pre = file_content_hash(path)
        _io.atomic_write_jsonl(path, [self._redact(row) for row in rows])
        self._audit.record(
            action=action,
            affected_ids=list(ids),
            pre_hash=pre,
            post_hash=file_content_hash(path),
            context=audit,
        )

    def _redact(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """The sole redaction chokepoint — every persisted row passes through here first."""
        return redact_mapping(dict(row), extra_secrets=self._extra_secrets)


# --- module-level pure helpers ------------------------------------------------


def _derive_id(kind: LongTermKind, subject: str) -> str:
    """Deterministic content-derived long-term id; stable across recurrences of a subject."""
    digest = content_hash(f"{kind.value}:{normalize_subject(subject)}".encode())[:12]
    return f"ltm_{digest}"


def _id_of(row: Mapping[str, Any]) -> Any:
    """The identifier of a stored row regardless of tier (memory_id / entity_id / id)."""
    return row.get("memory_id") or row.get("entity_id") or row.get("id")


def _ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(_id_of(row)) for row in rows if _id_of(row) is not None]


def _find(rows: Sequence[Mapping[str, Any]], key: str, value: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get(key) == value:
            return dict(row)
    return None


def _index_by(rows: Sequence[Mapping[str, Any]], key: str, value: str) -> int | None:
    for index, row in enumerate(rows):
        if row.get(key) == value:
            return index
    return None


def _index_by_subject(rows: Sequence[Mapping[str, Any]], normalized: str) -> int | None:
    for index, row in enumerate(rows):
        subject = row.get("subject")
        if isinstance(subject, str) and normalize_subject(subject) == normalized:
            return index
    return None


def _merge_long_term(
    existing: Mapping[str, Any],
    statement: str,
    evidence: Sequence[Evidence],
    now: str,
    task_id: str,
) -> dict[str, Any]:
    """Merge a candidate into an active long-term row (§5): keep the oldest id, union
    evidence, take the newest wording, and record the recurrence. Dict-level (no reconstruction)."""
    merged = dict(existing)
    merged["statement"] = statement  # newest wording
    union = [dict(item) for item in (existing.get("evidence") or []) if isinstance(item, Mapping)]
    have = {(item.get("type"), item.get("ref")) for item in union}
    for item in evidence:
        if (item.type, item.ref) not in have:
            union.append({"type": item.type, "ref": item.ref})
            have.add((item.type, item.ref))
    merged["evidence"] = union
    seen = _distinct([*_as_str_list(existing.get("seen_task_ids")), task_id])
    merged["seen_task_ids"] = seen
    merged["usage_count"] = len(seen)
    merged["last_verified_at"] = now
    merged["status"] = "active"
    return merged


def _within_window(first_seen_at: str | None, now: str, days: int) -> bool:
    """Whether the span from ``first_seen_at`` to ``now`` is within ``days`` (best-effort on bad
    input → ``True``, so an unparseable stamp never blocks a recurrence-based promotion)."""
    if first_seen_at is None:
        return True
    try:
        start = datetime.fromisoformat(first_seen_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return True
    return end - start <= timedelta(days=days)


def _distinct(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _bump(
    result: ApplyResult,
    *,
    promoted: int = 0,
    quarantined: int = 0,
    merged: int = 0,
    entities: int = 0,
) -> ApplyResult:
    return replace(
        result,
        promoted=result.promoted + promoted,
        quarantined=result.quarantined + quarantined,
        merged=result.merged + merged,
        entities=result.entities + entities,
    )
