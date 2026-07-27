"""Memory records + trust: trust required & readable, durability gate, serialization."""

from __future__ import annotations

import pytest

from wastech_orchestrator.memory import (
    DURABLE_TRUST_LEVELS,
    EntityRecord,
    EpisodeRecord,
    Evidence,
    LongTermKind,
    LongTermRecord,
    Scope,
    TrustLevel,
    as_row,
    is_durable_candidate,
    record_id,
)


def test_six_trust_levels_have_canonical_string_values() -> None:
    assert TrustLevel.HUMAN_CURATED == "human-curated"
    assert {t.value for t in TrustLevel} == {
        "repo-observed",
        "human-curated",
        "review-verified",
        "artifact-backed",
        "agent-inferred",
        "external-untrusted",
    }


@pytest.mark.parametrize(
    ("trust", "durable"),
    [
        (TrustLevel.REPO_OBSERVED, True),
        (TrustLevel.HUMAN_CURATED, True),
        (TrustLevel.REVIEW_VERIFIED, True),
        (TrustLevel.ARTIFACT_BACKED, True),
        (TrustLevel.AGENT_INFERRED, False),
        (TrustLevel.EXTERNAL_UNTRUSTED, False),
    ],
)
def test_durability_gate_reads_trust(trust: TrustLevel, durable: bool) -> None:
    # A low-trust record can never behave as a high-trust one.
    assert is_durable_candidate(trust) is durable
    assert (trust in DURABLE_TRUST_LEVELS) is durable


def test_every_record_requires_a_trust_level() -> None:
    # trust_level is a non-default field on each record → construction without it raises TypeError.
    with pytest.raises(TypeError):
        EpisodeRecord(id="ep1", task_id="t1", created_at="2026-06-30T00:00:00Z")
    with pytest.raises(TypeError):
        LongTermRecord(memory_id="ltm1", kind=LongTermKind.SEMANTIC, subject="s", statement="x")
    with pytest.raises(TypeError):
        EntityRecord(entity_id="e1", entity_type="module", canonical_name="m")


def test_as_row_serializes_enums_and_nested_dataclasses() -> None:
    record = LongTermRecord(
        memory_id="ltm1",
        kind=LongTermKind.SEMANTIC,
        subject="config",
        statement="bump docs with schema changes",
        trust_level=TrustLevel.HUMAN_CURATED,
        scope=Scope(paths=("src/config/",), nodes=("review",)),
        evidence=(Evidence(type="repo_doc", ref="CLAUDE.md"),),
    )
    row = as_row(record)
    assert row["trust_level"] == "human-curated"  # StrEnum -> its value
    assert row["kind"] == "semantic"
    assert list(row["scope"]["paths"]) == ["src/config/"]  # nested dataclass flattened
    assert dict(row["evidence"][0]) == {"type": "repo_doc", "ref": "CLAUDE.md"}


def test_record_id_dispatches_across_tiers() -> None:
    episode = EpisodeRecord(
        id="ep1", task_id="t", created_at="now", trust_level=TrustLevel.ARTIFACT_BACKED
    )
    lesson = LongTermRecord(
        memory_id="ltm1",
        kind=LongTermKind.FAILURE,
        subject="sig",
        statement="x",
        trust_level=TrustLevel.REVIEW_VERIFIED,
    )
    entity = EntityRecord(
        entity_id="e1",
        entity_type="module",
        canonical_name="m",
        trust_level=TrustLevel.REPO_OBSERVED,
    )
    assert record_id(episode) == "ep1"
    assert record_id(lesson) == "ltm1"
    assert record_id(entity) == "e1"
