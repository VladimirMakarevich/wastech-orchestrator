"""Safety drill 05.4 — snapshot → bad cleanup → restore returns byte-identical pre-state (AC-SF4).

Adversarial: populate memory, let the cleanup take its automatic pre-batch snapshot and mutate the
store, then restore from that snapshot and assert the tier files are byte-for-byte the pre-cleanup
content, with the rollback itself recorded as an audit row.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.config.schema import MemoryConfig
from wastech_orchestrator.memory import (
    AuditAction,
    AuditContext,
    CleanupJob,
    DerivedIndex,
    EntityRecord,
    MemoryLayout,
    MemoryService,
    TrustLevel,
)

_AUDIT = AuditContext(timestamp="2026-07-01T00:00:00Z", task_id="cleanup")


def _tier_bytes(service: MemoryService) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in service.tier_files()}


def test_restore_returns_byte_identical_pre_cleanup_state(tmp_path: Path) -> None:
    layout = MemoryLayout(tmp_path / ".worc")
    service = MemoryService(layout, config=MemoryConfig(enabled=True))
    # Two entities, one of which the cleanup will find stale (its path is gone) and quarantine.
    service.append(
        EntityRecord(
            entity_id="keep",
            entity_type="module",
            canonical_name="src/keep.py",
            trust_level=TrustLevel.REPO_OBSERVED,
            paths=("src/keep.py",),
        ),
        audit=_AUDIT,
    )
    service.append(
        EntityRecord(
            entity_id="gone",
            entity_type="module",
            canonical_name="src/gone.py",
            trust_level=TrustLevel.REPO_OBSERVED,
            paths=("src/gone.py",),
        ),
        audit=_AUDIT,
    )
    before = _tier_bytes(service)

    index = DerivedIndex(tmp_path, tracked_paths_provider=lambda _r: frozenset({"src/keep.py"}))
    job = CleanupJob(service, index, MemoryConfig(enabled=True), monotonic=lambda: 0.0)
    report = job.run_once(audit=_AUDIT)
    assert report.quarantined == 1 and report.snapshot is not None
    assert _tier_bytes(service) != before  # the bad cleanup did change the store

    # The store started with no quarantine file; the cleanup first-creates quarantine/pending.jsonl.
    assert (layout.quarantine / "pending.jsonl").is_file()

    restored = service.restore(Path(report.snapshot), audit=_AUDIT)
    assert restored  # files were put back
    after = _tier_bytes(service)
    # Byte-identical for every file present before the cleanup (UTF-8 + explicit \n → stable bytes).
    for name, content in before.items():
        assert after[name] == content, f"{name} not restored byte-identically"
    # F4: a tier file the cleanup first-created (pending.jsonl) is pruned on restore — the store is
    # the exact pre-cleanup set, not a superset with an inert leftover quarantine file.
    assert set(after) == set(before)
    assert not (layout.quarantine / "pending.jsonl").exists()


def test_rollback_is_recorded_as_an_audit_row(tmp_path: Path) -> None:
    layout = MemoryLayout(tmp_path / ".worc")
    service = MemoryService(layout, config=MemoryConfig(enabled=True))
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
    snapshot = service.snapshot(service.tier_files(), label="manual-1")
    service.restore(snapshot, audit=_AUDIT)
    rows = service.audit.rows()
    assert rows[-1]["action"] == AuditAction.ROLLBACK.value
    assert service.audit.verify_chain()  # the append-only chain stays intact across a rollback
