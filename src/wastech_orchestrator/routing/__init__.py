"""Routing layer (spec §4.2, §5, §7.2–§7.4, §8.1).

The Agent Router resolves the primary/fallback provider per stage and performs infrastructure-only
fallback between them. It depends solely on the ``AgentProvider`` contract (no CLI syntax) and
changes no state-machine state — see docs/rules/architecture.md.
"""

from __future__ import annotations

from wastech_orchestrator.routing.router import (
    CONDITIONAL_FALLBACK,
    AgentRouter,
    ProviderAttempt,
    ResolvedRoute,
    RouteSource,
    StageOutcome,
    fallback_allowed,
)
from wastech_orchestrator.routing.snapshots import (
    PartialChange,
    SnapshotHook,
    WorkingTreeSnapshot,
)

__all__ = [
    "CONDITIONAL_FALLBACK",
    "AgentRouter",
    "PartialChange",
    "ProviderAttempt",
    "ResolvedRoute",
    "RouteSource",
    "SnapshotHook",
    "StageOutcome",
    "WorkingTreeSnapshot",
    "fallback_allowed",
]
