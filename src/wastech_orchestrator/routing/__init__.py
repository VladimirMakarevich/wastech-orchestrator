"""Routing layer (–).

The Agent Router resolves the primary/fallback provider per stage and performs infrastructure-only
fallback between them. It depends solely on the ``AgentProvider`` contract (no CLI syntax) and
changes no state-machine state.
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
