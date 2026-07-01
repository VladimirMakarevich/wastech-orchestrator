"""Safety drill 05.3 — removed/renamed targets are detected and quarantined, never deleted (AC-C4).

Adversarial: seed entity cards, then make their referenced files vanish or move, run the real
``CleanupJob`` pass, and prove the design §5 / Q2 rule — rename-remap first, else mark-stale →
quarantine, **never** a silent delete, and never a judgment-based drop.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.config.schema import MemoryConfig
from wastech_orchestrator.memory import (
    AuditContext,
    CleanupJob,
    DerivedIndex,
    EntityRecord,
    LongTermKind,
    LongTermRecord,
    MemoryLayout,
    MemoryService,
    Scope,
    TrustLevel,
)

_AUDIT = AuditContext(timestamp="2026-07-01T00:00:00Z", task_id="cleanup")


def _service(tmp_path: Path) -> MemoryService:
    return MemoryService(MemoryLayout.for_repo(tmp_path), config=MemoryConfig(enabled=True))


def _entity(service: MemoryService, entity_id: str, paths: tuple[str, ...]) -> None:
    service.append(
        EntityRecord(
            entity_id=entity_id, entity_type="module", canonical_name=paths[0],
            trust_level=TrustLevel.REPO_OBSERVED, paths=paths,
        ),
        audit=_AUDIT,
    )


def _lesson(service: MemoryService, memory_id: str, *, paths: tuple[str, ...] = ()) -> None:
    service.append(
        LongTermRecord(
            memory_id=memory_id, kind=LongTermKind.SEMANTIC, subject=memory_id, statement="keep me",
            trust_level=TrustLevel.HUMAN_CURATED, scope=Scope(paths=paths),
        ),
        audit=_AUDIT,
    )


def _job(service: MemoryService, tracked: set[str], repo: Path) -> CleanupJob:
    index = DerivedIndex(repo, tracked_paths_provider=lambda _r: frozenset(tracked))
    return CleanupJob(service, index, MemoryConfig(enabled=True), monotonic=lambda: 0.0)


def test_removed_module_is_quarantined_not_deleted(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _entity(service, "gone", ("src/removed.py",))
    # The repo no longer tracks (and the disk no longer holds) that path.
    report = _job(service, tracked=set(), repo=tmp_path).run_once(audit=_AUDIT)
    assert report.quarantined == 1 and report.remapped == 0
    assert service.read_entities() == []  # removed from the active tier...
    pending = service.read_quarantine()
    assert [row["entity_id"] for row in pending] == ["gone"]  # ...preserved in quarantine, not gone
    assert pending[0]["status"] == "quarantined"


def test_renamed_module_is_remapped_not_quarantined(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _entity(service, "moved", ("src/old/widget.py",))
    # The file moved: a single same-basename tracked candidate exists.
    report = _job(service, tracked={"src/new/widget.py"}, repo=tmp_path).run_once(audit=_AUDIT)
    assert report.remapped == 1 and report.quarantined == 0
    assert service.read_entities()[0]["paths"] == ["src/new/widget.py"]
    assert service.read_quarantine() == []


def test_ambiguous_move_quarantines_rather_than_guessing(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _entity(service, "ambiguous", ("src/old/util.py",))
    # Two same-basename candidates → no confident remap → quarantine (fail-closed), never a guess.
    tracked = {"src/a/util.py", "src/b/util.py"}
    report = _job(service, tracked=tracked, repo=tmp_path).run_once(audit=_AUDIT)
    assert report.quarantined == 1 and report.remapped == 0
    assert [row["entity_id"] for row in service.read_quarantine()] == ["ambiguous"]


def test_pathless_lesson_is_never_dropped_on_judgment(tmp_path: Path) -> None:
    # The auto-drop boundary: a lesson with no scope.paths has no existence signal, so cleanup never
    # drops it on judgment (the contradiction-based drop is a deferred V2 item, no ledger in V1).
    service = _service(tmp_path)
    _lesson(service, "ltm_live")  # no scope.paths
    report = _job(service, tracked={"src/present.py"}, repo=tmp_path).run_once(audit=_AUDIT)
    active = service.read_long_term(LongTermKind.SEMANTIC)
    assert report.quarantined == 0
    assert len(active) == 1 and active[0]["memory_id"] == "ltm_live"


def test_lesson_scoped_to_present_path_is_kept(tmp_path: Path) -> None:
    # F2: a lesson whose scope.paths still exist is left fully intact (active, not quarantined).
    service = _service(tmp_path)
    _lesson(service, "ltm_live", paths=("src/present.py",))
    report = _job(service, tracked={"src/present.py"}, repo=tmp_path).run_once(audit=_AUDIT)
    active = service.read_long_term(LongTermKind.SEMANTIC)
    assert report.quarantined == 0
    assert [row["memory_id"] for row in active] == ["ltm_live"]
    assert service.read_quarantine() == []


def test_lesson_scoped_to_vanished_path_is_quarantined_not_deleted(tmp_path: Path) -> None:
    # F2: a lesson scoped to a since-removed path is quarantined (kept), never silently deleted.
    service = _service(tmp_path)
    _lesson(service, "ltm_stale", paths=("src/removed.py",))
    report = _job(service, tracked=set(), repo=tmp_path).run_once(audit=_AUDIT)
    assert report.quarantined == 1
    assert service.read_long_term(LongTermKind.SEMANTIC) == []  # removed from the active tier...
    pending = service.read_quarantine()
    assert [row["memory_id"] for row in pending] == ["ltm_stale"]  # ...preserved in quarantine
    assert pending[0]["status"] == "quarantined"
