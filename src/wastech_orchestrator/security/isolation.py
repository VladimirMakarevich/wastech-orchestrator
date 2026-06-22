"""``strict_isolation`` preflight check.

When ``security.strict_isolation`` is true, "an inability to enable the required isolation fails
preflight with an error (no silent downgrade)". This module is the deterministic, **offline** check
that drives that gate: it asks each provider that may run whether its configured isolation can be
enabled, **without launching any CLI**, so the gate is unit-testable and runs before a branch is
ever created (see :meth:`Orchestrator._drive_via_engine`).

The provider-specific meaning of "required isolation" stays in the adapters (``providers/*.py``
``isolation_reasons``) — Codex's sandbox, Claude's permission mode — so this module holds no CLI
syntax; it only dispatches by :class:`~wastech_orchestrator.providers.base.ProviderId` and frames
the reasons. Only providers that *may* run are checked (those in ``agents.allowed`` — every flow
node either declares an allowed ``provider`` or defaults to the global primary, also allowed), so a
configured-but-unused provider block never bricks an otherwise-valid run.
"""

from __future__ import annotations

from collections.abc import Callable

from wastech_orchestrator.config.schema import OrchestratorConfig, ProviderConfig
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.providers.claude import isolation_reasons as _claude_isolation_reasons
from wastech_orchestrator.providers.codex import isolation_reasons as _codex_isolation_reasons

_ISOLATION_CHECKS: dict[ProviderId, Callable[[ProviderConfig], list[str]]] = {
    ProviderId.CLAUDE: _claude_isolation_reasons,
    ProviderId.CODEX: _codex_isolation_reasons,
}


def check_isolation(config: OrchestratorConfig) -> list[str]:
    """Return a reason per provider whose required isolation cannot be enabled; ``[]`` means all OK.

    Pure and deterministic (no CLI launched). Each reason is prefixed with the provider id so the
    caller can surface a single combined message.
    """
    reasons: list[str] = []
    for provider_id in _providers_in_use(config):
        provider_cfg = config.agents.providers.get(provider_id)
        check = _ISOLATION_CHECKS.get(provider_id)
        if provider_cfg is None or check is None:
            continue
        reasons.extend(f"{provider_id.value}: {reason}" for reason in check(provider_cfg))
    return reasons


def _providers_in_use(config: OrchestratorConfig) -> list[ProviderId]:
    """The providers that may actually run: ``agents.allowed``.

    Every flow node either declares a ``provider`` (which must be in ``agents.allowed``) or defaults
    to the global primary (also in ``agents.allowed``), so the allowlist is the exact set of
    providers that can launch — a merely-configured provider block never bricks the run.
    """
    seen: dict[ProviderId, None] = {}
    for provider_id in config.agents.allowed:
        seen.setdefault(provider_id, None)
    return list(seen)
