"""MemoryService (01.3/01.4): redaction chokepoint, tier routing, round-trip, audited update."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.memory import (
    AuditAction,
    AuditContext,
    EntityRecord,
    EpisodeRecord,
    Evidence,
    LongTermKind,
    LongTermRecord,
    MemoryLayout,
    MemoryService,
    TrustLevel,
)
from wastech_orchestrator.memory._io import atomic_write_jsonl, read_jsonl
from wastech_orchestrator.providers.redaction import REDACTED

# Fake credential-shaped strings (assembled so they are obviously not real secrets).
FAKE_GH = "ghp_" + "0123456789abcdef0123456789"
FAKE_OPENAI = "sk-" + "ABCDEFGHIJKLMNOPQRSTUV"
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"

_AUDIT = AuditContext(timestamp="2026-06-30T00:00:00Z")


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    return MemoryService(MemoryLayout(tmp_path / ".worc"))


def _semantic(statement: str, *, trust: TrustLevel = TrustLevel.HUMAN_CURATED) -> LongTermRecord:
    return LongTermRecord(
        memory_id="ltm1",
        kind=LongTermKind.SEMANTIC,
        subject="s",
        statement=statement,
        trust_level=trust,
    )


def test_append_episode_round_trips(service: MemoryService) -> None:
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
    rows = service.read_episodes()
    assert len(rows) == 1
    assert rows[0]["id"] == "ep1"
    assert rows[0]["trust_level"] == "artifact-backed"
    assert rows[0]["touched_paths"] == ["src/a.py"]


def test_long_term_routes_by_kind(service: MemoryService, tmp_path: Path) -> None:
    service.append(
        LongTermRecord(
            memory_id="m1",
            kind=LongTermKind.SEMANTIC,
            subject="s",
            statement="x",
            trust_level=TrustLevel.REPO_OBSERVED,
        ),
        audit=_AUDIT,
    )
    service.append(
        LongTermRecord(
            memory_id="m2",
            kind=LongTermKind.FAILURE,
            subject="sig",
            statement="boom",
            remedy="do y",
            trust_level=TrustLevel.REVIEW_VERIFIED,
        ),
        audit=_AUDIT,
    )
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1
    assert len(service.read_long_term(LongTermKind.FAILURE)) == 1
    long_term = tmp_path / ".worc" / "memory" / "long_term"
    assert (long_term / "semantic.jsonl").exists()
    assert (long_term / "failures.jsonl").exists()


def test_append_entity_round_trips(service: MemoryService) -> None:
    service.append(
        EntityRecord(
            entity_id="e1",
            entity_type="module",
            canonical_name="core/x.py",
            trust_level=TrustLevel.REPO_OBSERVED,
            paths=("src/core/x.py",),
        ),
        audit=_AUDIT,
    )
    rows = service.read_entities()
    assert rows[0]["entity_id"] == "e1"
    assert rows[0]["paths"] == ["src/core/x.py"]


@pytest.mark.parametrize("secret", [FAKE_GH, FAKE_OPENAI, FAKE_AWS])
def test_planted_token_is_redacted_before_disk(tmp_path: Path, secret: str) -> None:
    # AC-SF1: a secret-shaped string in any record field never lands in a memory file.
    service = MemoryService(MemoryLayout(tmp_path / ".worc"))
    service.append(
        LongTermRecord(
            memory_id="ltm1",
            kind=LongTermKind.SEMANTIC,
            subject="s",
            statement=f"token leaked: {secret}",
            rationale=f"see {secret}",
            evidence=(Evidence(type="diff", ref=f"x={secret}"),),
            trust_level=TrustLevel.HUMAN_CURATED,
        ),
        audit=_AUDIT,
    )
    raw = (tmp_path / ".worc" / "memory" / "long_term" / "semantic.jsonl").read_text(
        encoding="utf-8"
    )
    assert secret not in raw
    assert REDACTED in raw


def test_literal_extra_secret_is_redacted_before_disk(tmp_path: Path) -> None:
    secret = "super-secret-passphrase-value"
    service = MemoryService(MemoryLayout(tmp_path / ".worc"), extra_secrets=[secret])
    service.append(
        EntityRecord(
            entity_id="e1",
            entity_type="owner",
            canonical_name="team",
            trust_level=TrustLevel.HUMAN_CURATED,
            summary=f"contact {secret}",
        ),
        audit=_AUDIT,
    )
    entities_file = tmp_path / ".worc" / "memory" / "entities" / "entities.jsonl"
    raw = entities_file.read_text(encoding="utf-8")
    assert secret not in raw
    assert REDACTED in raw


def test_replace_all_rewrites_file_and_still_redacts(tmp_path: Path) -> None:
    service = MemoryService(MemoryLayout(tmp_path / ".worc"), extra_secrets=["topsecretvalue"])
    service.append(_semantic("first"), audit=_AUDIT)
    service.append(_semantic("second"), audit=_AUDIT)
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 2
    # A full rewrite to a single record (carrying a planted secret) replaces the whole file.
    service.replace_all(
        [_semantic("only topsecretvalue here")], action=AuditAction.MERGE, audit=_AUDIT
    )
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 1
    raw = (tmp_path / ".worc" / "memory" / "long_term" / "semantic.jsonl").read_text(
        encoding="utf-8"
    )
    assert "topsecretvalue" not in raw
    assert REDACTED in raw


def test_append_leaves_no_temp_file(service: MemoryService, tmp_path: Path) -> None:
    for i in range(3):
        service.append(_semantic(f"lesson {i}"), audit=_AUDIT)
    long_term = tmp_path / ".worc" / "memory" / "long_term"
    assert not list(long_term.glob("*.tmp"))  # no partial temp files left behind
    assert len(service.read_long_term(LongTermKind.SEMANTIC)) == 3


def test_atomic_write_jsonl_is_atomic_with_no_partial(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "data.jsonl"
    atomic_write_jsonl(target, [{"a": 1}, {"b": 2}])
    assert read_jsonl(target) == [{"a": 1}, {"b": 2}]
    assert not list((tmp_path / "sub").glob("*.tmp"))
