"""Safety drill 05.1 — planted secrets never reach `.worc/memory/` (AC-SF1, leak count 0).

Adversarial end-to-end: plant secret-shaped strings in every free-text field a candidate delta and
episode carry, run the real write funnel (``apply_delta``), then scan **every** file the store wrote
(all tiers + audit + quarantine) and a rendered packet. Nothing redacted-away may resurface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
from wastech_orchestrator.providers.redaction import REDACTED, secret_env_values

# A repo-specific secret value that matches NO structural token pattern (the F3 gap): only the
# orchestrator's env-secret harvesting (fed into ``extra_secrets``) can catch it.
ENV_SECRET_VALUE = "pla1n-repo-value-not-a-token-shape"

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
    layout = MemoryLayout(tmp_path / ".worc")
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
    layout = MemoryLayout(tmp_path / ".worc")
    config = MemoryConfig(enabled=True)
    service = MemoryService(layout, extra_secrets=[LITERAL], config=config)
    episode = EpisodeRecord(
        id="ep_t1",
        task_id="t1",
        created_at="2026-07-01T00:00:00Z",
        trust_level=TrustLevel.ARTIFACT_BACKED,
    )
    service.apply_delta(_planted_delta(), episode=episode, source=WriteSource.SUCCESS, audit=_AUDIT)

    packet = PacketBuilder(service, config)
    rendered = packet.render(packet.build(PacketContext(node_id="implementation")))
    for secret in _PLANTED:
        assert secret not in rendered
    assert REDACTED in rendered  # the redaction marker did land where a secret was


def _episode_naming(secret: str) -> EpisodeRecord:
    return EpisodeRecord(
        id="ep_t1",
        task_id="t1",
        created_at="2026-07-01T00:00:00Z",
        trust_level=TrustLevel.ARTIFACT_BACKED,
        stage_outcomes={"note": f"used {secret}"},
    )


def test_env_secret_leaks_without_harvesting(tmp_path: Path) -> None:
    # F3 baseline: a repo-specific secret that matches no token shape is NOT caught by the
    # structural patterns alone — proving the gap the orchestrator's extra_secrets wiring closes.
    layout = MemoryLayout(tmp_path / ".worc")
    service = MemoryService(layout, config=MemoryConfig(enabled=True))  # extra_secrets=() (old)
    service.apply_delta(
        None, episode=_episode_naming(ENV_SECRET_VALUE), source=WriteSource.FAILURE, audit=_AUDIT
    )
    recent = (layout.short_term / "recent.jsonl").read_text(encoding="utf-8")
    assert ENV_SECRET_VALUE in recent  # structural patterns miss it


def test_orchestrator_style_env_secret_is_scrubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F3 fix: built the way the orchestrator now does — extra_secrets harvested from secret-named
    # env vars — the same non-pattern value is scrubbed from every memory file.
    monkeypatch.setenv("REPO_DB_SECRET", ENV_SECRET_VALUE)  # secret name, not allowlisted
    layout = MemoryLayout(tmp_path / ".worc")
    service = MemoryService(
        layout,
        config=MemoryConfig(enabled=True),
        extra_secrets=secret_env_values(allowed_environment=()),
    )
    service.apply_delta(
        None, episode=_episode_naming(ENV_SECRET_VALUE), source=WriteSource.FAILURE, audit=_AUDIT
    )
    for path in sorted(layout.root.rglob("*")):
        if path.is_file():
            assert ENV_SECRET_VALUE not in path.read_text(encoding="utf-8")
