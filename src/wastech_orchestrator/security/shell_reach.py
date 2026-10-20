"""Whether one attempt actually gets a shell — the per-attempt question, asked provider-neutrally.

Command execution, not the permission profile, is what makes a working-tree write or a ``.git``
mutation reachable in the first place, so every bracket that watches for one has to key on "can this
attempt run commands", not on a profile name or on a declared grant. That question is
provider-specific and host-specific: Codex's ``read-only`` sandbox permits commands, while Claude's
depends on whether the resolved tool set keeps ``Bash`` — which its own platform arms can drop.

So this module holds the seam, not the answer. Like the sibling ``strict_isolation`` gate
(:mod:`wastech_orchestrator.security.isolation`) it defines the provider-neutral callable type and
dispatches by :class:`~wastech_orchestrator.providers.base.ProviderId`; the concrete answers live in
the adapters (``providers/*.py`` ``attempt_has_shell``) and the composition root binds the table, so
neither this module nor its callers import a concrete adapter.

Fail-closed means **assume the shell is there**: an unknown provider or a missing check yields
``True``, so the caller brackets an attempt it could not classify. The cost of a wrong ``True`` is
one fingerprint; the cost of a wrong ``False`` is an unwatched attempt.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers.base import ProviderId


@dataclass(frozen=True, slots=True)
class ShellQuery:
    """The per-attempt facts a provider needs to answer "does this attempt get a shell".

    ``permission_profile`` is the node's resolved ceiling (``None`` — the node declared none, so
    the provider's configured default applies); ``git_evidence`` is the resolved read-only-git
    grant, which hands a shell to a profile that carries none; ``strict_isolation`` is the
    operator's master switch, because a host that cannot sandbox a shell either loses it or keeps it
    depending on that switch. Deliberately not the whole
    :class:`~wastech_orchestrator.providers.base.AgentRunRequest`: the bracket that asks this runs
    before a request exists.
    """

    permission_profile: str | None
    git_evidence: bool
    strict_isolation: bool


#: A provider's offline "does this attempt get a shell?" check, keyed by provider id. The concrete
#: implementations live in the adapters (Codex's sandbox always permits commands; Claude's resolved
#: tool set may or may not keep ``Bash``); the composition root binds them and injects the table so
#: this module imports no concrete adapter.
ShellCheck = Callable[[ProviderConfig, ShellQuery], bool]


def any_provider_grants_shell(
    providers: Iterable[ProviderId],
    configs: Mapping[ProviderId, ProviderConfig],
    checks: Mapping[ProviderId, ShellCheck],
    query: ShellQuery,
) -> bool:
    """True when **any** of *providers* would give this attempt a shell (offline; fail-closed).

    Pure and deterministic (no CLI launched). ``providers`` is every provider the attempt may land
    on — a route's primary *and* its fallback — because the bracket has to be taken before the run
    and therefore before it is known which one serves it. A provider with no config block or no
    bound check counts as granting a shell: the answer is unknown, and an unwatched attempt is the
    worse error.
    """
    for provider_id in providers:
        provider_cfg = configs.get(provider_id)
        check = checks.get(provider_id)
        if provider_cfg is None or check is None:
            return True
        if check(provider_cfg, query):
            return True
    return False


def every_provider_grants_shell(
    providers: Iterable[ProviderId],
    configs: Mapping[ProviderId, ProviderConfig],
    checks: Mapping[ProviderId, ShellCheck],
    query: ShellQuery,
) -> bool:
    """True only when **every** one of *providers* would give this attempt a shell (offline).

    The opposite question to :func:`any_provider_grants_shell`, with the opposite default, because
    a wrong answer costs the opposite thing. That one guards detection, where an unwatched attempt
    is the worse error; this one decides whether a node may be *told* to inspect the change with
    commands, and telling a node to run a command it cannot run sends it off to fail at a tool
    call. So the answer is ``True`` only when it is known for every provider the attempt may land
    on — a route's primary and its fallback alike, since the instruction is written once, before it
    is known which one serves it — and an empty list, a missing config block or an unbound check
    all answer ``False``.
    """
    provider_ids = list(providers)
    if not provider_ids:
        return False
    for provider_id in provider_ids:
        provider_cfg = configs.get(provider_id)
        check = checks.get(provider_id)
        if provider_cfg is None or check is None or not check(provider_cfg, query):
            return False
    return True
