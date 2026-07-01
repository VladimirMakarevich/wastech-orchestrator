"""Persistent, repo-scoped memory subsystem (docs/backlog/memory/).

A files-first, supervisor-distilled, deterministically-managed, evidence-backed memory layer under
the gitignored ``<repo>/.worc/memory/`` home. This package owns the **deterministic, model-free**
core: the canonical store layout, redacted atomic writes, trust/records, audit, and snapshots. The
single risky judgment — what to remember — stays with the narrow supervisor; the supervisor only
*proposes* a candidate delta, while every mutation here is pure, separately testable, and auditable.

Nothing in this package calls an LLM.
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
from wastech_orchestrator.memory.cleanup import CleanupJob, CleanupReport
from wastech_orchestrator.memory.delta import (
    DELTA_OUTPUT_SCHEMA,
    CandidateDelta,
    CandidateEntity,
    CandidateFailure,
    CandidateLesson,
    parse_delta,
)
from wastech_orchestrator.memory.derived import DerivedIndex, git_tracked_paths
from wastech_orchestrator.memory.packet import PacketBuilder, PacketContext, SelectedPacket
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
from wastech_orchestrator.memory.service import ApplyResult, MemoryService, WriteSource
from wastech_orchestrator.memory.trust import (
    DURABLE_TRUST_LEVELS,
    TrustLevel,
    is_durable_candidate,
)

__all__ = [
    "DELTA_OUTPUT_SCHEMA",
    "DURABLE_TRUST_LEVELS",
    "MEMORY_SCHEMA_VERSION",
    "AuditAction",
    "AuditActor",
    "AuditContext",
    "AuditLog",
    "ApplyResult",
    "CandidateDelta",
    "CandidateEntity",
    "CandidateFailure",
    "CandidateLesson",
    "CleanupJob",
    "CleanupReport",
    "DerivedIndex",
    "EntityRecord",
    "EpisodeRecord",
    "Evidence",
    "LongTermKind",
    "LongTermRecord",
    "MemoryLayout",
    "MemoryRecord",
    "MemoryService",
    "PacketBuilder",
    "PacketContext",
    "Relationship",
    "Scope",
    "SelectedPacket",
    "TrustLevel",
    "WriteSource",
    "as_row",
    "build_manifest",
    "content_hash",
    "ensure_store",
    "file_content_hash",
    "git_tracked_paths",
    "is_durable_candidate",
    "parse_delta",
    "record_id",
    "restore_snapshot",
    "take_snapshot",
]
