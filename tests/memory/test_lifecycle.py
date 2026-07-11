"""Lifecycle decision helpers (02.4): trust assignment, promotion gate, subject normalization."""

from __future__ import annotations

import pytest

from wastech_orchestrator.memory import Evidence, TrustLevel
from wastech_orchestrator.memory.lifecycle import (
    assign_entity_trust,
    assign_trust,
    normalize_subject,
    should_promote,
)


@pytest.mark.parametrize(
    ("ev_type", "expected"),
    [
        ("web", TrustLevel.EXTERNAL_UNTRUSTED),
        ("mcp", TrustLevel.EXTERNAL_UNTRUSTED),
        ("operator", TrustLevel.HUMAN_CURATED),
        ("review", TrustLevel.REVIEW_VERIFIED),
        ("repo_doc", TrustLevel.REPO_OBSERVED),
        ("check", TrustLevel.ARTIFACT_BACKED),
        # F29: the tokens the supervisor actually emits for a repo file / a git commit must ground
        # durable classes, not fall through to agent-inferred (which quarantined every repo lesson).
        ("file", TrustLevel.REPO_OBSERVED),
        ("commit", TrustLevel.ARTIFACT_BACKED),
        ("mystery", TrustLevel.AGENT_INFERRED),
    ],
)
def test_assign_trust_from_evidence_type(ev_type: str, expected: TrustLevel) -> None:
    assert assign_trust([Evidence(type=ev_type, ref="x")]) is expected


def test_assign_trust_no_evidence_is_agent_inferred() -> None:
    assert assign_trust([]) is TrustLevel.AGENT_INFERRED


def test_external_evidence_dominates() -> None:
    # Any external pointer makes the whole record external-untrusted, even with strong evidence.
    mixed = [Evidence("review", "r"), Evidence("web", "u")]
    assert assign_trust(mixed) is TrustLevel.EXTERNAL_UNTRUSTED


def test_assign_entity_trust_keys_off_paths() -> None:
    # No predicate (legacy/read-only callers): naming >= 1 path is repo-observed; path-less is not.
    assert assign_entity_trust(("src/a.py",)) is TrustLevel.REPO_OBSERVED
    assert assign_entity_trust(()) is TrustLevel.AGENT_INFERRED


def test_assign_entity_trust_validates_paths_when_predicate_given() -> None:
    # F1/NFR2: with a live-repo predicate, repo-observed requires every named path present; any
    # missing path downgrades the whole card off durable trust (the write funnel quarantines it).
    present = {"src/real.py", "src/also.py"}.__contains__

    def trust(*paths: str) -> TrustLevel:
        return assign_entity_trust(paths, path_exists=present)

    assert trust("src/real.py") is TrustLevel.REPO_OBSERVED
    assert trust("src/real.py", "src/also.py") is TrustLevel.REPO_OBSERVED
    assert trust("src/gone.py") is TrustLevel.AGENT_INFERRED
    assert trust("src/real.py", "src/gone.py") is TrustLevel.AGENT_INFERRED


def test_normalize_subject_lowercases_and_collapses_whitespace() -> None:
    assert normalize_subject("  Config   Schema  ") == "config schema"


def _promote(
    trust: TrustLevel, *, ev: bool = True, recur: int = 1, mn: int = 2, **kw: bool
) -> bool:
    return should_promote(trust=trust, has_evidence=ev, recurrence_tasks=recur, min_tasks=mn, **kw)


def test_promotion_requires_durable_trust_and_evidence_and_no_contradiction() -> None:
    assert _promote(TrustLevel.AGENT_INFERRED, recur=5) is False  # AC-SF2
    assert _promote(TrustLevel.EXTERNAL_UNTRUSTED, recur=5) is False  # AC-W4
    assert _promote(TrustLevel.HUMAN_CURATED, ev=False) is False  # AC-W2
    assert _promote(TrustLevel.HUMAN_CURATED, recur=5, has_contradiction=True) is False


def test_repo_human_and_review_trust_auto_promote() -> None:
    # Memory V2 (move 3): repo-observed joins human/review as first-sight promotable — repo-verified
    # durable knowledge no longer starves in quarantine waiting to recur.
    assert _promote(TrustLevel.REPO_OBSERVED, recur=1) is True
    assert _promote(TrustLevel.HUMAN_CURATED, recur=1) is True
    assert _promote(TrustLevel.REVIEW_VERIFIED, recur=1) is True


def test_artifact_backed_needs_recurrence() -> None:
    # Durable but NOT auto-promote (memory V2 keeps its recurrence gate as the interim stand-in for
    # its unbuilt validator): one short of recurrence stays short-term (Q3).
    assert _promote(TrustLevel.ARTIFACT_BACKED, recur=1, mn=2) is False
    assert _promote(TrustLevel.ARTIFACT_BACKED, recur=2, mn=2) is True


def test_explained_failure_and_hotspot_gates() -> None:
    assert _promote(TrustLevel.ARTIFACT_BACKED, recur=1, explained_failure=True) is True
    # A non-auto-promote trust so the hotspot gate is what carries it (repo-observed would pass on
    # its own now).
    assert _promote(TrustLevel.ARTIFACT_BACKED, recur=1, stable_hotspot=True) is True
