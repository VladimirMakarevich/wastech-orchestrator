"""apply_delta funnel (02.4/02.5): validate, trust, merge, promote/quarantine, audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.memory import (
    AuditContext,
    CandidateDelta,
    CandidateEntity,
    CandidateFailure,
    CandidateLesson,
    DerivedIndex,
    EpisodeRecord,
    Evidence,
    LongTermKind,
    MemoryLayout,
    MemoryService,
    TrustLevel,
)
from wastech_orchestrator.memory.service import WriteSource

_TS = "2026-06-30T00:00:00Z"


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    return MemoryService(MemoryLayout.for_repo(tmp_path))


def _episode(task_id: str = "t1") -> EpisodeRecord:
    return EpisodeRecord(
        id=f"ep_{task_id}", task_id=task_id, created_at=_TS, trust_level=TrustLevel.ARTIFACT_BACKED
    )


def _lesson(*, ev_type: str | None = "check", subject: str = "s") -> CandidateLesson:
    evidence = (Evidence(type=ev_type, ref="r"),) if ev_type else ()
    return CandidateLesson(
        kind=LongTermKind.SEMANTIC, subject=subject, statement="do x", evidence=evidence
    )


def _apply(
    service: MemoryService,
    delta: CandidateDelta | None,
    *,
    task_id: str = "t1",
    source: WriteSource = WriteSource.SUCCESS,
    ts: str = _TS,
):
    return service.apply_delta(
        delta, episode=_episode(task_id), source=source, audit=AuditContext(timestamp=ts)
    )


def test_episode_is_always_written(service: MemoryService) -> None:
    result = _apply(service, None)
    assert len(service.read_episodes()) == 1
    assert result.episode_id == "ep_t1"


def test_human_curated_lesson_auto_promotes(service: MemoryService) -> None:
    result = _apply(service, CandidateDelta(lessons=(_lesson(ev_type="operator"),)))
    assert result.promoted == 1
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1
    assert service.read_long_term(LongTermKind.SEMANTIC)[0]["trust_level"] == "human-curated"


def test_missing_evidence_is_quarantined_never_promoted(service: MemoryService) -> None:
    # AC-W2: a candidate with no evidence is quarantined, never long-term.
    result = _apply(service, CandidateDelta(lessons=(_lesson(ev_type=None),)))
    assert result.quarantined == 1
    assert service.read_long_term(LongTermKind.SEMANTIC) == []
    assert len(service.read_quarantine()) == 1


@pytest.mark.parametrize("ev_type", ["web", "mcp", "mystery"])
def test_external_and_agent_inferred_never_durable(service: MemoryService, ev_type: str) -> None:
    # AC-SF2/AC-W4: external-untrusted / agent-inferred candidates quarantine, never long-term.
    _apply(service, CandidateDelta(lessons=(_lesson(ev_type=ev_type),)))
    assert service.read_long_term(LongTermKind.SEMANTIC) == []
    assert len(service.read_quarantine()) == 1


def test_artifact_backed_promotes_only_after_recurrence(service: MemoryService) -> None:
    # Q3: durable-but-not-auto-promote stays short-term until it recurs in a 2nd task.
    delta = CandidateDelta(lessons=(_lesson(ev_type="check", subject="cfg"),))
    _apply(service, delta, task_id="t1")
    assert service.read_long_term(LongTermKind.SEMANTIC) == []  # one short of recurrence
    assert len(service.read_quarantine()) == 1
    _apply(service, delta, task_id="t2")
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1  # recurred -> promoted
    assert service.read_quarantine() == []  # cleared from pending


def test_merge_keeps_oldest_id_and_unions_evidence(service: MemoryService) -> None:
    first = CandidateLesson(
        kind=LongTermKind.SEMANTIC, subject="cfg", statement="v1",
        evidence=(Evidence("operator", "r1"),),
    )
    _apply(service, CandidateDelta(lessons=(first,)), task_id="t1")
    original_id = service.read_long_term(LongTermKind.SEMANTIC)[0]["memory_id"]
    second = CandidateLesson(
        kind=LongTermKind.SEMANTIC, subject="cfg", statement="v2-newer",
        evidence=(Evidence("operator", "r2"),),
    )
    result = _apply(service, CandidateDelta(lessons=(second,)), task_id="t2")
    rows = service.read_long_term(LongTermKind.SEMANTIC)
    assert result.merged == 1
    assert len(rows) == 1  # merged, not duplicated
    assert rows[0]["memory_id"] == original_id  # oldest id kept
    assert rows[0]["statement"] == "v2-newer"  # newest wording
    assert len(rows[0]["evidence"]) == 2  # evidence unioned


def test_failure_source_writes_episode_but_never_long_term(service: MemoryService) -> None:
    # AC-W3: a failed/manual close writes short-term but never promotes (even a strong lesson).
    result = _apply(
        service,
        CandidateDelta(lessons=(_lesson(ev_type="operator"),)),
        source=WriteSource.FAILURE,
    )
    assert len(service.read_episodes()) == 1
    assert service.read_long_term(LongTermKind.SEMANTIC) == []
    assert result.promoted == 0


def test_failure_with_remedy_promotes_via_explained_failure(service: MemoryService) -> None:
    failure = CandidateFailure(
        signature="boom", remedy="do y", evidence=(Evidence("check", "ci"),)
    )
    result = _apply(service, CandidateDelta(failures=(failure,)))
    assert result.promoted == 1
    rows = service.read_long_term(LongTermKind.FAILURE)
    assert len(rows) == 1
    assert rows[0]["remedy"] == "do y"


def _indexed_service(tmp_path: Path, tracked: set[str]) -> MemoryService:
    """A service whose write funnel validates entity paths against an injected tracked-path set."""
    index = DerivedIndex(tmp_path, tracked_paths_provider=lambda _r: frozenset(tracked))
    return MemoryService(MemoryLayout.for_repo(tmp_path), index=index)


def test_entity_verified_path_stored_missing_or_pathless_quarantined(tmp_path: Path) -> None:
    # F1/NFR2: with a DerivedIndex wired, an entity card earns durable repo-observed only when its
    # paths verify present in the live repo. A hallucinated/gone path is downgraded -> quarantine;
    # a path-less card is quarantined as before. Nothing is ever silently deleted.
    service = _indexed_service(tmp_path, tracked={"src/real.py"})
    delta = CandidateDelta(
        entities=(
            CandidateEntity(entity_id="module:real", entity_type="module", paths=("src/real.py",)),
            CandidateEntity(
                entity_id="module:gone", entity_type="module", paths=("src/hallucinated.py",)
            ),
            CandidateEntity(entity_id="ctx:b", entity_type="context"),  # no paths -> quarantine
        )
    )
    _apply(service, delta)
    entities = service.read_entities()
    assert [row["entity_id"] for row in entities] == ["module:real"]
    assert entities[0]["trust_level"] == "repo-observed"
    quarantined = {row.get("entity_id") for row in service.read_quarantine()}
    assert quarantined == {"module:gone", "ctx:b"}


def test_entity_without_index_stores_any_named_path(service: MemoryService) -> None:
    # Back-compat: a service built with no DerivedIndex (read-only/legacy callers) skips the
    # existence check — a card naming >= 1 path is still stored, a path-less card quarantined.
    delta = CandidateDelta(
        entities=(
            CandidateEntity(entity_id="module:a", entity_type="module", paths=("src/a.py",)),
            CandidateEntity(entity_id="ctx:b", entity_type="context"),  # no paths -> quarantine
        )
    )
    _apply(service, delta)
    entities = service.read_entities()
    assert len(entities) == 1
    assert entities[0]["entity_id"] == "module:a"
    assert any(row.get("entity_id") == "ctx:b" for row in service.read_quarantine())


def test_every_mutation_is_audited_and_chain_holds(service: MemoryService) -> None:
    _apply(service, CandidateDelta(lessons=(_lesson(ev_type="operator"),)))
    rows = service.audit.rows()
    # At least the episode append + the promoted-lesson append.
    assert len(rows) >= 2
    assert service.audit.verify_chain() is True
    assert all(row.get("post_hash") for row in rows)


def test_disabled_window_does_not_block_recurrence_within_default(service: MemoryService) -> None:
    # Two same-day occurrences are within the default 60d window, so recurrence counts.
    for task in ("t1", "t2"):
        _apply(
            service,
            CandidateDelta(lessons=(_lesson(ev_type="check", subject="win"),)),
            task_id=task,
        )
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1
