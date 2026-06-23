"""Check resolution — normalize the operator's ``checks.command_sets`` into runnable sets.

There is no discovery, caching, fingerprinting, or agent assist: the gate is exactly what the
operator listed in ``config.yaml`` (an empty ``command_sets`` mapping means no gate). The
orchestrator-owned Check Runner remains the sole quality-gate authority; this just turns config
shapes into :class:`~wastech_orchestrator.checks.model.ResolvedCheckSet`s.
"""

from __future__ import annotations

from wastech_orchestrator.checks.model import ResolvedCheckSet, normalize_command_sets
from wastech_orchestrator.config.schema import OrchestratorConfig


class CheckResolver:
    """Normalizes ``checks.command_sets`` into ``ResolvedCheckSet``s (no discovery, no cache)."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config

    def resolve(self) -> tuple[ResolvedCheckSet, ...]:
        """Return the configured command sets, normalized. Empty mapping → ``()`` (no gate)."""
        return normalize_command_sets(self._config.checks.command_sets)
