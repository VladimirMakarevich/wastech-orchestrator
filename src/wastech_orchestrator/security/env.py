"""Environment allowlist.

A child process started by the orchestrator receives **only** the environment variables named in
``security.allowed_environment`` that are present in the parent environment — never the parent's
full environment. No secret or token is ever forwarded implicitly; git/agent credentials are
configured outside the orchestrator (.agents/rules/security.md).

The **default** allowlist is OS-aware: on top of a cross-platform base it adds the OS-launch
essentials a freshly spawned process needs to start at all on the host OS (see
:func:`default_allowed_environment`). This keeps the allowlist the single gate (security rule #4)
while making a fresh install work out of the box on Windows, Linux, and macOS.

This module has no provider knowledge and is reused by every adapter (P2/P3) and the Check
Runner (P5).
"""

from __future__ import annotations

import os
import platform
from collections.abc import Mapping, Sequence

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


def build_child_env(
    allowed_keys: Sequence[str],
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the child environment from only the allowlisted keys present in the parent.

    :param allowed_keys: the ``security.allowed_environment`` allowlist, in order.
    :param parent_env: the environment to draw from; defaults to the live ``os.environ``.
    :returns: a fresh dict containing exactly the allowlisted keys that exist in ``parent_env``,
        in allowlist order. A key absent from the parent is skipped (never added as empty).
    """
    source: Mapping[str, str] = os.environ if parent_env is None else parent_env
    return {key: source[key] for key in allowed_keys if key in source}
