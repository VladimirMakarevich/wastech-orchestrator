"""Flow agent/evaluator prompt assembly.

A node's prompt template is the contents of its ``role_file``; the security-critical renderer (the
fixed core) is unchanged:

    prompt = render_prompt(read(<flow_dir>/role_file), <allowlisted path variables>)

:func:`render_prompt` and :data:`ALLOWED_PROMPT_VARS` stay in ``core.prompts`` — the renderer only
substitutes allowlisted ``{name}`` *path* tokens (never task bodies, diffs, logs, env, or secrets;
those stay in the artifact files the agent reads by path). ``role_file`` is resolved **inside the
flow directory** (defense-in-depth on top of the load-time traversal check in
``core.flow.validator``): a path escaping ``flow_dir`` is a fatal error.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.prompts import ALLOWED_PROMPT_VARS, render_prompt


class RoleFileError(Exception):
    """Raised when a node ``role_file`` cannot be read or escapes the flow directory."""


def read_role_file(flow_dir: Path, role_file: str) -> str:
    """Read ``<flow_dir>/<role_file>``, enforcing that it stays within ``flow_dir``.

    :raises RoleFileError: if the resolved path escapes ``flow_dir`` or cannot be read.
    """
    base = flow_dir.resolve()
    target = (base / role_file).resolve()
    if base != target and base not in target.parents:
        raise RoleFileError(f"role_file {role_file!r} escapes flow directory {flow_dir}")
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoleFileError(f"cannot read role_file {target}: {exc}") from exc


def render_role_prompt(
    flow_dir: Path,
    role_file: str,
    variables: dict[str, object | None],
    *,
    allowed: frozenset[str] = ALLOWED_PROMPT_VARS,
) -> str:
    """Build a node's prompt: read its ``role_file`` and substitute allowlisted path variables.

    *allowed* is the effective substitutable-name set. It defaults to :data:`ALLOWED_PROMPT_VARS`;
    the agent runner passes the flow-derived set (core allowlist ∪ each agent node's
    ``{<id>_path}``) so a node can reference an upstream node's output by id. The renderer stays the
    fixed security core — every value in *variables* is still a Core-written artifact path.
    """
    return render_prompt(read_role_file(flow_dir, role_file), variables, allowed=allowed)
