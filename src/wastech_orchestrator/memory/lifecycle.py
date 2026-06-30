"""Deterministic lifecycle rules for the memory write path (design §5 + §7).

Pure, model-free decision helpers that :meth:`MemoryService.apply_delta` composes:

* **Trust assignment** from evidence types — never from the candidate's advisory ``trust_hint`` (no
  self-certification to a durable level, AC-SF5). External evidence dominates (most cautious).
* **Subject normalization** for dedup / recurrence matching.
* **The promotion gate** (design §5 / Q3 thresholds): durable trust + evidence + no contradiction,
  then one of recurrence / auto-promote trust / explained-failure / stable-hotspot.

Keeping these here (small, total functions) makes the write-path policy unit-testable in isolation.
"""

from __future__ import annotations

from collections.abc import Sequence

from wastech_orchestrator.memory.records import Evidence
from wastech_orchestrator.memory.trust import DURABLE_TRUST_LEVELS, TrustLevel

# Evidence ``type`` tokens grouped by the trust they ground (matched case-insensitively). An unknown
# type grounds nothing (→ ``agent-inferred``). ``external`` dominates: any external pointer makes
# the whole record ``external-untrusted`` (poisoning defense, AC-W4).
_EXTERNAL: frozenset[str] = frozenset({"web", "mcp", "url", "external", "api"})
_OPERATOR: frozenset[str] = frozenset({"operator", "human", "hitl"})
_REVIEW: frozenset[str] = frozenset({"review", "fixing"})
_REPO: frozenset[str] = frozenset({"repo", "repo_doc", "code", "config", "doc"})
_ARTIFACT: frozenset[str] = frozenset({"artifact", "check", "diff", "test", "plan"})

# Trust levels that auto-promote (no recurrence required) — design §5 / Q3.
_AUTO_PROMOTE: frozenset[TrustLevel] = frozenset(
    {TrustLevel.HUMAN_CURATED, TrustLevel.REVIEW_VERIFIED}
)


def assign_trust(evidence: Sequence[Evidence]) -> TrustLevel:
    """Assign the final trust deterministically from evidence types (design §7).

    External dominates; otherwise the best-grounded class wins (operator/review/repo/artifact);
    no (recognized) evidence → ``agent-inferred``. The candidate's advisory ``trust_hint`` is
    intentionally **not** an input — a candidate can never self-certify (AC-SF5).
    """
    types = {item.type.strip().lower() for item in evidence}
    if types & _EXTERNAL:
        return TrustLevel.EXTERNAL_UNTRUSTED
    if types & _OPERATOR:
        return TrustLevel.HUMAN_CURATED
    if types & _REVIEW:
        return TrustLevel.REVIEW_VERIFIED
    if types & _REPO:
        return TrustLevel.REPO_OBSERVED
    if types & _ARTIFACT:
        return TrustLevel.ARTIFACT_BACKED
    return TrustLevel.AGENT_INFERRED


def assign_entity_trust(paths: Sequence[str]) -> TrustLevel:
    """Trust for an entity card. Candidate entities have no evidence — they are grounded in the repo
    paths/symbols they name. A card naming >= 1 path is treated as ``repo-observed`` (a later phase
    verifies the paths via ``DerivedIndex``); a path-less card is ``agent-inferred`` (non-durable).
    """
    return TrustLevel.REPO_OBSERVED if paths else TrustLevel.AGENT_INFERRED


def normalize_subject(subject: str) -> str:
    """A stable key for dedup/recurrence: lowercased with collapsed whitespace."""
    return " ".join(subject.lower().split())


def should_promote(
    *,
    trust: TrustLevel,
    has_evidence: bool,
    recurrence_tasks: int,
    min_tasks: int,
    explained_failure: bool = False,
    stable_hotspot: bool = False,
    has_contradiction: bool = False,
) -> bool:
    """The design §5 / Q3 promotion gate.

    **Necessary** (all required): durable trust, non-empty evidence, no active contradiction —
    ``external-untrusted`` / ``agent-inferred`` therefore never promote (AC-SF2/AC-W4). **Then any
    one** of: ``>= min_tasks`` recurrence, auto-promote trust, explained failure, or stable hotspot.
    """
    if trust not in DURABLE_TRUST_LEVELS or not has_evidence or has_contradiction:
        return False
    return (
        recurrence_tasks >= min_tasks
        or trust in _AUTO_PROMOTE
        or explained_failure
        or stable_hotspot
    )
