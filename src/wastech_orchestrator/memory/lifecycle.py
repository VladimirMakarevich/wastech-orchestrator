"""Deterministic lifecycle rules for the memory write path.

Pure, model-free decision helpers that :meth:`MemoryService.apply_delta` composes:

* **Trust assignment** from evidence types — never from the candidate's advisory ``trust_hint`` (no
  self-certification to a durable level). External evidence dominates (most cautious).
* **Subject normalization** for dedup / recurrence matching.
* **The promotion gate** (the configured thresholds): durable trust + evidence + no contradiction,
  then one of recurrence / auto-promote trust / explained-failure / stable-hotspot.

Keeping these here (small, total functions) makes the write-path policy unit-testable in isolation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from wastech_orchestrator.memory.records import Evidence
from wastech_orchestrator.memory.trust import DURABLE_TRUST_LEVELS, TrustLevel

# Evidence ``type`` tokens grouped by the trust they ground (matched case-insensitively). An unknown
# type grounds nothing (→ ``agent-inferred``). ``external`` dominates: any external pointer makes
# the whole record ``external-untrusted`` (poisoning defense).
_EXTERNAL: frozenset[str] = frozenset({"web", "mcp", "url", "external", "api"})
_OPERATOR: frozenset[str] = frozenset({"operator", "human", "hitl"})
_REVIEW: frozenset[str] = frozenset({"review", "fixing"})
# ``file`` is the token the supervisor naturally writes for a repo file pointer (a code/doc
# reference verifiable in the tree), and ``commit`` for a git artifact. Both ground the durable
# classes semantically but fell through to ``agent-inferred`` because they were absent here — which
# is why every repo-grounded lesson quarantined and ``long_term/`` stayed empty.
_REPO: frozenset[str] = frozenset({"repo", "repo_doc", "code", "config", "doc", "file"})
_ARTIFACT: frozenset[str] = frozenset({"artifact", "check", "diff", "test", "plan", "commit"})

# Trust levels that auto-promote on first sight (no recurrence required).
# ``repo-observed`` is included because the card/lesson is verified against
# the live repo at write time (its paths exist — ``assign_entity_trust``), so waiting for it
# to recur only starves durable knowledge the operator wants. ``artifact-backed`` deliberately stays
# out — it keeps the recurrence gate as the interim stand-in for its unbuilt validator pass — and
# ``agent-inferred`` / ``external-untrusted`` are non-durable and never promote at all.
_AUTO_PROMOTE: frozenset[TrustLevel] = frozenset(
    {TrustLevel.REPO_OBSERVED, TrustLevel.HUMAN_CURATED, TrustLevel.REVIEW_VERIFIED}
)


def assign_trust(evidence: Sequence[Evidence]) -> TrustLevel:
    """Assign the final trust deterministically from evidence types.

    External dominates; otherwise the best-grounded class wins (operator/review/repo/artifact);
    no (recognized) evidence → ``agent-inferred``. The candidate's advisory ``trust_hint`` is
    intentionally **not** an input — a candidate can never self-certify.
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


def assign_entity_trust(
    paths: Sequence[str], *, path_exists: Callable[[str], bool] | None = None
) -> TrustLevel:
    """Trust for an entity card. Candidate entities have no evidence — they are grounded in the repo
    paths they name. A card earns the durable ``repo-observed`` only when it names >= 1 path **and**
    those paths verify present in the live repo (durable entries are verified against code);
    a card whose target is gone — or a path-less card — is ``agent-inferred`` (non-durable, so the
    write funnel quarantines it).

    ``path_exists`` is the live-repo predicate (``DerivedIndex.path_exists``). Best-effort and
    fail-closed: it never raises (a git-unavailable repo falls back to a filesystem stat), and a
    path it cannot confirm present downgrades the card off durable trust → quarantine (recoverable),
    never a silent durable card. When ``path_exists`` is ``None`` (read-only / legacy callers that
    wire no index) the check is skipped and naming >= 1 path is ``repo-observed`` as before.
    """
    if not paths:
        return TrustLevel.AGENT_INFERRED
    if path_exists is not None and not all(path_exists(p) for p in paths):
        return TrustLevel.AGENT_INFERRED
    return TrustLevel.REPO_OBSERVED


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
    """The promotion gate.

    **Necessary** (all required): durable trust, non-empty evidence, no active contradiction —
    ``external-untrusted`` / ``agent-inferred`` therefore never promote. **Then any
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
