"""Safety drill 05.1 — planted secrets never reach `.worc/memory/` (AC-SF1, leak count 0).

Adversarial end-to-end: plant secret-shaped strings in every free-text field a candidate delta and
episode carry, run the real write funnel (``apply_delta``), then scan **every** file the store wrote
(all tiers + audit + quarantine) and a rendered packet. Nothing redacted-away may resurface.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.config.schema import MemoryConfig
from wastech_orchestrator.memory import (
    AuditContext,
    CandidateDelta,
    CandidateEntity,
    CandidateFailure,
    CandidateLesson,
    EpisodeRecord,
    Evidence,
    MemoryLayout,
    MemoryService,
    PacketBuilder,
    PacketContext,
    Scope,
    TrustLevel,
    WriteSource,
)
from wastech_orchestrator.memory.records import LongTermKind
from wastech_orchestrator.providers.redaction import REDACTED

# Credential-shaped strings (assembled so they are obviously not real), plus a literal extra secret.
FAKE_GH = "ghp_" + "0123456789abcdef0123456789"
FAKE_OPENAI = "sk-" + "ABCDEFGHIJKLMNOPQRSTUV"
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
LITERAL = "super-secret-passphrase-value"
_PLANTED = (FAKE_GH, FAKE_OPENAI, FAKE_AWS, LITERAL)

_AUDIT = AuditContext(timestamp="2026-07-01T00:00:00Z", task_id="t1")


def _planted_delta() -> CandidateDelta:
    return CandidateDelta(
        lessons=(
            CandidateLesson(
                kind=LongTermKind.SEMANTIC,
                subject="db connection",
                statement=f"connect with token {FAKE_GH}",
                rationale=f"observed {FAKE_OPENAI} in the config",
                scope=Scope(paths=("src/db.py",)),
                evidence=(Evidence(type="repo_doc", ref=f"README:{FAKE_AWS}"),),
            ),
        ),
        failures=(
            CandidateFailure(
                signature=f"auth fails with {FAKE_GH}",
                paths=("src/auth.py",),
                remedy=f"rotate {LITERAL}",
                evidence=(Evidence(type="review", ref="review:1"),),
            ),
        ),
        entities=(
            CandidateEntity(
                entity_id="e1",
                entity_type="module",
                paths=("src/db.py",),
                summary=f"owner contact {LITERAL}",
                risk_notes=(f"holds {FAKE_AWS}",),
            ),
        ),
    )


def test_planted_secrets_never_reach_any_memory_file(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    service = MemoryService(layout, extra_secrets=[LITERAL], config=MemoryConfig(enabled=True))
    episode = EpisodeRecord(
        id="ep_t1",
        task_id="t1",
        created_at="2026-07-01T00:00:00Z",
        trust_level=TrustLevel.ARTIFACT_BACKED,
        stage_outcomes={"note": f"leaked {FAKE_GH}"},
    )
    service.apply_delta(_planted_delta(), episode=episode, source=WriteSource.SUCCESS, audit=_AUDIT)

    leaks: list[str] = []
    for path in sorted(layout.root.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for secret in _PLANTED:
            if secret in text:
                leaks.append(f"{path.relative_to(layout.root).as_posix()} -> {secret[:6]}…")
    assert leaks == [], f"secret leaked into a memory file: {leaks}"


def test_planted_secret_never_reaches_a_rendered_packet(tmp_path: Path) -> None:
    layout = MemoryLayout.for_repo(tmp_path)
    config = MemoryConfig(enabled=True)
    service = MemoryService(layout, extra_secrets=[LITERAL], config=config)
    episode = EpisodeRecord(
        id="ep_t1", task_id="t1", created_at="2026-07-01T00:00:00Z",
        trust_level=TrustLevel.ARTIFACT_BACKED,
    )
    service.apply_delta(_planted_delta(), episode=episode, source=WriteSource.SUCCESS, audit=_AUDIT)

    packet = PacketBuilder(service, config)
    rendered = packet.render(packet.build(PacketContext(node_id="implementation")))
    for secret in _PLANTED:
        assert secret not in rendered
    assert REDACTED in rendered  # the redaction marker did land where a secret was
