"""Environment allowlist.

Under ``security.strict_isolation`` a child process started by the orchestrator receives **only**
the environment variables named in ``security.allowed_environment`` that are present in the parent
environment — never the parent's full environment — plus the ones the operator **assigns** outright
in ``security.extra_environment``. Both halves are name-gated policy: no secret or token is ever
forwarded implicitly, and git/agent credentials are configured outside the orchestrator.

``strict_isolation: false`` is the operator's advanced mode, and there the gate is not the policy:
a child run on the agent's behalf gets the parent environment whole (:func:`build_child_env`), while
the orchestrator's own ``git``/``gh`` processes keep the allowlist (:func:`build_orchestrator_env`).
Everything below still describes the allowlist itself, which both the strict policy and the
orchestrator's own processes use, and which stays the operator's declared intent either way.

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
from typing import TYPE_CHECKING

from wastech_orchestrator.env_file import env_file_names
from wastech_orchestrator.providers.redaction import is_sensitive_key

if TYPE_CHECKING:
    from wastech_orchestrator.config.schema import SecurityConfig

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
    The lone ``*`` is deliberately returned as ``None`` too. The validator reports its specific
    grammar error, while this lower-level helper remains fail-closed if a caller bypasses that gate:
    a malformed inversion can never expand to the whole process environment.
    """
    if entry == _PATTERN_SUFFIX:
        return None
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
    source_by_fold = {key.upper(): key for key in source} if fold else {}
    for entry in names:
        prefix = env_pattern_prefix(entry)
        if prefix is None:
            actual = source_by_fold.get(entry.upper(), entry) if fold else entry
            identity = actual.upper() if fold else actual
            if identity not in seen:
                seen.add(identity)
                effective.append(actual)
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
            identity = key.upper() if fold else key
            if identity not in seen:
                seen.add(identity)
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
    """The host-specific reason the allowlist cannot launch required Windows children, or ``None``.

    Only one name qualifies, and only on one OS. Orchestrator-owned git/gh uses this allowlist in
    every mode, so the verdict is unconditional. Under strict isolation the same omission also
    reaches Node-based agent CLIs; a real ``claude.exe`` launch demonstrated an abort before output
    with ``0xC0000409``. The remaining OS-launch essentials only degrade a child, so they are not
    asserted here.

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
        "— orchestrator-owned git/gh cannot be launched reliably on Windows in either mode; under "
        "strict isolation a Node-based agent CLI may also abort before printing anything "
        "(observed with claude.exe as exit 0xC0000409). "
        "Add 'SystemRoot' to the list."
    )


def _name_identity(name: str, *, system: str) -> str:
    """The environment-name identity for *system* (Windows names are case-insensitive)."""
    return name.upper() if system == "Windows" else name


def _allowlisted_names(
    security: SecurityConfig,
    source: Mapping[str, str],
    *,
    system: str,
    withhold_env_file: bool,
) -> tuple[str, ...]:
    """Resolve allowlisted names, optionally excluding implicit env-file pattern matches.

    The orchestrator loads ``.worc/.env`` into its own process before building children. Agent-side
    environments must not receive a value merely because a prefix pattern happened to match its
    non-secret-looking name: that would route around the provider read-deny on the file. An exact
    ``allowed_environment`` entry remains an explicit strict-mode grant, and
    ``extra_environment`` remains the explicit restoration in either mode. Orchestrator-owned
    git/gh is not an agent and keeps the ordinary allowlist behavior.
    """
    forwarded, _ = expand_allowed_environment(security.allowed_environment, source, system=system)
    if not withhold_env_file:
        return forwarded
    explicit = {
        _name_identity(name, system=system)
        for name in security.allowed_environment
        if env_pattern_prefix(name) is None
    }
    withheld = {
        _name_identity(name, system=system)
        for name in env_file_names()
        if _name_identity(name, system=system) not in explicit
    }
    return tuple(name for name in forwarded if _name_identity(name, system=system) not in withheld)


def _assemble(
    security: SecurityConfig,
    source: Mapping[str, str],
    forwarded: Sequence[str],
    *,
    system: str,
) -> dict[str, str]:
    """Forward *forwarded* out of *source*, then assign ``extra_environment`` on top.

    The half both policies share, so neither can drift on the two properties tests pin: a name
    missing from the parent is skipped rather than added empty, and an assigned name keeps its
    forwarded position while taking the assigned value.
    """
    child = {key: source[key] for key in forwarded if key in source}
    if system != "Windows":
        child.update(security.extra_environment)
        return child

    # A plain dict is case-sensitive even when it models a Windows environment. Preserve the
    # forwarded key's position/spelling, but replace its value case-insensitively so an assignment
    # cannot leave two spellings whose winner is deferred to CreateProcess.
    by_identity = {key.upper(): key for key in child}
    for name, value in security.extra_environment.items():
        existing = by_identity.get(name.upper())
        if existing is not None:
            child[existing] = value
        else:
            child[name] = value
            by_identity[name.upper()] = name
    return child


def build_child_env(
    security: SecurityConfig,
    parent_env: Mapping[str, str] | None = None,
    *,
    system: str | None = None,
) -> dict[str, str]:
    """Build the environment for a child the orchestrator runs **on the agent's behalf**.

    Two policies, chosen by ``security.strict_isolation``:

    * **strict isolation on** — the allowlist gate described in this module's docstring: only the
      names ``allowed_environment`` covers, then the assigned extras. A prefix pattern cannot
      implicitly forward a name loaded from the orchestrator env-file; an exact entry can.
    * **advanced mode** (``strict_isolation: false``) — the parent environment is forwarded
      **whole**, with no name gate at all, because that switch now means "full freedom under the
      operator's responsibility, except the floor", and a toolchain the agent has to drive needs the
      variables the operator's own shell has. Two things are still not the parent's to give: the
      names the orchestrator's own env-file defines
      (:func:`~wastech_orchestrator.env_file.env_file_names` — the agent is denied reading that
      file, so forwarding its contents would route around the deny), and whatever
      ``extra_environment`` assigns on top, which wins here exactly as it does above. The redaction
      net compensates: with the gate gone the harvest stops exempting allowlisted names (see
      :func:`~wastech_orchestrator.providers.redaction.secret_env_values`), so a secret-named value
      that is now forwarded gets scrubbed out of logs and artifacts instead of left in them.

    Takes the whole :class:`SecurityConfig` rather than the fields it reads, and that is a
    deliberate contract: the failure mode here is *partial* delivery — the agent sees a toolchain
    variable and the Check Runner does not, so a task dies on the quality gate after the agent
    already succeeded. A call site that must pass a policy object cannot build an environment that
    silently omits half the policy, which makes a new call site a type error rather than a code
    review someone has to catch. It is also why the mode is decided inside this function rather than
    at the five call sites: they cannot drift apart. The orchestrator's own git/gh processes are the
    one exemption, and they say so by calling a different function —
    :func:`build_orchestrator_env`.

    Prefix patterns in ``allowed_environment`` are resolved here through
    :func:`expand_allowed_environment`, silently — the diagnostics belong to the callers that print
    once per run, not to a builder invoked for every child process.

    :param security: the operator's security policy; ``allowed_environment`` names what is forwarded
        from *parent_env* under strict isolation, ``extra_environment`` names what is assigned
        outright under either policy.
    :param parent_env: the environment to draw from; defaults to the live ``os.environ``.
    :returns: a fresh dict. Under strict isolation: the allowlisted keys present in *parent_env*, in
        allowlist order (a pattern's matches sorted, in the pattern's place), minus implicit
        env-file pattern matches, then the assigned names in config order. In advanced mode: every
        parent name except the env-file's, sorted, then the assigned names. A forwarded key absent
        from the parent is skipped (never added as empty); an assigned name in both wins, keeping
        its forwarded position. The order is part of the contract — the result is compared in
        tests, and an unordered result would make a run's environment irreproducible, which is why
        the mode sorts instead of inheriting ``os.environ``'s unreproducible iteration order.
    """
    source: Mapping[str, str] = os.environ if parent_env is None else parent_env
    host = system if system is not None else platform.system()
    if security.strict_isolation:
        return _assemble(
            security,
            source,
            _allowlisted_names(security, source, system=host, withhold_env_file=True),
            system=host,
        )
    withheld = {_name_identity(name, system=host) for name in env_file_names()}
    forwarded = tuple(
        sorted(key for key in source if _name_identity(key, system=host) not in withheld)
    )
    return _assemble(security, source, forwarded, system=host)


def build_orchestrator_env(
    security: SecurityConfig,
    parent_env: Mapping[str, str] | None = None,
    *,
    system: str | None = None,
) -> dict[str, str]:
    """Build the environment for a child the orchestrator runs **as itself** — its ``git``/``gh``.

    The allowlist gate, at every value of ``strict_isolation``: advanced mode widens what the
    *agent* may do, and these processes are not the agent. They need no toolchain variable, and the
    cost of a mistake here is somebody else's repository — a shell ``GH_REPO`` would retarget a pull
    request, and ``GIT_DIR`` / ``GIT_WORK_TREE`` would move both the commands and the resolved
    control paths, so the write-guard would be protecting the wrong ``.git``. An operator who
    genuinely needs a proxy on this path assigns it through ``extra_environment``, which reaches
    here. Names that retarget publication or substitute Git's transport are additionally scrubbed
    by the caller (see ``_GIT_ENV_SCRUB`` in :mod:`wastech_orchestrator.git_manager`), because
    ``extra_environment`` can assign one and an allowlist entry can forward one.

    A separate function rather than a flag on :func:`build_child_env`: the exemption is legible at
    the call site, and a new agent-side call site cannot accidentally opt into it.
    """
    source: Mapping[str, str] = os.environ if parent_env is None else parent_env
    host = system if system is not None else platform.system()
    return _assemble(
        security,
        source,
        _allowlisted_names(security, source, system=host, withhold_env_file=False),
        system=host,
    )
