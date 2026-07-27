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

Read-isolation is orthogonal to this gate. The operator escape hatch
``security.disable_read_isolation`` — like the master ``strict_isolation: false`` — relaxes
only the READ side (native discovery + the private read-deny projection); this preflight validates
the WRITE/permission/sandbox ceiling, which stays in force regardless. So ``disable_read_isolation``
is a sanctioned opt-out, never itself a preflight reason (the per-provider ``isolation_reasons`` do
not examine it), and the ``strict_isolation`` preflight is unaffected.

``security.allow_git_evidence`` is likewise not a reason here, but for the opposite reason: it does
not relax the ceiling at all. A node it grants a shell to is held to reading by that same ceiling —
the adapter refuses the attempt outright (``CAPABILITY_UNAVAILABLE``) on a host where the shell
could not be sandboxed, which is a per-attempt decision the adapter makes with the node's
declaration in hand. This gate sees only the provider config, so it cannot tell whether any node
declares the grant; flagging on the switch alone would fail preflight for runs that never use it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from wastech_orchestrator.config.schema import OrchestratorConfig, ProviderConfig
from wastech_orchestrator.providers.base import ProviderId

# A provider's offline "can your required isolation be enabled?" check, keyed by provider id. The
# concrete implementations live in the adapters (Codex's sandbox / Claude's permission mode); the
# composition root binds them and injects the table so this module imports no concrete adapter.
IsolationCheck = Callable[[ProviderConfig], list[str]]


def check_isolation(
    config: OrchestratorConfig, checks: Mapping[ProviderId, IsolationCheck]
) -> list[str]:
    """Return a reason per provider whose required isolation cannot be enabled; ``[]`` means all OK.

    Pure and deterministic (no CLI launched). ``checks`` is the ProviderId→isolation-check table
    injected by the composition root (so this module imports no concrete adapter). Each reason is
    prefixed with the provider id so the caller can surface a single combined message.
    """
    reasons: list[str] = []
    for provider_id in _providers_in_use(config):
        provider_cfg = config.agents.providers.get(provider_id)
        check = checks.get(provider_id)
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
