"""Environment allowlist.

A child process started by the orchestrator receives **only** the environment variables named in
``security.allowed_environment`` that are present in the parent environment — never the parent's
full environment — plus the ones the operator **assigns** outright in
``security.extra_environment``. Both halves are name-gated policy: no secret or token is ever
forwarded implicitly, and git/agent credentials are configured outside the orchestrator.

The **default** allowlist is OS-aware: on top of a cross-platform base it adds the OS-launch
essentials a freshly spawned process needs to start at all on the host OS (see
:func:`default_allowed_environment`). This keeps the allowlist the single gate
while making a fresh install work out of the box on Windows, Linux, and macOS.

This module has no provider knowledge and is reused by every adapter and the Check Runner.
"""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping, Sequence

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
    alarm.
    """
    name = system if system is not None else platform.system()
    if name != "Windows":
        return None
    if any(entry.upper() == "SYSTEMROOT" for entry in allowed_environment):
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

    :param security: the operator's security policy; ``allowed_environment`` names what is forwarded
        from *parent_env*, ``extra_environment`` names what is assigned outright.
    :param parent_env: the environment to draw from; defaults to the live ``os.environ``.
    :returns: a fresh dict holding the allowlisted keys that exist in *parent_env* in allowlist
        order, then the assigned names in config order. A forwarded key absent from the parent is
        skipped (never added as empty); an assigned name in both wins, keeping its forwarded
        position. The order is part of the contract — the result is compared in tests, and an
        unordered result would make a run's environment irreproducible.
    """
    source: Mapping[str, str] = os.environ if parent_env is None else parent_env
    child = {key: source[key] for key in security.allowed_environment if key in source}
    child.update(security.extra_environment)
    return child
