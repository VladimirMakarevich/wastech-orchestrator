"""apply_delta funnel: validate, trust, merge, promote/quarantine, audit."""

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
    Scope,
    TrustLevel,
)
from wastech_orchestrator.memory.service import WriteSource

_TS = "2026-06-30T00:00:00Z"


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    return MemoryService(MemoryLayout(tmp_path / ".worc"))


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
    # A candidate with no evidence is quarantined, never long-term.
    result = _apply(service, CandidateDelta(lessons=(_lesson(ev_type=None),)))
    assert result.quarantined == 1
    assert service.read_long_term(LongTermKind.SEMANTIC) == []
    assert len(service.read_quarantine()) == 1


@pytest.mark.parametrize("ev_type", ["web", "mcp", "mystery"])
def test_external_and_agent_inferred_never_durable(service: MemoryService, ev_type: str) -> None:
    # External-untrusted / agent-inferred candidates quarantine, never long-term.
    _apply(service, CandidateDelta(lessons=(_lesson(ev_type=ev_type),)))
    assert service.read_long_term(LongTermKind.SEMANTIC) == []
    assert len(service.read_quarantine()) == 1


def test_artifact_backed_promotes_only_after_recurrence(service: MemoryService) -> None:
    # Durable-but-not-auto-promote stays short-term until it recurs in a 2nd task.
    delta = CandidateDelta(lessons=(_lesson(ev_type="check", subject="cfg"),))
    _apply(service, delta, task_id="t1")
    assert service.read_long_term(LongTermKind.SEMANTIC) == []  # one short of recurrence
    assert len(service.read_quarantine()) == 1
    _apply(service, delta, task_id="t2")
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1  # recurred -> promoted
    assert service.read_quarantine() == []  # cleared from pending


def test_recurrence_dedups_across_drifting_subject_by_scope(service: MemoryService) -> None:
    # The same lesson recurring under DIFFERENT subject wording but the SAME scope.paths must
    # accumulate recurrence (one memory_id), so a real repeat promotes — the prettier-baseline-drift
    # lesson recurred in 3 tasks under 3 subjects and never promoted because ids diverged.
    def lesson(subject: str) -> CandidateLesson:
        return CandidateLesson(
            kind=LongTermKind.SEMANTIC,
            subject=subject,
            statement="prettier baseline drift",
            evidence=(Evidence("check", "ci"),),
            scope=Scope(paths=("packages/core/a.ts",)),
        )

    _apply(service, CandidateDelta(lessons=(lesson("npm run format baseline"),)), task_id="t1")
    assert service.read_long_term(LongTermKind.SEMANTIC) == []  # 1/2, held short-term
    _apply(service, CandidateDelta(lessons=(lesson("repo-wide Prettier drift"),)), task_id="t2")
    rows = service.read_long_term(LongTermKind.SEMANTIC)
    assert len(rows) == 1  # different subject, shared scope -> recurred -> promoted (not 3 ids)
    assert sorted(rows[0]["seen_task_ids"]) == ["t1", "t2"]


def test_pathless_lesson_still_keys_on_subject(service: MemoryService) -> None:
    # A path-less lesson keeps the subject key: distinct subjects stay distinct.
    _apply(service, CandidateDelta(lessons=(_lesson(subject="alpha"),)), task_id="t1")
    _apply(service, CandidateDelta(lessons=(_lesson(subject="beta"),)), task_id="t2")
    # Two different lessons, each 1/2 -> both held, nothing promoted by spurious recurrence.
    assert service.read_long_term(LongTermKind.SEMANTIC) == []
    assert len(service.read_quarantine()) == 2


def test_merge_keeps_oldest_id_and_unions_evidence(service: MemoryService) -> None:
    first = CandidateLesson(
        kind=LongTermKind.SEMANTIC,
        subject="cfg",
        statement="v1",
        evidence=(Evidence("operator", "r1"),),
    )
    _apply(service, CandidateDelta(lessons=(first,)), task_id="t1")
    original_id = service.read_long_term(LongTermKind.SEMANTIC)[0]["memory_id"]
    second = CandidateLesson(
        kind=LongTermKind.SEMANTIC,
        subject="cfg",
        statement="v2-newer",
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
    # A failed/manual close writes short-term but never promotes (even a strong lesson).
    result = _apply(
        service,
        CandidateDelta(lessons=(_lesson(ev_type="operator"),)),
        source=WriteSource.FAILURE,
    )
    assert len(service.read_episodes()) == 1
    assert service.read_long_term(LongTermKind.SEMANTIC) == []
    assert result.promoted == 0


def test_failure_with_remedy_promotes_via_explained_failure(service: MemoryService) -> None:
    failure = CandidateFailure(signature="boom", remedy="do y", evidence=(Evidence("check", "ci"),))
    result = _apply(service, CandidateDelta(failures=(failure,)))
    assert result.promoted == 1
    rows = service.read_long_term(LongTermKind.FAILURE)
    assert len(rows) == 1
    assert rows[0]["remedy"] == "do y"


def _indexed_service(tmp_path: Path, tracked: set[str]) -> MemoryService:
    """A service whose write funnel validates entity paths against an injected tracked-path set."""
    index = DerivedIndex(tmp_path, tracked_paths_provider=lambda _r: frozenset(tracked))
    return MemoryService(MemoryLayout(tmp_path / ".worc"), index=index)


def test_entity_verified_path_stored_missing_or_pathless_quarantined(tmp_path: Path) -> None:
    # With a DerivedIndex wired, an entity card earns durable repo-observed only when its
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


def test_entity_cards_dedupe_by_path_and_accumulate_task_ids(tmp_path: Path) -> None:
    # Two cards for the same file — with different LLM-authored entity_ids (wording drift) —
    # merge into ONE card keyed by canonical path, and last_seen_task_ids accumulates every task.
    service = _indexed_service(tmp_path, tracked={"src/mod.py"})
    _apply(
        service,
        CandidateDelta(
            entities=(
                CandidateEntity(entity_id="core-mod", entity_type="module", paths=("src/mod.py",)),
            )
        ),
        task_id="t1",
    )
    _apply(
        service,
        CandidateDelta(
            entities=(
                CandidateEntity(entity_id="mod", entity_type="module", paths=("src/mod.py",)),
            )
        ),
        task_id="t2",
    )
    entities = service.read_entities()
    assert len(entities) == 1
    assert entities[0]["canonical_name"] == "src/mod.py"
    assert list(entities[0]["last_seen_task_ids"]) == ["t1", "t2"]


def test_merge_audit_names_scope_key_and_single_id(service: MemoryService) -> None:
    # Merging into an existing active record names the real dedup key (kind+scope.paths), not
    # "same subject", and scopes affected_ids to just the merged record (not every row in the file).
    lesson = CandidateLesson(
        kind=LongTermKind.SEMANTIC,
        subject="cfg",
        statement="x",
        evidence=(Evidence(type="operator", ref="op"),),  # human-curated -> auto-promotes to active
        scope=Scope(paths=("src/a.py",)),
    )
    _apply(service, CandidateDelta(lessons=(lesson,)), task_id="t1")  # promote
    _apply(service, CandidateDelta(lessons=(lesson,)), task_id="t2")  # merge into the active record
    merge_rows = [r for r in service.audit.rows() if r.get("action") == "merge"]
    assert merge_rows
    row = merge_rows[-1]
    assert "same kind+scope.paths" in row.get("rationale", "")
    assert "same subject" not in row.get("rationale", "")
    assert len(row.get("affected_ids") or []) == 1


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


def test_every_audit_row_carries_nonempty_rationale(service: MemoryService) -> None:
    # Every mutation records a human-readable rationale beside the pre/post
    # hashes, so `worc memory show/validate` explains WHY each record was appended / quarantined.
    _apply(service, CandidateDelta(lessons=(_lesson(ev_type="operator"),)))  # promotes
    rows = service.audit.rows()
    assert rows and all((row.get("rationale") or "").strip() for row in rows)
    assert any("promoted to long-term" in row.get("rationale", "") for row in rows)


def test_quarantine_rationale_names_the_cause(service: MemoryService) -> None:
    # A non-durable candidate is held with a concrete deterministic cause (not an empty string).
    _apply(service, CandidateDelta(lessons=(_lesson(ev_type="agent"),)))  # agent-inferred → held
    rows = service.audit.rows()
    quarantine_rows = [r for r in rows if r.get("action") == "quarantine"]
    assert quarantine_rows
    assert any("non-durable trust" in r.get("rationale", "") for r in quarantine_rows)


def test_missing_evidence_quarantine_rationale(service: MemoryService) -> None:
    # A no-evidence candidate names evidence as the cause.
    _apply(service, CandidateDelta(lessons=(_lesson(ev_type=None),)))
    rows = service.audit.rows()
    assert any(
        r.get("action") == "quarantine" and "no supporting evidence" in r.get("rationale", "")
        for r in rows
    )


def test_disabled_window_does_not_block_recurrence_within_default(service: MemoryService) -> None:
    # Two same-day occurrences are within the default 60d window, so recurrence counts.
    for task in ("t1", "t2"):
        _apply(
            service,
            CandidateDelta(lessons=(_lesson(ev_type="check", subject="win"),)),
            task_id=task,
        )
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1
