"""MemoryService.clear (worc memory clear): snapshot-first, audited empty-rewrite, per-tier."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.memory import (
    AuditAction,
    AuditContext,
    EntityRecord,
    EpisodeRecord,
    LongTermKind,
    LongTermRecord,
    MemoryLayout,
    MemoryService,
    MemoryTier,
    TrustLevel,
)

_AUDIT = AuditContext(timestamp="2026-07-08T00:00:00Z")


@pytest.fixture
def layout(tmp_path: Path) -> MemoryLayout:
    return MemoryLayout.for_repo(tmp_path)


@pytest.fixture
def service(layout: MemoryLayout) -> MemoryService:
    return MemoryService(layout)


def _episode(service: MemoryService) -> None:
    service.append(
        EpisodeRecord(
            id="ep1",
            task_id="t1",
            created_at="2026-07-07T00:00:00Z",
            trust_level=TrustLevel.ARTIFACT_BACKED,
        ),
        audit=_AUDIT,
    )


def _lesson(service: MemoryService, kind: LongTermKind, memory_id: str) -> None:
    service.append(
        LongTermRecord(
            memory_id=memory_id,
            kind=kind,
            subject="s",
            statement="x",
            trust_level=TrustLevel.HUMAN_CURATED,
        ),
        audit=_AUDIT,
    )


def _entity(service: MemoryService) -> None:
    service.append(
        EntityRecord(
            entity_id="e1",
            entity_type="module",
            canonical_name="src/a.py",
            trust_level=TrustLevel.REPO_OBSERVED,
            paths=("src/a.py",),
        ),
        audit=_AUDIT,
    )


def _quarantine(service: MemoryService) -> None:
    service.replace_quarantine(
        [{"memory_id": "q1", "statement": "x"}], action=AuditAction.QUARANTINE, audit=_AUDIT
    )


def _populate_all(service: MemoryService) -> None:
    _episode(service)
    _lesson(service, LongTermKind.SEMANTIC, "m1")
    _entity(service)
    _quarantine(service)


def test_tier_counts_reflect_each_tier(service: MemoryService) -> None:
    _populate_all(service)
    assert service.tier_counts(list(MemoryTier)) == {
        MemoryTier.SHORT: 1,
        MemoryTier.LONG: 1,
        MemoryTier.ENTITY: 1,
        MemoryTier.QUARANTINE: 1,
    }


def test_clear_all_snapshots_then_empties_keeping_audit(service: MemoryService) -> None:
    _populate_all(service)
    report = service.clear(tiers=list(MemoryTier), audit=_AUDIT)
    assert service.read_episodes() == []
    assert service.read_entities() == []
    assert service.read_quarantine() == []
    assert all(service.read_long_term(kind) == [] for kind in LongTermKind)
    assert report.cleared == {
        MemoryTier.SHORT: 1,
        MemoryTier.LONG: 1,
        MemoryTier.ENTITY: 1,
        MemoryTier.QUARANTINE: 1,
    }
    assert report.snapshot is not None and report.snapshot.is_dir()
    # The append-only audit trail survives and still verifies (a wipe is not a purge).
    assert service.audit.verify_chain()


def test_clear_kind_long_sums_and_clears_only_long(service: MemoryService) -> None:
    _episode(service)
    _lesson(service, LongTermKind.SEMANTIC, "m1")
    _lesson(service, LongTermKind.FAILURE, "m2")
    report = service.clear(tiers=[MemoryTier.LONG], audit=_AUDIT)
    assert report.cleared == {MemoryTier.LONG: 2}
    assert all(service.read_long_term(kind) == [] for kind in LongTermKind)
    assert len(service.read_episodes()) == 1  # other tiers untouched


def test_clear_only_touches_nonempty_tiers(layout: MemoryLayout, service: MemoryService) -> None:
    _episode(service)  # only the short tier has content
    report = service.clear(tiers=list(MemoryTier), audit=_AUDIT)
    assert report.cleared == {MemoryTier.SHORT: 1}
    # No empty files / audit noise created for tiers that never had rows.
    assert not (layout.entities / "entities.jsonl").exists()
    assert not (layout.quarantine / "pending.jsonl").exists()
    assert not any((layout.long_term / f"{kind.value}.jsonl").exists() for kind in LongTermKind)
    # Exactly one PRUNE row was appended on top of the single episode APPEND.
    rows = service.audit.rows()
    assert len(rows) == 2
    assert rows[-1]["action"] == AuditAction.PRUNE.value


def test_clear_empty_store_is_a_noop(layout: MemoryLayout, service: MemoryService) -> None:
    layout.ensure_tree()  # store exists but holds no records
    report = service.clear(tiers=list(MemoryTier), audit=_AUDIT)
    assert report.snapshot is None and report.cleared == {}
    assert not layout.snapshots.exists()  # no snapshot taken when there is nothing to clear


def test_clear_is_reversible_via_restore(service: MemoryService) -> None:
    _populate_all(service)
    report = service.clear(tiers=list(MemoryTier), audit=_AUDIT)
    assert report.snapshot is not None
    service.restore(report.snapshot, audit=_AUDIT)
    assert len(service.read_episodes()) == 1
    assert len(service.read_entities()) == 1
    assert len(service.read_quarantine()) == 1
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1
