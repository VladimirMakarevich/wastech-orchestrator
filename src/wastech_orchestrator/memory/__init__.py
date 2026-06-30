"""Persistent, repo-scoped memory subsystem (docs/backlog/memory/).

A files-first, supervisor-distilled, deterministically-managed, evidence-backed memory layer under
the gitignored ``<repo>/.worc/memory/`` home. This package owns the **deterministic, model-free**
core: the canonical store layout, redacted atomic writes, trust/records, audit, and snapshots. The
single risky judgment — what to remember — stays with the narrow supervisor (a later phase); every
mutation here is pure, separately testable, and auditable.

Foundations phase (01): store layout + config + the ``MemoryService`` skeleton + audit/snapshots.
Nothing here calls an LLM.
"""

from __future__ import annotations

from wastech_orchestrator.memory.audit import (
    AuditAction,
    AuditActor,
    AuditContext,
    AuditLog,
    content_hash,
    file_content_hash,
    restore_snapshot,
    take_snapshot,
)
from wastech_orchestrator.memory.paths import (
    MEMORY_SCHEMA_VERSION,
    MemoryLayout,
    build_manifest,
    ensure_store,
)
from wastech_orchestrator.memory.records import (
    EntityRecord,
    EpisodeRecord,
    Evidence,
    LongTermKind,
    LongTermRecord,
    MemoryRecord,
    Relationship,
    Scope,
    as_row,
    record_id,
)
from wastech_orchestrator.memory.service import MemoryService
from wastech_orchestrator.memory.trust import (
    DURABLE_TRUST_LEVELS,
    TrustLevel,
    is_durable_candidate,
)

__all__ = [
    "DURABLE_TRUST_LEVELS",
    "MEMORY_SCHEMA_VERSION",
    "AuditAction",
    "AuditActor",
    "AuditContext",
    "AuditLog",
    "EntityRecord",
    "EpisodeRecord",
    "Evidence",
    "LongTermKind",
    "LongTermRecord",
    "MemoryLayout",
    "MemoryRecord",
    "MemoryService",
    "Relationship",
    "Scope",
    "TrustLevel",
    "as_row",
    "build_manifest",
    "content_hash",
    "ensure_store",
    "file_content_hash",
    "is_durable_candidate",
    "record_id",
    "restore_snapshot",
    "take_snapshot",
]
