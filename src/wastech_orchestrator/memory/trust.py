"""Trust levels and the promotion-durability gate.

Trust is the spine of the poisoning defenses: it is assigned by the deterministic ``MemoryService``
(never self-certified by a candidate), and a record's trust level alone decides whether it is even
*eligible* to become durable long-term knowledge. This module owns the enum and the eligibility
predicate; the full promotion gate (evidence, recurrence, the ``artifact-backed`` validator pass)
layers on top in a later phase.
"""

from __future__ import annotations

from enum import StrEnum


class TrustLevel(StrEnum):
    """How much a piece of memory can be trusted, most→least trusted."""

    REPO_OBSERVED = "repo-observed"  # verifiable from current code/config
    HUMAN_CURATED = "human-curated"  # operator wrote/approved (required for procedural)
    REVIEW_VERIFIED = "review-verified"  # confirmed by a review/fixing outcome
    ARTIFACT_BACKED = "artifact-backed"  # derived from task artifacts/checks (validator-gated)
    AGENT_INFERRED = "agent-inferred"  # LLM synthesis, unconfirmed → quarantine / short-term only
    EXTERNAL_UNTRUSTED = "external-untrusted"  # web/MCP/user/API content → never auto, human-gated


# Trust levels that MAY back a durable long-term/entity record. Membership is *necessary,
# not sufficient*: ``artifact-backed`` additionally requires a validator pass at promotion (a later
# phase), and the gate also checks evidence/recurrence. ``agent-inferred`` / ``external-untrusted``
# are excluded outright — they can never auto-promote (quarantine or short-term only).
DURABLE_TRUST_LEVELS: frozenset[TrustLevel] = frozenset(
    {
        TrustLevel.REPO_OBSERVED,
        TrustLevel.HUMAN_CURATED,
        TrustLevel.REVIEW_VERIFIED,
        TrustLevel.ARTIFACT_BACKED,
    }
)


def is_durable_candidate(trust: TrustLevel) -> bool:
    """Whether ``trust`` is *eligible* (necessary, not sufficient) for a durable long-term record.

    This is the trust-level half of the guarantee that a low-trust record can never behave as a
    high-trust one.
    The promotion gate (a later phase) applies the remaining rules (evidence, recurrence, and the
    ``artifact-backed`` validator pass) before anything is actually promoted.
    """
    return trust in DURABLE_TRUST_LEVELS
