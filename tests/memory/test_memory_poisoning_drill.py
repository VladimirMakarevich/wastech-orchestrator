"""Safety drill — low-trust memory never auto-promotes or outranks trusted.

Adversarial: feed ``external-untrusted`` / ``agent-inferred`` candidates through the real funnel and
prove they are quarantined (never durable long-term), never reach a packet, never outrank a trusted
repo-backed record, and never silently overwrite active memory on a contradiction.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.config.schema import MemoryConfig
from wastech_orchestrator.memory import (
    AuditContext,
    CandidateDelta,
    CandidateLesson,
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
    WriteSource,
)

_AUDIT = AuditContext(timestamp="2026-07-01T00:00:00Z", task_id="t1")


def _service(tmp_path: Path) -> MemoryService:
    return MemoryService(MemoryLayout(tmp_path / ".worc"), config=MemoryConfig(enabled=True))


def _episode() -> EpisodeRecord:
    return EpisodeRecord(
        id="ep_t1",
        task_id="t1",
        created_at="2026-07-01T00:00:00Z",
        trust_level=TrustLevel.ARTIFACT_BACKED,
    )


def test_external_and_inferred_candidates_are_quarantined_never_durable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    delta = CandidateDelta(
        lessons=(
            CandidateLesson(  # external-untrusted (web evidence) — never auto
                kind=LongTermKind.SEMANTIC,
                subject="web claim",
                statement="trust the internet",
                evidence=(Evidence(type="web", ref="https://evil.example"),),
                trust_hint="repo-observed",  # advisory lie — must be ignored
            ),
            CandidateLesson(  # agent-inferred (no recognized evidence)
                kind=LongTermKind.SEMANTIC,
                subject="guess",
                statement="probably fine",
                evidence=(),
            ),
        ),
    )
    service.apply_delta(delta, episode=_episode(), source=WriteSource.SUCCESS, audit=_AUDIT)

    assert service.read_long_term(LongTermKind.SEMANTIC) == []  # nothing durable
    pending = service.read_quarantine()
    assert {row["subject"] for row in pending} == {"web claim", "guess"}
    # The advisory trust_hint was ignored — the service assigned the real (non-durable) trust.
    trust = {row["subject"]: row["trust_level"] for row in pending}
    assert trust["web claim"] == TrustLevel.EXTERNAL_UNTRUSTED.value
    assert trust["guess"] == TrustLevel.AGENT_INFERRED.value


def test_quarantined_low_trust_never_reaches_a_packet(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.apply_delta(
        CandidateDelta(
            lessons=(
                CandidateLesson(
                    kind=LongTermKind.SEMANTIC,
                    subject="web claim",
                    statement="trust the internet",
                    evidence=(Evidence(type="web", ref="https://evil.example"),),
                ),
            )
        ),
        episode=_episode(),
        source=WriteSource.SUCCESS,
        audit=_AUDIT,
    )
    config = MemoryConfig(enabled=True)
    packet = PacketBuilder(service, config).build(PacketContext(node_id="implementation"))
    assert packet.long_term == ()  # quarantine is never read into a packet


def test_trusted_record_always_outranks_low_trust_in_a_packet(tmp_path: Path) -> None:
    service = _service(tmp_path)
    # Two ACTIVE records (e.g. hand-edited into the tier file): even if the low-trust one has the
    # stronger path overlap, trust is the dominant ranking key, so trusted sorts first.
    service.append(
        LongTermRecord(
            memory_id="trusted",
            kind=LongTermKind.SEMANTIC,
            subject="trusted",
            statement="t",
            trust_level=TrustLevel.HUMAN_CURATED,
        ),
        audit=_AUDIT,
    )
    service.append(
        LongTermRecord(
            memory_id="weak",
            kind=LongTermKind.SEMANTIC,
            subject="weak",
            statement="w",
            trust_level=TrustLevel.AGENT_INFERRED,
            scope=Scope(paths=("src/x.py",)),
        ),
        audit=_AUDIT,
    )
    packet = PacketBuilder(service, MemoryConfig(enabled=True)).build(
        PacketContext(node_id="implementation", touched_paths=("src/x.py",))
    )
    assert [row["memory_id"] for row in packet.long_term] == ["trusted", "weak"]


def test_low_trust_contradiction_never_overwrites_active_memory(tmp_path: Path) -> None:
    service = _service(tmp_path)
    # An active, trusted lesson.
    service.append(
        LongTermRecord(
            memory_id="ltm_active",
            kind=LongTermKind.SEMANTIC,
            subject="build command",
            statement="use `make build`",
            trust_level=TrustLevel.HUMAN_CURATED,
            evidence=(Evidence(type="operator", ref="op:1"),),
        ),
        audit=_AUDIT,
    )
    # A weakly-grounded candidate that contradicts it (same subject, opposite statement).
    service.apply_delta(
        CandidateDelta(
            lessons=(
                CandidateLesson(
                    kind=LongTermKind.SEMANTIC,
                    subject="build command",
                    statement="use `npm run build`",
                    evidence=(),  # agent-inferred
                ),
            )
        ),
        episode=_episode(),
        source=WriteSource.SUCCESS,
        audit=_AUDIT,
    )
    active = service.read_long_term(LongTermKind.SEMANTIC)
    assert len(active) == 1
    assert active[0]["statement"] == "use `make build`"  # untouched — no silent overwrite
    assert any(row["subject"] == "build command" for row in service.read_quarantine())
