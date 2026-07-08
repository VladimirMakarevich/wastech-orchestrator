"""Memory record schemas (design §4, blueprint §5.3) — typed, frozen, provenance-bearing.

Three tiers: short-term episodic (:class:`EpisodeRecord`), long-term lessons
(:class:`LongTermRecord`, routed by ``kind`` to semantic / procedural / reviewer / failures), and
entity cards (:class:`EntityRecord`). Invariants the skeleton enforces:

* **Trust is required.** Every record carries a ``trust_level`` as a non-default field, so a record
  cannot be constructed without one (AC-SF5 groundwork). The service assigns the final trust — a
  record never self-certifies.
* **Provenance travels with the record** (``evidence`` / ``artifact_paths``).
* **No hidden clock.** Ids and timestamps are supplied by the caller; nothing here reads the time.
* **POSIX paths.** Any stored path string is the ``as_posix()`` form (AC-X1).

:func:`as_row` is the JSON-serializable form the service redacts and writes; ``StrEnum`` fields
(trust, kind) serialize to their string value automatically.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from wastech_orchestrator.memory.trust import TrustLevel


class LongTermKind(StrEnum):
    """Which long-term file a lesson is routed to (one ``.jsonl`` per kind)."""

    SEMANTIC = "semantic"  # stable repo facts, commands, fragile areas
    PROCEDURAL = "procedural"  # verified workflows
    REVIEWER = "reviewer"  # recurring reviewer expectations
    FAILURE = "failure"  # failure signatures + canonical remedy


class MemoryTier(StrEnum):
    """A selectable top-level tier for ``worc memory clear`` — the storage families a wipe targets.

    Each maps to whole tier file(s): ``short`` → ``recent.jsonl``; ``long`` → the four
    ``long_term/*.jsonl`` (one per :class:`LongTermKind`); ``entity`` → ``entities.jsonl``;
    ``quarantine`` → ``pending.jsonl`` (mixed-kind holding area). Clearing every tier is a true
    zero.
    """

    SHORT = "short"
    LONG = "long"
    ENTITY = "entity"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class Evidence:
    """A provenance pointer backing a record (blueprint §5.3).

    ``type`` is the source class (e.g. ``repo_doc`` | ``task`` | ``check`` | ``review`` | ``diff`` |
    ``artifact``); ``ref`` is the pointer itself (a path, task id, or artifact path).
    """

    type: str
    ref: str


@dataclass(frozen=True)
class Relationship:
    """A typed edge from an entity card to another entity/artifact (e.g. ``depends_on``)."""

    type: str
    target: str


@dataclass(frozen=True)
class Scope:
    """Where a lesson applies — drives path-scoped retrieval (design §10).

    ``nodes`` are flow node ids (there is no ``Stage`` enum). ``paths`` are POSIX repo-relative.
    """

    paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpisodeRecord:
    """A distilled per-run outcome (short-term episodic tier).

    Raw resume/debug detail stays in ``logs/<task-id>/`` + ``state.db`` (Q7); this keeps only the
    distilled episode plus ``artifact_paths`` pointers.
    """

    id: str
    task_id: str
    created_at: str
    trust_level: TrustLevel
    task_type: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    touched_paths: tuple[str, ...] = ()
    touched_symbols: tuple[str, ...] = ()
    # Write-once: built at construction and only ever read (serialized via ``as_row``). Kept a
    # ``dict`` for JSON symmetry, so it is mutable in place despite ``frozen=True`` — treat as
    # immutable (nothing mutates it after construction).
    stage_outcomes: dict[str, str] = field(default_factory=dict)
    artifact_paths: tuple[str, ...] = ()
    expires_at: str | None = None


@dataclass(frozen=True)
class LongTermRecord:
    """A durable lesson (long-term tier).

    ``kind`` routes the record to the semantic / procedural / reviewer / failures file. A
    ``failure``-kind record carries a ``remedy`` and uses ``subject`` as the failure signature; the
    other kinds carry ``statement`` + ``rationale``.
    """

    memory_id: str
    kind: LongTermKind
    subject: str
    statement: str
    trust_level: TrustLevel
    rationale: str | None = None
    scope: Scope = field(default_factory=Scope)
    evidence: tuple[Evidence, ...] = ()
    remedy: str | None = None
    status: str = "active"
    first_seen_commit: str | None = None
    last_verified_commit: str | None = None
    last_verified_at: str | None = None
    usage_count: int = 0
    supersedes: tuple[str, ...] = ()
    # Recurrence bookkeeping (design §5 / Q3): the distinct tasks that have proposed this lesson and
    # when it was first seen — drive the "recurred in >= N tasks within the window" promotion gate.
    # Carried on quarantined (pending) records; an active promoted record keeps the history too.
    first_seen_at: str | None = None
    seen_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityRecord:
    """An entity card (file / module / context / dependency / owner) for path-scoped retrieval."""

    entity_id: str
    entity_type: str
    canonical_name: str
    trust_level: TrustLevel
    aliases: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    summary: str = ""
    relationships: tuple[Relationship, ...] = ()
    risk_notes: tuple[str, ...] = ()
    memory_refs: tuple[str, ...] = ()
    last_seen_task_ids: tuple[str, ...] = ()
    last_validated_commit: str | None = None
    status: str = "active"


type MemoryRecord = EpisodeRecord | LongTermRecord | EntityRecord


def as_row(record: MemoryRecord) -> dict[str, Any]:
    """The JSON-serializable dict for one record (nested dataclasses → dicts; enums → values).

    This is the form the service redacts and writes; it performs no redaction itself.
    """
    return asdict(record)


def record_id(record: MemoryRecord) -> str:
    """The record's stable identifier across tiers (its ``id`` / ``memory_id`` / ``entity_id``)."""
    if isinstance(record, EpisodeRecord):
        return record.id
    if isinstance(record, LongTermRecord):
        return record.memory_id
    return record.entity_id
