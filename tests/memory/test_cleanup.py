"""CleanupJob (04.2): bounded, snapshotted, audited maintenance — never promotes, never edits code.

Model-free throughout: a fake tracked-paths provider feeds DerivedIndex; time is injected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import MemoryConfig
from wastech_orchestrator.memory import (
    AuditContext,
    CleanupJob,
    DerivedIndex,
    EntityRecord,
    EpisodeRecord,
    LongTermKind,
    LongTermRecord,
    MemoryLayout,
    MemoryService,
    TrustLevel,
)

_AUDIT = AuditContext(timestamp="2026-06-30T00:00:00Z")


@pytest.fixture
def layout(tmp_path: Path) -> MemoryLayout:
    return MemoryLayout.for_repo(tmp_path)


def _service(layout: MemoryLayout) -> MemoryService:
    return MemoryService(layout)


def _index(repo_root: Path, tracked: set[str]) -> DerivedIndex:
    return DerivedIndex(repo_root, tracked_paths_provider=lambda _r: frozenset(tracked))


def _job(
    service: MemoryService, index: DerivedIndex, **cfg: object
) -> CleanupJob:
    config = MemoryConfig(enabled=True, **cfg)  # type: ignore[arg-type]
    # Freeze wall-clock at 0 so only the scan/edit caps gate the pass deterministically.
    return CleanupJob(service, index, config, monotonic=lambda: 0.0)


def _episode(service: MemoryService, ep_id: str, created_at: str) -> None:
    service.append(
        EpisodeRecord(
            id=ep_id, task_id=ep_id, created_at=created_at, trust_level=TrustLevel.ARTIFACT_BACKED
        ),
        audit=_AUDIT,
    )


def _entity(service: MemoryService, entity_id: str, paths: tuple[str, ...]) -> None:
    service.append(
        EntityRecord(
            entity_id=entity_id,
            entity_type="module",
            canonical_name=paths[0] if paths else entity_id,
            trust_level=TrustLevel.REPO_OBSERVED,
            paths=paths,
        ),
        audit=_AUDIT,
    )


def _lesson(service: MemoryService, memory_id: str, subject: str) -> None:
    service.append(
        LongTermRecord(
            memory_id=memory_id,
            kind=LongTermKind.SEMANTIC,
            subject=subject,
            statement="s",
            trust_level=TrustLevel.HUMAN_CURATED,
        ),
        audit=_AUDIT,
    )


# -- TTL expiry ---------------------------------------------------------------


def test_expires_episodes_past_ttl_only(layout: MemoryLayout) -> None:
    service = _service(layout)
    _episode(service, "old", "2026-01-01T00:00:00Z")  # > 30d before now
    _episode(service, "fresh", "2026-06-29T00:00:00Z")  # within ttl
    report = _job(service, _index(layout.root.parent.parent, set())).run_once(audit=_AUDIT)
    assert report.expired == 1
    ids = {row["id"] for row in service.read_episodes()}
    assert ids == {"fresh"}


# -- entity staleness (AC-C4) -------------------------------------------------


def test_stale_entity_is_quarantined_never_deleted(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    service = _service(layout)
    _entity(service, "e1", ("src/gone.py",))  # path neither tracked nor on disk
    report = _job(service, _index(tmp_path, set())).run_once(audit=_AUDIT)
    assert report.quarantined == 1
    assert service.read_entities() == []  # removed from the active tier...
    pending = service.read_quarantine()
    assert len(pending) == 1 and pending[0]["entity_id"] == "e1"  # ...but moved, not deleted
    assert pending[0]["status"] == "quarantined"


def test_moved_entity_is_remapped_by_basename(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    service = _service(layout)
    _entity(service, "e1", ("src/old/foo.py",))
    index = _index(tmp_path, {"src/new/foo.py"})  # the file moved (same basename, one candidate)
    report = _job(service, index).run_once(audit=_AUDIT)
    assert report.remapped == 1 and report.quarantined == 0
    entities = service.read_entities()
    assert entities[0]["paths"] == ["src/new/foo.py"]  # remapped, stays active
    assert service.read_quarantine() == []


def test_present_entity_is_left_alone(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    service = _service(layout)
    _entity(service, "e1", ("src/a.py",))
    report = _job(service, _index(tmp_path, {"src/a.py"})).run_once(audit=_AUDIT)
    assert report.remapped == 0 and report.quarantined == 0
    assert len(service.read_entities()) == 1


# -- duplicate merge ----------------------------------------------------------


def test_duplicate_long_term_lessons_are_merged(layout: MemoryLayout) -> None:
    service = _service(layout)
    _lesson(service, "m1", "Always run ruff")
    _lesson(service, "m2", "always   run RUFF")  # same normalized subject
    report = _job(service, _index(layout.root.parent.parent, set())).run_once(audit=_AUDIT)
    assert report.merged == 1
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1


# -- bounded autonomy (AC-C3, §7) ---------------------------------------------


def test_never_promotes_and_keeps_promoted_zero(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    service = _service(layout)
    _entity(service, "e1", ("src/gone.py",))
    before = len(service.read_long_term(LongTermKind.SEMANTIC))
    report = _job(service, _index(tmp_path, set())).run_once(audit=_AUDIT)
    assert report.promoted == 0  # cleanup never creates a long-term lesson
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == before


def test_edit_budget_is_respected(layout: MemoryLayout) -> None:
    service = _service(layout)
    for i in range(5):
        # five stale episodes (all far past the TTL) but max_edits=2 → only two expired this pass
        _episode(service, f"e{i}", f"2026-01-0{i + 1}T00:00:00Z")
    report = _job(
        service, _index(layout.root.parent.parent, set()), cleanup_max_edits=2
    ).run_once(audit=_AUDIT)
    assert report.expired == 2
    assert len(service.read_episodes()) == 3  # three left for a later pass


def test_snapshot_precedes_the_batch(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    service = _service(layout)
    _entity(service, "e1", ("src/gone.py",))
    report = _job(service, _index(tmp_path, set())).run_once(audit=_AUDIT)
    assert report.snapshot is not None
    assert Path(report.snapshot).is_dir()  # the pre-batch snapshot exists (AC-SF4 groundwork)


def test_empty_store_is_a_noop(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    service = _service(layout)
    report = _job(service, _index(tmp_path, set())).run_once(audit=_AUDIT)
    assert report.ran is False  # nothing on disk → no snapshot, no work
