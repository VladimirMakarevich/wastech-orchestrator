"""Codex-owned projection of the orchestrator deny policy.

Codex exposes two different enforcement primitives: permission profiles for filesystem access and
execpolicy rules for command prefixes.  This module is intentionally provider-local because every
string it renders is Codex CLI/config syntax.  It never reads denied file contents; those remain a
separate redaction input in :mod:`wastech_orchestrator.providers.redaction`.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path
from typing import Final

from wastech_orchestrator.security.forbidden_args import FORBIDDEN_SANDBOX_VALUE

POLICY_PROFILE_NAME: Final = "worc-deny-policy"
POLICY_RULES_FILENAME: Final = "worc-deny-policy.rules"
_MANAGED_HOME_DIRNAME: Final = "worc-managed"
_AUTH_FILENAME: Final = "auth.json"
_GLOB_SCAN_MAX_DEPTH: Final = 256


class CodexPolicyError(Exception):
    """The controlled Codex deny boundary could not be constructed safely."""


def command_prefixes(denied_commands: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Normalize provider-neutral denied command strings into literal argv prefixes.

    The public configuration semantics are whitespace-delimited command prefixes (the same
    semantics used by check-command validation).  JSON/Starlark quoting happens only when the
    provider-owned rules file is rendered, so operators never need provider-specific syntax.
    """
    seen: set[tuple[str, ...]] = set()
    prefixes: list[tuple[str, ...]] = []
    for raw in denied_commands:
        prefix = tuple(raw.split())
        if prefix and prefix not in seen:
            seen.add(prefix)
            prefixes.append(prefix)
    return tuple(prefixes)


def render_exec_policy(denied_commands: tuple[str, ...]) -> str:
    """Render deterministic forbidden ``prefix_rule`` entries without secret material."""
    blocks = []
    for prefix in command_prefixes(denied_commands):
        pattern = json.dumps(list(prefix), ensure_ascii=False)
        blocks.append(
            "prefix_rule(\n"
            f"    pattern = {pattern},\n"
            '    decision = "forbidden",\n'
            '    justification = "This operation is reserved for the orchestrator.",\n'
            ")\n"
        )
    return "\n".join(blocks)


def resolve_user_codex_home(environment: dict[str, str] | None = None) -> Path:
    """Resolve the existing user Codex home without reading or recording its contents."""
    source = os.environ if environment is None else environment
    explicit = source.get("CODEX_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    home = source.get("USERPROFILE") if platform.system() == "Windows" else source.get("HOME")
    if not home:
        home = source.get("HOME") or source.get("USERPROFILE")
    if not home:
        raise CodexPolicyError("cannot resolve the existing Codex auth home")
    return (Path(home).expanduser() / ".codex").resolve()


def controlled_codex_home(user_home: Path, instance_root: Path) -> Path:
    """Return a stable isolated home for one orchestrator installation.

    The digest prevents two repositories from replacing each other's generated rules while keeping
    the directory under the real Codex home.  Keeping both homes on one filesystem makes the
    credential hard link portable across Windows, macOS, Linux, and WSL.
    """
    identity = os.fspath(instance_root.resolve()).encode("utf-8", errors="surrogatepass")
    suffix = hashlib.sha256(identity).hexdigest()[:16]
    return user_home / _MANAGED_HOME_DIRNAME / suffix


def prepare_controlled_home(
    user_home: Path,
    instance_root: Path,
    denied_commands: tuple[str, ...],
) -> tuple[Path, Path]:
    """Create the isolated policy home and project the existing file auth store into it.

    ``auth.json`` is hard-linked, never copied.  A hard-link failure is fatal because silently
    switching to an unauthenticated home would turn a security feature into an infrastructure
    fallback.  When no source file exists, any stale projection is removed and non-file credential
    handling remains owned by the Codex CLI.  Returns ``(controlled_home, generated_rules_path)``.
    """
    home = controlled_codex_home(user_home, instance_root)
    rules_dir = home / "rules"
    try:
        rules_dir.mkdir(parents=True, exist_ok=True)
        _make_private(home)
        _project_auth(user_home / _AUTH_FILENAME, home / _AUTH_FILENAME)
        rules_path = rules_dir / POLICY_RULES_FILENAME
        _atomic_write(rules_path, render_exec_policy(denied_commands))
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise CodexPolicyError(
            f"cannot construct the controlled Codex policy home: {reason}"
        ) from exc
    return home, rules_path


def permission_config_values(
    *,
    sandbox: str,
    network_access: bool,
    denied_read_paths: tuple[str, ...],
    strict_isolation: bool = True,
) -> tuple[str, ...]:
    """Render the highest-precedence permission profile for one fresh or resumed attempt."""
    if sandbox == FORBIDDEN_SANDBOX_VALUE:
        if strict_isolation:
            raise CodexPolicyError(
                "Codex danger-full-access requires security.strict_isolation=false"
            )
        if not network_access:
            raise CodexPolicyError(
                "offline Codex danger-full-access cannot enforce network isolation"
            )
        return ('default_permissions=":danger-full-access"',)

    parent = ":read-only" if sandbox == "read-only" else ":workspace"
    values = [
        f'default_permissions="{POLICY_PROFILE_NAME}"',
        f'permissions.{POLICY_PROFILE_NAME}.extends="{parent}"',
        f'permissions.{POLICY_PROFILE_NAME}.filesystem.":root"="deny"',
        f'permissions.{POLICY_PROFILE_NAME}.filesystem.":minimal"="read"',
        (
            f"permissions.{POLICY_PROFILE_NAME}.filesystem.glob_scan_max_depth="
            f"{_GLOB_SCAN_MAX_DEPTH}"
        ),
        f"permissions.{POLICY_PROFILE_NAME}.network.enabled={str(network_access).lower()}",
    ]
    for raw in denied_read_paths:
        pattern = _normalize_denied_path(raw)
        key = json.dumps(pattern, ensure_ascii=False)
        values.append(
            f'permissions.{POLICY_PROFILE_NAME}.filesystem.":workspace_roots".{key}="deny"'
        )
    return tuple(values)


def _normalize_denied_path(raw: str) -> str:
    """Use portable slash-relative patterns and collapse ``dir/**`` to a subtree deny."""
    pattern = raw.strip().replace("\\", "/")
    if pattern.endswith("/**") and not any(char in pattern[:-3] for char in "*?["):
        return pattern[:-3].rstrip("/")
    return pattern


def _project_auth(source: Path, target: Path) -> None:
    if not source.is_file():
        if target.exists():
            target.unlink()
        return
    if target.exists():
        try:
            if source.samefile(target):
                return
        except OSError:
            pass
        target.unlink()
    os.link(source, target)


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    _make_private(temporary, file=True)
    temporary.replace(path)


def _make_private(path: Path, *, file: bool = False) -> None:
    if os.name != "nt":
        path.chmod(0o600 if file else 0o700)
