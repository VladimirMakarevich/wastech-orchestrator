"""PacketBuilder (03.1/03.2/03.5): deterministic filter + ranking, caps, empty state.

The read path is model-free: every test builds packets from a store written by the service — no
router, no model anywhere (AC-R3 reproducibility, AC-R2 caps, AC-R4 empty state, AC-R1 path-only).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import MemoryConfig
from wastech_orchestrator.memory import (
    AuditContext,
    CandidateDelta,
    CandidateLesson,
    EntityRecord,
    EpisodeRecord,
    Evidence,
    LongTermKind,
    LongTermRecord,
    MemoryLayout,
    MemoryService,
    PacketBuilder,
    PacketContext,
    Scope,
    TrustLevel,
)
from wastech_orchestrator.memory.service import WriteSource

_AUDIT = AuditContext(timestamp="2026-06-30T00:00:00Z")
_NO_SCOPE = Scope()  # module-level singleton (avoids a call in a default argument)


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    return MemoryService(MemoryLayout.for_repo(tmp_path))


def _builder(service: MemoryService, **overrides: object) -> PacketBuilder:
    config = MemoryConfig(enabled=True, **overrides)  # type: ignore[arg-type]
    return PacketBuilder(service, config)


def _lesson(
    memory_id: str,
    *,
    kind: LongTermKind = LongTermKind.SEMANTIC,
    statement: str = "x",
    trust: TrustLevel = TrustLevel.REPO_OBSERVED,
    scope: Scope = _NO_SCOPE,
    last_verified_at: str = "2026-06-30T00:00:00Z",
) -> LongTermRecord:
    return LongTermRecord(
        memory_id=memory_id,
        kind=kind,
        subject=memory_id,
        statement=statement,
        trust_level=trust,
        scope=scope,
        evidence=(Evidence(type="repo_doc", ref="README.md"),),
        last_verified_at=last_verified_at,
    )


# -- selection / ranking (03.1) -----------------------------------------------


def test_build_is_deterministic(service: MemoryService) -> None:
    # AC-R3: same inputs → byte-identical packet (stable sort, no clock/randomness in ranking).
    for i in range(4):
        service.append(_lesson(f"m{i}", statement=f"lesson {i}"), audit=_AUDIT)
    builder = _builder(service)
    ctx = PacketContext(node_id="planning")
    first = builder.build(ctx)
    second = builder.build(ctx)
    assert first == second
    assert builder.render(first) == builder.render(second)


def test_ranking_is_trust_weighted(service: MemoryService) -> None:
    service.append(_lesson("low", trust=TrustLevel.ARTIFACT_BACKED), audit=_AUDIT)
    service.append(_lesson("high", trust=TrustLevel.HUMAN_CURATED), audit=_AUDIT)
    packet = _builder(service).build(PacketContext(node_id="planning"))
    assert [row["memory_id"] for row in packet.long_term] == ["high", "low"]


def test_ranking_prefers_path_overlap(service: MemoryService) -> None:
    service.append(_lesson("unrelated"), audit=_AUDIT)
    service.append(_lesson("scoped", scope=Scope(paths=("src/core/x.py",))), audit=_AUDIT)
    packet = _builder(service).build(
        PacketContext(node_id="implementation", touched_paths=("src/core/x.py",))
    )
    assert packet.long_term[0]["memory_id"] == "scoped"


def _hold_lesson(service: MemoryService, *, subject: str, ev_type: str) -> None:
    """Apply one lesson so it lands 'held' in quarantine (durable at 1/2, or non-durable)."""
    service.apply_delta(
        CandidateDelta(
            lessons=(
                CandidateLesson(
                    kind=LongTermKind.REVIEWER,
                    subject=subject,
                    statement="expect X",
                    evidence=(Evidence(type=ev_type, ref="ref"),),
                ),
            )
        ),
        episode=EpisodeRecord(
            id="ep_t1",
            task_id="t1",
            created_at=_AUDIT.timestamp,
            trust_level=TrustLevel.ARTIFACT_BACKED,
        ),
        source=WriteSource.SUCCESS,
        audit=_AUDIT,
    )


def test_durable_held_quarantine_lesson_is_surfaced(service: MemoryService) -> None:
    # F43: a durable lesson still held in quarantine (awaiting recurrence) is real repo knowledge —
    # the packet surfaces it instead of leaving it write-only.
    _hold_lesson(service, subject="rv", ev_type="check")  # artifact-backed → durable, held 1/2
    assert service.read_long_term(LongTermKind.REVIEWER) == []  # held, not promoted
    assert len(service.read_quarantine()) == 1
    packet = _builder(service).build(PacketContext(node_id="review"))
    assert [row["subject"] for row in packet.long_term] == ["rv"]


def test_agent_inferred_quarantine_is_not_surfaced(service: MemoryService) -> None:
    # F43: only DURABLE-trust held lessons are surfaced; external/agent-inferred stays invisible.
    _hold_lesson(service, subject="ai", ev_type="web")  # external-untrusted
    assert len(service.read_quarantine()) == 1
    packet = _builder(service).build(PacketContext(node_id="review"))
    assert packet.long_term == ()


def test_episode_with_content_renders_a_nonempty_bullet(service: MemoryService) -> None:
    # F47: an episode carrying touched_paths + stage_outcomes renders content, not a bare bullet.
    service.append(
        EpisodeRecord(
            id="ep_t1",
            task_id="t1",
            created_at=_AUDIT.timestamp,
            trust_level=TrustLevel.ARTIFACT_BACKED,
            stage_outcomes={"task": "done"},
            touched_paths=("src/mod.py",),
        ),
        audit=_AUDIT,
    )
    builder = _builder(service)
    rendered = builder.render(builder.build(PacketContext(node_id="planning")))
    assert "task=done" in rendered
    assert "src/mod.py" in rendered


def test_lesson_node_scope_is_honored(service: MemoryService) -> None:
    # A lesson scoped to specific nodes is excluded from a node it does not name.
    service.append(_lesson("review_only", scope=Scope(nodes=("review",))), audit=_AUDIT)
    service.append(_lesson("anywhere"), audit=_AUDIT)
    builder = _builder(service)
    planning = builder.build(PacketContext(node_id="planning"))
    review = builder.build(PacketContext(node_id="review"))
    assert {row["memory_id"] for row in planning.long_term} == {"anywhere"}
    assert {row["memory_id"] for row in review.long_term} == {"anywhere", "review_only"}


def test_review_prefers_reviewer_lessons(service: MemoryService) -> None:
    # Equal trust: the review node ranks a reviewer-kind lesson above a plain semantic one.
    service.append(_lesson("sem", kind=LongTermKind.SEMANTIC), audit=_AUDIT)
    service.append(_lesson("rev", kind=LongTermKind.REVIEWER), audit=_AUDIT)
    review = _builder(service).build(PacketContext(node_id="review"))
    assert review.long_term[0]["memory_id"] == "rev"
    # ... while a non-review node keeps the stable id order (no reviewer preference).
    planning = _builder(service).build(PacketContext(node_id="planning"))
    assert planning.long_term[0]["memory_id"] == "rev"  # "rev" < "sem" by id tiebreak


# -- caps (03.2 / AC-R2) ------------------------------------------------------


def test_count_caps_are_enforced(service: MemoryService) -> None:
    for i in range(6):
        service.append(_lesson(f"m{i}"), audit=_AUDIT)
    packet = _builder(service, packet_max_long_term=3).build(PacketContext(node_id="planning"))
    assert len(packet.long_term) == 3


def test_implementation_gets_more_entity_cards(service: MemoryService) -> None:
    for i in range(8):
        service.append(
            EntityRecord(
                entity_id=f"e{i}",
                entity_type="module",
                canonical_name=f"src/m{i}.py",
                trust_level=TrustLevel.REPO_OBSERVED,
                paths=(f"src/m{i}.py",),
            ),
            audit=_AUDIT,
        )
    builder = _builder(service, packet_max_entity=5)
    # base cap (5) elsewhere, +2 bump on implementation (7).
    assert len(builder.build(PacketContext(node_id="planning")).entities) == 5
    assert len(builder.build(PacketContext(node_id="implementation")).entities) == 7


def test_line_backstop_drops_whole_records_never_partial(service: MemoryService) -> None:
    # AC-R2 / NFR4: over the line backstop, whole lowest-ranked records are dropped (episode first),
    # never a truncated record. A tiny cap forces the drop; what survives renders in full.
    service.append(_lesson("keep", statement="durable lesson"), audit=_AUDIT)
    service.append(
        EpisodeRecord(
            id="ep1",
            task_id="t1",
            created_at="2026-06-30T00:00:00Z",
            trust_level=TrustLevel.ARTIFACT_BACKED,
            touched_paths=("src/a.py",),
        ),
        audit=_AUDIT,
    )
    # Header + blank + "## Lessons" + 1 bullet = 4 lines for the lesson alone; the episode section
    # would push it past 5, so the episode (lowest tier) is dropped whole.
    builder = _builder(service, packet_max_lines=5)
    rendered = builder.render(builder.build(PacketContext(node_id="planning")))
    assert "durable lesson" in rendered  # the surviving lesson is rendered in full
    assert "## Recent episodes" not in rendered  # the episode was dropped whole, not truncated
    assert len(rendered.splitlines()) <= 5


# -- empty state (03.5 / AC-R4) -----------------------------------------------


def test_empty_store_yields_empty_packet(service: MemoryService) -> None:
    packet = _builder(service).build(PacketContext(node_id="planning"))
    assert packet.is_empty


def test_write_packet_writes_no_file_when_empty(service: MemoryService, tmp_path: Path) -> None:
    # AC-R4: a node with no relevant memory gets NO packet file (so {memory_path} renders empty).
    dest = tmp_path / "logs" / "t1" / "memory" / "planning.md"
    result = _builder(service).write_packet(
        node_id="planning", task_type=None, touched_paths=(), dest=dest
    )
    assert result is None
    assert not dest.exists()


def test_write_packet_writes_file_atomically(service: MemoryService, tmp_path: Path) -> None:
    service.append(_lesson("m1", statement="real lesson"), audit=_AUDIT)
    dest = tmp_path / "logs" / "t1" / "memory" / "planning.md"
    result = _builder(service).write_packet(
        node_id="planning", task_type=None, touched_paths=(), dest=dest
    )
    assert result == dest
    assert dest.read_text(encoding="utf-8").startswith("# Repository memory")
    assert "real lesson" in dest.read_text(encoding="utf-8")
    assert not list(dest.parent.glob("*.tmp"))  # atomic: no partial temp file
