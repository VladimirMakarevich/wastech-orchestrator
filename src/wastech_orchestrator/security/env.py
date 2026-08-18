"""Environment allowlist.

A child process started by the orchestrator receives **only** the environment variables named in
``security.allowed_environment`` that are present in the parent environment — never the parent's
full environment — plus the ones the operator **assigns** outright in
``security.extra_environment``. Both halves are name-gated policy: no secret or token is ever
forwarded implicitly, and git/agent credentials are configured outside the orchestrator.

An ``allowed_environment`` entry may also be a **prefix pattern** (``DOTNET_*``): still an
allow-list by name, just one that does not require the operator to know every name a toolchain
invents. Patterns are resolved against the parent environment by :func:`expand_allowed_environment`,
and every name a pattern produces passes the secret-name filter afterwards — so a pattern can never
forward what a plain name could not.

The **default** allowlist is OS-aware: on top of a cross-platform base it adds the OS-launch
essentials a freshly spawned process needs to start at all on the host OS (see
:func:`default_allowed_environment`). This keeps the allowlist the single gate
while making a fresh install work out of the box on Windows, Linux, and macOS.

This module has no provider knowledge — its one provider-layer import is the shared secret-name
policy, so there is exactly one definition of "secret name" in the product — and it is reused by
every adapter and the Check Runner.
"""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from wastech_orchestrator.config.schema import SecurityConfig
from wastech_orchestrator.providers.redaction import is_sensitive_key

# Cross-platform base: the few names every OS needs for the agent CLIs to find their binaries
# (PATH), their home/config dirs, and their credentials. ``USER`` is the macOS Keychain login
# (subscription/OAuth CLIs report "Not logged in" without it); ``USERPROFILE`` is the Windows home.
# A name absent from the host OS is simply skipped by :func:`build_child_env`, so the union is safe.
_BASE_ALLOWED_ENV: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "USERPROFILE",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
)

# Windows OS-launch essentials. These carry no secrets. ``SystemRoot`` is load-bearing: the
# Node-based ``claude.exe`` aborts at startup with exit ``0xC0000409`` (STATUS_STACK_BUFFER_OVERRUN)
# — before printing anything — when it is unset, so preflight would report the CLI "did not
# succeed". The rest cover temp dirs, the command/extension resolution a child may need to launch
# its own tools, and the per-user profile dirs the CLIs read their config/auth from.
_WINDOWS_ESSENTIAL_ENV: tuple[str, ...] = (
    "SystemRoot",
    "SystemDrive",
    "windir",
    "ComSpec",
    "PATHEXT",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)

# POSIX OS-launch essentials (Linux/macOS, and WSL — which reports as Linux). The agent CLIs
# already start with the base list here; these are the dynamic-linker / temp names some toolchains
# need. Secret-free, like the Windows set.
_POSIX_ESSENTIAL_ENV: tuple[str, ...] = (
    "TMPDIR",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
)


def os_essential_env(system: str | None = None) -> tuple[str, ...]:
    """The OS-launch essentials for ``system`` (default: the current OS via ``platform.system()``).

    ``platform.system()`` returns ``"Windows"`` on Windows, ``"Linux"`` on Linux **and WSL**, and
    ``"Darwin"`` on macOS. WSL needs no special handling: inside it the agent CLIs are Linux
    binaries, so the POSIX set is correct. (Driving a Windows ``.exe`` agent from inside WSL via
    interop is unsupported — add any extra names to ``allowed_environment`` by hand.)
    """
    name = system if system is not None else platform.system()
    return _WINDOWS_ESSENTIAL_ENV if name == "Windows" else _POSIX_ESSENTIAL_ENV


def default_allowed_environment(system: str | None = None) -> tuple[str, ...]:
    """The default ``security.allowed_environment``: cross-platform base + OS-launch essentials.

    Used both as the loader fallback (a config that omits the key) and as the value the installer
    writes, so a fresh install works out of the box on the OS it was generated on. The list never
    carries secrets; :func:`build_child_env` still drops any name absent from the parent
    environment, so the OS-specific entries are inert on the other OSes.
    """
    return _BASE_ALLOWED_ENV + os_essential_env(system)


# The one wildcard the grammar allows: a single trailing ``*`` turns an entry into a prefix pattern.
_PATTERN_SUFFIX = "*"


def env_pattern_prefix(entry: str) -> str | None:
    """The prefix of a prefix-pattern ``allowed_environment`` entry, or ``None`` for a plain name.

    ``"DOTNET_*"`` → ``"DOTNET_"``; ``"DOTNET_ROOT"`` → ``None``. The single source of truth for the
    grammar's shape, shared by the config validator — which is what rejects every *other* use of
    ``*`` (a lone ``*``, ``A*B``, ``**``, ``*SUFFIX``) — and by :func:`expand_allowed_environment`.
    A malformed entry therefore never reaches run time; if one somehow did, it would behave as an
    exact name that matches nothing, never as a wider pattern.
    """
    return entry[: -len(_PATTERN_SUFFIX)] if entry.endswith(_PATTERN_SUFFIX) else None


@dataclass(frozen=True)
class PatternExpansion:
    """What one prefix pattern matched in the parent environment — the unit of diagnostics.

    ``kept`` are the names forwarded, ``dropped`` the ones the secret-name filter refused. Both are
    reported because a pattern is deliberately allowed to be wider than the operator realized (no
    minimum prefix length, no ceiling on the match count): what it pulled in is *shown* instead of
    being silently forwarded or silently dropped. A pattern that matched nothing yields two empty
    tuples, which is the most useful record of the three — it is almost always a typo (``DOTNET*``
    for ``DOTNET_*``) or a toolchain that is not installed on this host, and today neither is
    visible anywhere.
    """

    pattern: str
    kept: tuple[str, ...]
    dropped: tuple[str, ...]


def expand_allowed_environment(
    names: Sequence[str],
    parent_env: Mapping[str, str] | None = None,
    *,
    system: str | None = None,
) -> tuple[tuple[str, ...], tuple[PatternExpansion, ...]]:
    """Resolve the prefix patterns in *names* against *parent_env*.

    Deliberately a **pure** function that prints nothing: the callers that already talk to the
    operator — ``worc preflight`` and the run's start-of-flow announcement — print the returned
    expansions once and up front, while :func:`build_child_env` uses the resolved names in silence
    for each of the many child processes it builds. (A logger inside this module with a global set
    of
    "already warned" was rejected: it would put state into a module that has none, and the state
    would need resetting between tests.)

    Three properties the callers rely on:

    * **a plain name passes through untouched**, in place — so a config with no pattern gets back
      its
      own list and today's child environment byte for byte;
    * **the secret-name filter runs after expansion**, by the one policy that defines a secret name
      (:func:`~wastech_orchestrator.providers.redaction.is_sensitive_key`). ``NUGET_*`` forwards
      ``NUGET_PACKAGES`` and refuses ``NUGET_API_KEY``; a pattern can never widen the gate past what
      a plain name is allowed through. The refused value also stays *under redaction*, because the
      redaction harvest matches the allow-list by exact name and the list holds ``NUGET_*``, not the
      name it refused;
    * **matches are added sorted**, because ``os.environ`` iteration order is not reproducible while
      the result is compared in tests and printed in preflight.

    :param names: the ``allowed_environment`` entries, plain names and patterns mixed, in config
        order.
    :param parent_env: the environment to match patterns against; defaults to ``os.environ``.
    :param system: the OS deciding case sensitivity, defaulting to ``platform.system()``. Windows
        matches case-insensitively because its environment is; POSIX matches exactly. The branch is
        explicit rather than inferred from the parent environment's own spellings.
    :returns: ``(effective_names, expansions)`` — the de-duplicated names to forward, and one record
        per pattern in config order (empty when the config holds no pattern).
    """
    source: Mapping[str, str] = os.environ if parent_env is None else parent_env
    fold = (system if system is not None else platform.system()) == "Windows"
    effective: list[str] = []
    seen: set[str] = set()
    expansions: list[PatternExpansion] = []
    for entry in names:
        prefix = env_pattern_prefix(entry)
        if prefix is None:
            if entry not in seen:
                seen.add(entry)
                effective.append(entry)
            continue
        probe = prefix.upper() if fold else prefix
        matched = sorted(key for key in source if (key.upper() if fold else key).startswith(probe))
        kept = tuple(key for key in matched if not is_sensitive_key(key))
        expansions.append(
            PatternExpansion(
                pattern=entry,
                kept=kept,
                dropped=tuple(key for key in matched if is_sensitive_key(key)),
            )
        )
        for key in kept:
            if key not in seen:
                seen.add(key)
                effective.append(key)
    return tuple(effective), tuple(expansions)


def describe_expansions(expansions: Sequence[PatternExpansion]) -> tuple[str, ...]:
    """One operator-facing line per prefix pattern: what it matched, and what it refused.

    A pure formatter rather than a logger, for the reason :func:`expand_allowed_environment` is pure
    — but shared, because the two surfaces that print this (``worc preflight`` and the run's
    start-of-flow announcement) have to say the same thing about the same config. Wording lives here
    for the same reason :func:`launch_critical_env_issue` returns prose: there is one explanation of
    the mechanism, not one per caller.

    Names only, never values — a name is already in the operator's config, whereas a value is the
    thing worth leaking. The zero-match line spells out its own likely causes, because a pattern
    that matched nothing is the case an operator cannot otherwise tell from one that worked.
    """
    lines: list[str] = []
    for item in expansions:
        kept = f"{len(item.kept)} name(s)"
        if item.kept:
            kept += f" ({', '.join(item.kept)})"
        dropped = (
            f", {len(item.dropped)} dropped as secret-named ({', '.join(item.dropped)})"
            if item.dropped
            else ""
        )
        tail = (
            ""
            if item.kept or item.dropped
            else " — nothing in this environment starts with that prefix (a typo, or the toolchain "
            "is not installed on this host)"
        )
        lines.append(f"{item.pattern} \u2192 {kept}{dropped}{tail}")
    return tuple(lines)


def env_name_is_covered(allowed_environment: Sequence[str], name: str) -> bool:
    """Whether ``allowed_environment`` covers *name* — spelled exactly, or reachable via a pattern.

    The one rule shared by the two host-independent/host-specific gates that assert a name is not
    missing from the list (``PATH`` in the validator, ``SystemRoot`` in
    :func:`launch_critical_env_issue`). Both predate patterns and compared literally, so without
    this
    a legitimate ``PATH*`` / ``SYSTEM*`` would be reported as the very omission it is not.

    Coverage is decided on the *config alone* — a pattern counts when *name* starts with its prefix,
    whether or not this host's environment currently holds the variable. That keeps the validator's
    verdict host-independent, and makes the preflight gate report the config's real intent.
    Comparison is case-sensitive; the ``SystemRoot`` caller folds case by passing pre-upper-cased
    arguments, which is correct there because that verdict is Windows-only.
    """
    for entry in allowed_environment:
        prefix = env_pattern_prefix(entry)
        if prefix is None:
            if entry == name:
                return True
        elif name.startswith(prefix):
            return True
    return False


def launch_critical_env_issue(
    allowed_environment: Sequence[str], system: str | None = None
) -> str | None:
    """The host-specific reason ``allowed_environment`` cannot launch a child CLI, or ``None``.

    Only one name qualifies, and only on one OS: without ``SystemRoot`` the Windows CLI aborts
    before printing anything (see :data:`_WINDOWS_ESSENTIAL_ENV`), so the operator sees "CLI did not
    succeed" and nothing else. The remaining OS-launch essentials only degrade a child (no temp dir,
    no extension resolution), so they are not asserted here.

    The verdict depends on the host, which is why it belongs to ``worc preflight`` and never to
    ``validate_config``: the same config file must get the same verdict on every machine. The
    host-independent half of the same rule — ``PATH`` is mandatory everywhere — is a validator
    error.

    Matching is case-insensitive because it is Windows-only and Windows environment names are: a
    config spelling the name ``SYSTEMROOT`` forwards it just as well, so failing it would be a false
    alarm. It also honors a prefix pattern (``SYSTEM*`` covers the name), via the shared
    :func:`env_name_is_covered`: a config that legitimately forwards ``SystemRoot`` through a
    pattern
    must not be reported as the omission it is not.
    """
    name = system if system is not None else platform.system()
    if name != "Windows":
        return None
    folded = [entry.upper() for entry in allowed_environment]
    if env_name_is_covered(folded, "SYSTEMROOT"):
        return None
    return (
        "security.allowed_environment omits 'SystemRoot' (the list replaces the default wholesale) "
        "— on Windows the Node-based claude.exe aborts at startup with exit 0xC0000409 before "
        "printing anything, so a run would only report that the CLI did not succeed. "
        "Add 'SystemRoot' to the list."
    )


def build_child_env(
    security: SecurityConfig,
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the child environment: forwarded allowlisted names, then assigned extras.

    Takes the whole :class:`SecurityConfig` rather than the two fields it reads, and that is a
    deliberate contract: the failure mode this key introduces is *partial* delivery — the agent sees
    a toolchain variable and the Check Runner does not, so a task dies on the quality gate after the
    agent already succeeded. A call site that must pass a policy object cannot build an environment
    that silently omits half the policy, which makes a new call site a type error rather than a code
    review someone has to catch.

    Prefix patterns in ``allowed_environment`` are resolved here through
    :func:`expand_allowed_environment`, silently — the diagnostics belong to the callers that print
    once per run, not to a builder invoked for every child process.

    :param security: the operator's security policy; ``allowed_environment`` names what is forwarded
        from *parent_env*, ``extra_environment`` names what is assigned outright.
    :param parent_env: the environment to draw from; defaults to the live ``os.environ``.
    :returns: a fresh dict holding the allowlisted keys that exist in *parent_env* in allowlist
        order (a pattern's matches sorted, in the pattern's place), then the assigned names in
        config
        order. A forwarded key absent from the parent is skipped (never added as empty); an assigned
        name in both wins, keeping its forwarded position. The order is part of the contract — the
        result is compared in tests, and an unordered result would make a run's environment
        irreproducible.
    """
    source: Mapping[str, str] = os.environ if parent_env is None else parent_env
    forwarded, _ = expand_allowed_environment(security.allowed_environment, source)
    child = {key: source[key] for key in forwarded if key in source}
    child.update(security.extra_environment)
    return child
