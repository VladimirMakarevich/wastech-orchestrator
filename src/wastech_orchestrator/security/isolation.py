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
* :func:`describe_host_floor` — "can an OS-enforced write floor exist on this host at all?" The
  verdict is a loud line and nothing more. A host without a sandbox is still a host an operator has
  to work on, so refusing the run there would leave them with neither the guarantee nor the work;
  instead the loss is stated in full, in preflight and in the run log, and the run continues.

The provider-specific meaning of both stays in the adapters (``providers/*.py``) — Codex's sandbox,
Claude's permission mode and Bash-sandbox host classes — so this module holds no CLI syntax; it only
dispatches by :class:`~wastech_orchestrator.providers.base.ProviderId` and frames the answers.

Read-isolation is orthogonal to the fatal gate. The operator escape hatch
``security.disable_read_isolation`` — like the master ``strict_isolation: false`` — relaxes only the
READ side, and only the *native discovery* half of it: the private read-deny projection (``.worc``,
the env-file, the frozen bundles) stays ``Read``-denied at either value, with Claude's own config
home the one carve-out (``allow_native_memory`` governs that path alone). This preflight validates
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

# A provider's offline "is this configuration legal?" check, keyed by provider id. The concrete
# implementations live in the adapters (Codex's profile/authority flags, Claude's permission mode);
# the composition root binds them and injects the table so this module imports no concrete adapter.
IsolationCheck = Callable[[ProviderConfig], list[str]]

# A provider's offline "what can this host not enforce?" answer: prose naming the missing floor (and
# its remedy where one exists), or ``None`` when the floor can exist here. It takes no argument on
# purpose — the answer describes the machine, not the configuration, so it cannot drift into a
# per-config verdict and back into a refusal.
HostFloorCheck = Callable[[], str | None]

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
# existing relaxation lines (`read-isolation: OFF`, `git-evidence: ON`) are hand-written twice each
# and have already drifted in wording. Subject + ON/OFF + the key that caused it, matching the shape
# those two established, then one indented line per floor level.
_MODE_SUBJECT = (
    "advanced-mode: ON (security.strict_isolation=false) — full freedom for the agent under the "
    "operator's responsibility, except the floor below"
)

# What the mode changes. Written per axis so each line became true as its phase landed, rather than
# one sentence that was part-false while the campaign ran; with the write and network axes below all
# four are now present-tense true.
_MODE_ENVIRONMENT = (
    "environment: the parent environment is forwarded WHOLE to agent processes, the check "
    "commands, the scanners and the tool nodes — security.allowed_environment is not consulted "
    "for them. Withheld: the names the orchestrator's own env-file defines (return one with "
    "security.extra_environment). The orchestrator's own git/gh keep the allowlist"
)
_MODE_REDACTION = (
    "redaction: secret-named values are scrubbed from logs and artifacts by NAME alone now, "
    "without the allowlist excusing any — so a secret-named variable holding something harmless "
    "may appear as [REDACTED] in output"
)
_MODE_TOOLS = (
    "tools: the agent CLI's built-in tool set is no longer gated by an allowlist, so EVERY node "
    "gets a shell — read-only ones included — and a tool shipped by a future CLI release is "
    "available the day it ships, without anyone here having read its name. What is still denied: "
    "the file-editing tools on .git, .worc and the task tree, which is the floor; and four names "
    "that are friction rather than a boundary, since a shell walks around all four. Persistence is "
    "NOT held — a task can leave behind a launch agent, a systemd unit or a shell rc line"
)

_MODE_WRITE = (
    "write: the agent may write ANYWHERE ON THIS VOLUME that the host lets the sandbox reach, "
    "not only inside the clone — a toolchain cache under $HOME, a scratch tree in the system "
    "temp, and just as much a directory on PATH or a shell rc file. Say that one out loud: the "
    "right to write a directory on PATH is the right to replace an executable that later runs "
    "OUTSIDE the sandbox, including one this orchestrator launches as itself. One volume, not "
    "every volume: the grant is expressed as the workspace path's anchor, so on Windows a "
    "second drive (D:) stays unwritable — a toolchain cache kept there will still fail, and it "
    "will look like a broken toolchain. What is still write-denied is floor 1 below, plus the "
    "agent CLIs' own config homes (~/.claude / $CLAUDE_CONFIG_DIR, $CODEX_HOME), because one of "
    "those files is loaded as configuration on the shipped default. "
    "agents.providers.claude.allow_native_memory narrows that last one and only that one: it "
    "opens the per-project memory store under the Claude home (projects/<slug>/memory) while "
    "the credential and settings layers above it stay write-denied"
)
_MODE_NETWORK = (
    "network: EVERY node reaches the whole network whatever its flow granted, and so do the CLIs' "
    "own web tools. That is three surfaces, not one boundary: the sandboxed shell (a sandbox "
    "policy), the built-in WebFetch/WebSearch (which do not pass through it), and Codex's "
    "web_search (which runs on its backend, outside the permission profile). There is no domain "
    "filtering and none is planned — an allowlist that had to pass github.com for ordinary "
    "dependencies would hold nothing, and on Codex a domain list is not enforced at all without an "
    "experimental feature that ships disabled"
)


# The four levels, in the words of ТA.1. The third was deliberately in the future tense for two
# phases — the agent had no network, and a line that overstates is discounted exactly like one
# that understates — and it turns present-tense here, with the phase that hands the network over.
_FLOOR_LEVELS: tuple[str, ...] = (
    (
        "floor 1 of 4 — the integrity of the task's own state is held MECHANICALLY: the clone's "
        ".git and the private .worc stay unwritable, wherever this host can enforce a sandbox at "
        "all (an isolation-floor line above says where it cannot). One qualifier, since the write "
        "granted above is filesystem-wide: what keeps these paths out of it is the carve-out being "
        "the more specific rule, which Codex re-proves under its own sandbox before every provider "
        "attempt that gets a shell — an agent node, an evaluator and the supervisor's own "
        "read-only turn alike — and Claude does not. There this level rests on the tool-level "
        "write denies. How that CLI ranks a denyWrite inside an allowWrite is a first-class "
        "construction of the vendor's own (its settings compiler carries the carve-outs as "
        "denyWithinAllow and applies them inside the allowed set) — but it is not proven on THIS "
        "host, and the instrument that can prove it is the opt-in "
        "`worc preflight --paid-isolation-probe`"
    ),
    (
        "floor 2 of 4 — publication to this repository's origin is held by DETECTION, not by "
        "prohibition: a branch or a pull request that appears without the orchestrator's own "
        "record is seen, and what happens then depends on where. During a WRITING agent node it "
        "parks the task; during any other node with a shell (a read-only agent attempt, an "
        "evaluator, a tool, the supervisor's own turn) it is a loud warning and the run continues "
        "— the operator decision of 2026-07-26, not an oversight. At publish time nothing parks "
        "for a moved branch at all: it is recovered from in four cases, and only a merge "
        "conflict, a pull request this orchestrator did not open, or failing checks over the "
        "adopted combination stop the run"
    ),
    (
        "floor 3 of 4 — publication anywhere else IS HELD BY NOTHING, and is reachable today: the "
        "agent has the network, and credentials are picked up automatically and are not withheld, "
        "so a repository assembled outside the clone and pushed to any address is neither "
        "prevented nor seen. Nothing is planned to hold it"
    ),
    (
        "floor 4 of 4 — publication AS THE ORCHESTRATOR is held by DETECTION: the user git config "
        "and the clone's own agent-CLI config are fingerprinted around the attempt, every gh call "
        "names its repository outright WHEN that repository can be named at all (an ssh alias, a "
        "file:// URL or a local path in repo.url yields no pin and no open-PR probe — worc "
        "preflight prints that as its own gh-repo-pin line), and the executables the orchestrator "
        "launches are pinned to the paths resolved at startup. Not covered, and not coverable this "
        "way: a substitution made between runs, and an edit to the installed package's own code"
    ),
)


def describe_advanced_mode(config: OrchestratorConfig) -> tuple[str, ...]:
    """The mode's loud announcement: a subject, then what it relaxes, then all four floor levels.

    Empty when the mode is off, so a caller can extend its report unconditionally. The floor lines
    are stated whether or not this host can enforce level 1 — what that host cannot do is
    :func:`describe_host_floor`'s answer, printed beside this one, and merging the two would let a
    host-specific gap read as a property of the mode.
    """
    if config.security.strict_isolation:
        return ()
    return (
        _MODE_SUBJECT,
        _MODE_ENVIRONMENT,
        _MODE_REDACTION,
        _MODE_TOOLS,
        _MODE_WRITE,
        _MODE_NETWORK,
        *_FLOOR_LEVELS,
    )


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
    """One line per provider whose host cannot enforce the write floor; ``()`` when every host can.

    Each line names the gap and then what it costs. The cost half depends on
    ``security.strict_isolation`` because the truth does: with it off the shell runs unsandboxed
    and anything it starts reaches the denied paths, with it on the attempt loses its shell instead.
    Both halves live in one formatter, so preflight and the run log can never describe the same
    host differently; the caller supplies its own framing (a verdict line, a log record).
    """
    tail = (
        _FLOOR_LOSS_SHELL_WITHHELD
        if config.security.strict_isolation
        else _FLOOR_LOSS_SHELL_UNSANDBOXED
    )
    lines: list[str] = []
    for provider_id in _providers_in_use(config):
        check = checks.get(provider_id)
        gap = check() if check is not None else None
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
