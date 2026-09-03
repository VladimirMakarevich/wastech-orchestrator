"""The two isolation questions the core asks without knowing any CLI.

Both are deterministic and **offline** (no CLI is launched), both are answered by provider-owned
functions the composition root injects — so this module imports no concrete adapter — and both are
asked only of the providers that may actually run (``agents.allowed``; every flow node either
declares an allowed ``provider`` or defaults to the global primary, also allowed, so a
configured-but-unused provider block never bricks an otherwise-valid run). What differs is what the
answer does:

* :func:`check_isolation` — "is this provider's configuration legal?" The verdict is fatal: under
  ``security.strict_isolation`` the run stops before a branch is ever created (see
  :meth:`Orchestrator._drive_via_engine`) and ``worc preflight`` reports a failure.
* :func:`describe_host_floor` — "does an OS-enforced write floor exist for this run at all?" The
  verdict is a loud line and nothing more. A host without a sandbox is still a host an operator has
  to work on, and the advanced mode is an operator who chose to do without one, so refusing either
  would leave them with neither the guarantee nor the work; instead the loss is stated in full, in
  preflight, in the run log and in the prompt every node receives, and the run continues.

The provider-specific meaning of both stays in the adapters (``providers/*.py``) — Codex's sandbox,
Claude's permission mode and Bash-sandbox host classes — so this module holds no CLI syntax; it only
dispatches by :class:`~wastech_orchestrator.providers.base.ProviderId` and frames the answers.

Read-isolation is orthogonal to the fatal gate. The operator escape hatch
``security.disable_read_isolation`` — like the master ``strict_isolation: false`` — relaxes only the
READ side, and only the *native discovery* half of it: the private read-deny projection (``.worc``,
the env-file, the frozen bundles) stays ``Read``-denied at either value; the provider CLIs' own
config homes carry no deny at all, at either value. This preflight validates
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
from typing import Protocol

from wastech_orchestrator.config.schema import OrchestratorConfig, ProviderConfig
from wastech_orchestrator.providers.base import ProviderId

# A provider's offline "is this configuration legal?" check, keyed by provider id. The concrete
# implementations live in the adapters (Codex's profile/authority flags, Claude's permission mode);
# the composition root binds them and injects the table so this module imports no concrete adapter.
IsolationCheck = Callable[[ProviderConfig], list[str]]


# A provider's offline "what can this run not enforce?" answer: prose naming the missing floor (and
# its remedy where one exists), or ``None`` when the floor can exist here.
#
# ``strict_isolation`` is the ONE configuration value it takes, and the narrowness is the point.
# The question used to be purely "what can this machine do", and the callable was zero-arg so it
# could not drift into a per-config verdict; since the owner's 2026-09-03 decision that framing is
# no longer answerable, because the advanced mode removes Claude's sandbox on every host — a
# host-only answer would report a floor that this run does not have. What still keeps it from
# drifting back into a refusal is the caller rather than the signature: :func:`describe_host_floor`
# returns lines and never a verdict, and the fatal question stayed where it was
# (:func:`check_isolation`, plus the adapters' per-attempt refusals). A ``Protocol`` rather than a
# ``Callable`` alias so each adapter may keep its own injectable test seam (Claude's ``capability``,
# Codex's ``system``) behind a default.
class HostFloorCheck(Protocol):
    """A provider-owned "is there an OS-enforced write floor for this run?" answer."""

    def __call__(self, *, strict_isolation: bool) -> str | None:
        """Return prose naming the missing floor, or ``None`` when one can exist here."""
        ...


# What is lost wherever no OS-enforced write floor exists, stated rather than softened. ``.worc`` is
# the more expensive half of the two: with it writable a frozen control plane can be swapped without
# detection, the exchange cannot be quarantined, and ``state.db`` can be rewritten.
_FLOOR_LOSS = (
    "No OS-enforced write floor exists here, so nothing outside the agent CLI's own tool policy "
    "keeps a write out of the clone's .git or out of .worc — and with .worc writable, "
    "control-plane tamper detection, exchange quarantine and state.db integrity are unenforced"
)

# The two tails are adjacent on purpose: both describe the same missing floor, and keeping them one
# line apart is what stops one of them from drifting into a claim the other contradicts.
_FLOOR_LOSS_SHELL_UNSANDBOXED = (
    "; with strict_isolation=false EVERY node here keeps an unsandboxed shell, read-only ones "
    "included, so anything any of them starts — which no tool policy sees at all — reaches both "
    "paths directly, and only the prompt's advisory contract and after-the-fact drift detection "
    "remain"
)
_FLOOR_LOSS_SHELL_WITHHELD = (
    "; with strict_isolation=true the shell is withheld rather than unsandboxed (dropped from the "
    "tool set, or the attempt refused), which closes the command-execution path and leaves the "
    "CLI's own file-editing denies as the only boundary"
)


# The mode's own announcement, in one formatter for the reason `describe_host_floor` is: preflight
# and the run log must not be able to describe the same configuration differently, and the two
# neighbouring relaxation lines (`read-isolation: OFF`, `git-evidence: ON`) are hand-written twice
# each and have already drifted in wording. Subject + ON/OFF + the key that caused it, matching the
# shape those two established.
#
# One line, not a recital: the six axes (environment, redaction, tools, write, network) and the
# four floor levels are spelled out in `guide/config/security.md`, where they are read once, instead
# of being recited into every preflight report and every run log and scrolled past. The report still
# announces that the mode is on and names the key that turned it on — a weakening is never silent —
# it just does not restate the whole document.
_MODE_SUBJECT = (
    "advanced-mode: ON (security.strict_isolation=false) — full freedom for the agent under the "
    "operator's responsibility, except the floor; guide/config/security.md says what that floor "
    "holds and what it does not"
)


def describe_advanced_mode(config: OrchestratorConfig) -> tuple[str, ...]:
    """The mode's announcement: one line, or nothing at all when the mode is off.

    A tuple rather than ``str | None`` so a caller can extend its report unconditionally, and one
    formatter rather than two literals so preflight and the run log cannot describe the same
    configuration differently. What this *host* cannot enforce is :func:`describe_host_floor`'s
    answer, printed beside this one — merging the two would let a host-specific gap read as a
    property of the mode.
    """
    return () if config.security.strict_isolation else (_MODE_SUBJECT,)


def check_isolation(
    config: OrchestratorConfig, checks: Mapping[ProviderId, IsolationCheck]
) -> list[str]:
    """Return a reason per provider whose configuration is illegal; ``[]`` means all OK.

    Pure and deterministic (no CLI launched, and no host probing — the same config file gets the
    same verdict on every machine). ``checks`` is the ProviderId→isolation-check table injected by
    the composition root (so this module imports no concrete adapter). Each reason is prefixed with
    the provider id so the caller can surface a single combined message.
    """
    reasons: list[str] = []
    for provider_id in _providers_in_use(config):
        provider_cfg = config.agents.providers.get(provider_id)
        check = checks.get(provider_id)
        if provider_cfg is None or check is None:
            continue
        reasons.extend(f"{provider_id.value}: {reason}" for reason in check(provider_cfg))
    return reasons


def describe_host_floor(
    config: OrchestratorConfig, checks: Mapping[ProviderId, HostFloorCheck]
) -> tuple[str, ...]:
    """One line per provider with no OS-enforced write floor for this run; ``()`` when all have one.

    Each line names the gap and then what it costs, and ``security.strict_isolation`` decides both
    halves because the truth does. The gap half is the provider's answer to it: under
    ``strict_isolation: false`` Claude raises no sandbox on any host, while Codex still gets its
    generated profile — so the mode produces a Claude line on a machine that reports nothing under
    strict isolation, and never a Codex one. The cost half follows the same flag: with it off the
    shell runs unsandboxed and anything it starts reaches the denied paths, with it on the attempt
    loses its shell instead. Both live in one formatter, so preflight, the run log and the prompt
    preamble can never describe the same run differently; the caller supplies its own framing (a
    verdict line, a log record, a paragraph).
    """
    tail = (
        _FLOOR_LOSS_SHELL_WITHHELD
        if config.security.strict_isolation
        else _FLOOR_LOSS_SHELL_UNSANDBOXED
    )
    lines: list[str] = []
    for provider_id in _providers_in_use(config):
        check = checks.get(provider_id)
        gap = (
            check(strict_isolation=config.security.strict_isolation) if check is not None else None
        )
        if gap is None:
            continue
        lines.append(f"{provider_id.value}: {gap}. {_FLOOR_LOSS}{tail}")
    return tuple(lines)


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
