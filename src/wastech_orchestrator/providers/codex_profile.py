"""Generate the Codex permission profile.

Pure, platform-parameterized: turns the requested access level plus the orchestrator's
:class:`InternalDenyPolicy` and :class:`ProviderWriteGuardPolicy` into a Codex
``[permissions.<name>]`` profile, then renders it as one inline-table ``-c`` value the adapter
injects. Injecting the whole profile as ONE inline table is deliberate: incremental dotted ``-c``
keys fail Codex's untagged-enum parse of ``FilesystemPermissionToml`` (verified on codex-cli
0.144.4), and it never mutates the operator's ``CODEX_HOME``. The same rendered profile is proven by
the ``codex sandbox -P`` canary (:mod:`wastech_orchestrator.providers.codex_canary`) and for
``codex exec`` via ``default_permissions`` — one definition, two selectors.

Every carved path is an **absolute** key in the top ``filesystem`` table (no ``:workspace_roots``
relativization is needed): ``extends`` sets the base, the workspace root is granted read/write, and
Codex's specificity rule ``deny > write > read`` (more-specific path wins) makes the deny/read
carve-outs override the workspace grant. A directory ``deny`` covers its whole subtree, so a
``secrets/**`` blacklist entry denies the ``secrets`` directory without any glob scan.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from wastech_orchestrator.providers.base import ErrorClass, ProviderError
from wastech_orchestrator.runtime_layout import InternalDenyPolicy, ProviderWriteGuardPolicy

# The orchestrator-owned profile name. A bare name (no leading ``:``) so it never collides with a
# reserved built-in profile prefix (``:read-only`` / ``:workspace`` / ``:danger-full-access``).
PROFILE_NAME = "worc"

# Access level → the Codex built-in profile it extends. ``:read-only`` denies every write;
# ``:workspace`` grants writes inside the workspace roots (and tmp) — the orchestrator then carves
# the private/control/exchange/Git paths back out by more-specific rules.
_EXTENDS: Mapping[str, str] = {"read-only": ":read-only", "workspace-write": ":workspace"}

# Bound applied to Codex's glob scan whenever a wildcard deny is emitted, so a ``*`` pattern cannot
# force an unbounded filesystem walk. A ``dir/**`` blacklist entry is reduced to a directory deny
# (subtree-covering, no scan) and never contributes a wildcard.
_GLOB_SCAN_MAX_DEPTH = 8

# The path renderer seam: turns an orchestrator ``Path`` into the native absolute string Codex
# accepts (POSIX on macOS/Linux/WSL, drive-letter/UNC on native Windows). ``str`` is the host-native
# default; tests inject a Windows renderer to prove cross-platform emission on any host.
NativePath = Callable[[Path], str]


def toml_basic_string(value: str) -> str:
    r"""Render *value* as a TOML basic string, escaping ``\`` and ``"`` (and control chars).

    Windows paths carry backslashes, so basic-string escaping (``C:\repo`` → ``"C:\\repo"``) is
    mandatory for a valid inline table on native Windows.
    """
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def _render_inline(value: Any) -> str:
    """Render a Python value as inline TOML (string, int, bool, nested inline table)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Mapping):
        items = ", ".join(
            f"{toml_basic_string(str(k))} = {_render_inline(v)}" for k, v in value.items()
        )
        return f"{{ {items} }}"
    return toml_basic_string(str(value))


def _resolve_denied_read(
    working_directory: Path,
    pattern: str,
    *,
    strict_isolation: bool,
    to_native: NativePath,
) -> tuple[str, bool]:
    """Resolve one ``security.denied_read_paths`` entry to a native deny key + whether it globs.

    Entries are repo-relative (``.env``, ``secrets/**``); an absolute entry is honored as-is. A
    ``dir/**`` entry is reduced to the directory (a directory ``deny`` covers its subtree with
    no glob scan). Under ``strict_isolation`` an unbounded ``**`` glob that cannot be reduced
    is rejected — it cannot be proven equivalently on Linux, WSL, and native Windows.
    """
    text = pattern.strip()
    text = text.removesuffix("/**")  # deny the directory subtree without a wildcard scan
    if "**" in text:
        if strict_isolation:
            raise ProviderError(
                ErrorClass.CONFIGURATION_ERROR,
                f"denied_read_paths entry {pattern!r} uses an unbounded '**' glob that cannot be "
                "proven cross-platform under strict_isolation; use an exact path or a 'dir/**' "
                "subtree",
            )
        globs = True
    else:
        globs = "*" in text or "?" in text
    candidate = Path(text)
    resolved = candidate if candidate.is_absolute() else working_directory / candidate
    return to_native(resolved), globs


def build_codex_permission_profile(
    *,
    permission_profile: str,
    working_directory: str,
    deny_policy: InternalDenyPolicy | None,
    write_guard: ProviderWriteGuardPolicy | None,
    denied_read_paths: Sequence[str],
    network_access: bool = False,
    strict_isolation: bool = True,
    to_native: NativePath = str,
) -> dict[str, Any]:
    """Build the Codex ``[permissions.<name>]`` profile mapping for one attempt.

    ``read-only`` extends ``:read-only`` and grants the workspace read; ``workspace-write`` extends
    ``:workspace`` and grants it write, then Write/Edit-denies (as more-specific ``read`` rules) the
    exchange, resolved Git dirs, and ``tasks/`` tree from *write_guard* so they stay readable
    but immutable. Both profiles ``deny`` the *deny_policy* set (private/control homes, resolved
    env-file, frozen bundles) — unconditionally, at every
    read-isolation setting — and the public *denied_read_paths* blacklist. ``network.enabled``
    follows *network_access*, which the adapter resolves once for the attempt (the flow's grant, or
    the advanced mode, which is online for every node) — the same flag that decides the backend-side
    ``web_search``, so the shell and the native web tool cannot end up on opposite sides of it. Deny
    rules are applied last so a deny always wins on any path collision.

    ``strict_isolation: false`` — the advanced mode — additionally grants ``write`` on the workspace
    volume's ROOT, which is what stops ``dotnet build`` failing on ``~/.nuget``. The floor's
    carve-outs survive it by being more specific paths (Codex resolves ``deny > write > read`` by
    specificity first, verified live), and unlike Claude's file this profile is re-proven under
    ``codex sandbox`` before every provider attempt that gets a shell — which on Codex is all of
    them, an evaluator and the supervisor's own read-only turn included — so the survival is
    demonstrated rather than assumed. Two
    honest limits: an inherited ``extends`` grant can still beat an explicit deny (a clone under the
    global ``/private/tmp`` removes the carve-outs entirely), and the base ``:minimal`` read set may
    keep some system paths read-only inside the root grant — its exact composition is
    platform-dependent and nothing here depends on it.
    """
    if permission_profile not in _EXTENDS:
        raise ProviderError(
            ErrorClass.CONFIGURATION_ERROR,
            f"unsupported Codex permission_profile {permission_profile!r}; "
            f"expected one of {sorted(_EXTENDS)}",
        )
    workspace_root = Path(working_directory)
    filesystem: dict[str, Any] = {":minimal": "read"}
    if not strict_isolation and workspace_root.anchor:
        # Advanced mode: write anywhere on this volume, expressed as the workspace path's anchor so
        # one rule reads ``/`` on POSIX and a drive root on native Windows. Written before the rules
        # below because that is the order they are read in — broad grant first, carve-outs after —
        # not because Codex resolves them in written order (it resolves by specificity).
        filesystem[to_native(Path(workspace_root.anchor))] = "write"
    filesystem[to_native(workspace_root)] = (
        "write" if permission_profile == "workspace-write" else "read"
    )
    # Readable-but-not-writable roots (workspace-write attempts only; read-only denies all writes).
    if write_guard is not None:
        for path in write_guard.denied_write_paths:
            filesystem[to_native(path)] = "read"
    # Deny last: private/control/secret roots always win over any read/write grant above.
    needs_glob_scan = False
    if deny_policy is not None:
        # ``deny`` at EVERY read-isolation setting. This set used to be downgraded to ``read``
        # when read-isolation was off — that is, on the shipped default — which made the private
        # home and the resolved env-file readable to the sandboxed shell. It holds ONLY the
        # orchestrator's own private set: the provider config home is deliberately absent at every
        # setting (owner decision 2026-08-24). A deny on ``$CODEX_HOME`` used to live here, argued
        # safe because the CLI loads its config and auth outside this profile — which missed that
        # the standalone package keeps the ``codex`` BINARY inside that home, and ``apply_patch``
        # re-execs it under the sandbox as its fs helper, so the deny broke every patch. The public
        # ``denied_read_paths`` blacklist below is ``deny`` regardless.
        for path in deny_policy.denied_paths:
            filesystem[to_native(path)] = "deny"
    for pattern in denied_read_paths:
        if not pattern.strip():
            continue
        key, globs = _resolve_denied_read(
            workspace_root, pattern, strict_isolation=strict_isolation, to_native=to_native
        )
        filesystem[key] = "deny"
        needs_glob_scan = needs_glob_scan or globs
    if needs_glob_scan:
        filesystem["glob_scan_max_depth"] = _GLOB_SCAN_MAX_DEPTH
    return {
        "extends": _EXTENDS[permission_profile],
        "filesystem": filesystem,
        "network": {"enabled": network_access},
    }


def render_permission_profile_arg(profile: Mapping[str, Any], *, name: str = PROFILE_NAME) -> str:
    """Render *profile* as the single ``permissions.<name>={...}`` inline-table ``-c`` value."""
    return f"permissions.{name}={_render_inline(profile)}"
