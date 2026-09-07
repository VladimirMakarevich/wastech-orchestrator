"""Canonical, provider-agnostic check model.

The single internal representation of a quality-gate check is :class:`ResolvedCheck` — a logical
name, an **argv list** (never a shell string), and an optional repo-relative ``cwd``. Operator
``checks.command_sets`` normalize into :class:`ResolvedCheckSet`s here (each carrying its selection
``paths`` and the runtime knobs), and the Check Runner consumes that shape.

This module holds *shapes and normalization only*: no provider/CLI syntax, no process launching,
no filesystem I/O.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wastech_orchestrator.config.schema import CommandSet

# Characters that only mean something to a shell. We never launch through a shell, so any of these
# in an argv token is a configuration mistake (it would be passed literally and fail) —
# reject it as defense in depth against shell-injection-shaped commands.
_SHELL_METACHARS: frozenset[str] = frozenset(";|&$`><(){}*?\n\r")


class CheckCommandError(ValueError):
    """A configured check command is malformed (empty argv or an unsupported shape)."""


@dataclass(frozen=True)
class ResolvedCheck:
    """A logical check resolved to an executable argv list (no shell interpolation).

    ``cwd`` is repo-relative (``""`` => the clone root); the runner launches the command in
    ``clone_dir / cwd``.
    """

    name: str
    argv: tuple[str, ...]
    cwd: str = ""


@dataclass(frozen=True)
class ResolvedCheckSet:
    """A named, normalized command set: its selection ``paths`` plus the checks and runtime knobs.

    ``paths`` are repo-relative globs used by :func:`checks.selection.select_check_sets` to decide
    whether the set runs for a given diff (empty => always, on any non-empty diff).
    ``timeout_seconds`` (``None`` => the global ``checks.timeout_seconds``) is the per-command
    timeout for this set; ``skip_if_unavailable`` lets the set be skipped — loudly, never passed —
    when a command's binary is absent on the host.
    """

    name: str
    paths: tuple[str, ...]
    checks: tuple[ResolvedCheck, ...]
    timeout_seconds: int | None = None
    skip_if_unavailable: bool = False


def _name_from_argv(argv: tuple[str, ...]) -> str:
    """Derive a logical name from ``argv[0]`` — the executable's basename (both path flavors)."""
    head = argv[0]
    # Strip POSIX then Windows separators so a host of either flavor names the same thing.
    base = PureWindowsPath(PurePosixPath(head).name).name
    return base or head


def _structured_fields(item: Any) -> tuple[str | None, tuple[str, ...], str]:
    """Pull ``(name, argv, cwd)`` from a mapping or a ``CheckCommandSpec``-like object."""
    if isinstance(item, Mapping):
        raw_name = item.get("name")
        raw_argv = item.get("argv")
        raw_cwd = item.get("cwd")
    else:
        raw_name = getattr(item, "name", None)
        raw_argv = getattr(item, "argv", None)
        raw_cwd = getattr(item, "cwd", None)
    if not isinstance(raw_argv, list | tuple) or not raw_argv:
        raise CheckCommandError(f"check command requires a non-empty 'argv' list: {item!r}")
    argv = tuple(str(token) for token in raw_argv)
    name = str(raw_name) if raw_name else None
    cwd = str(raw_cwd) if raw_cwd else ""
    return name, argv, cwd


def normalize_check_command(item: Any) -> ResolvedCheck:
    """Normalize one configured command into a :class:`ResolvedCheck`.

    Accepts a structured mapping ``{name, argv: [...], cwd?}``, a ``CheckCommandSpec``-like object,
    or an existing :class:`ResolvedCheck`. (A legacy shell string is still split with ``shlex`` so
    config validation can reuse this on any argv-bearing shape.) Raises :class:`CheckCommandError`
    on an empty/blank command or a malformed mapping.
    """
    if isinstance(item, ResolvedCheck):
        return item
    if isinstance(item, str):
        argv = tuple(shlex.split(item, posix=True))
        if not argv:
            raise CheckCommandError(f"empty check command: {item!r}")
        return ResolvedCheck(name=_name_from_argv(argv), argv=argv)
    name, argv, cwd = _structured_fields(item)
    return ResolvedCheck(name=name or _name_from_argv(argv), argv=argv, cwd=cwd)


def normalize_command_sets(command_sets: Mapping[str, CommandSet]) -> tuple[ResolvedCheckSet, ...]:
    """Normalize ``checks.command_sets`` into :class:`ResolvedCheckSet`s (pure, shapes-only).

    Each set's name is the mapping key; each command normalizes via :func:`normalize_check_command`
    (carrying its ``cwd``), and the set's ``paths`` / ``timeout_seconds`` / ``skip_if_unavailable``
    are carried through verbatim.
    """
    out: list[ResolvedCheckSet] = []
    for name, cset in command_sets.items():
        out.append(
            ResolvedCheckSet(
                name=name,
                paths=tuple(cset.paths),
                checks=tuple(normalize_check_command(spec) for spec in cset.commands),
                timeout_seconds=cset.timeout_seconds,
                skip_if_unavailable=cset.skip_if_unavailable,
            )
        )
    return tuple(out)


def is_safe_relpath(value: str) -> bool:
    """True iff ``value`` is a repo-relative path — not absolute, no ``~``, no ``..`` traversal, not
    Windows-absolute. Used to validate a check command's ``cwd`` at config-load time, and again
    before the runner joins it."""
    norm = value.replace("\\", "/").strip()
    if not norm:
        return False
    if norm.startswith(("/", "~")) or ".." in norm.split("/"):
        return False
    return not PureWindowsPath(value).is_absolute()


def shell_metachars(argv: Sequence[str]) -> str | None:
    """Return the first argv token with a shell metacharacter, or ``None`` when all are safe."""
    for token in argv:
        if any(ch in _SHELL_METACHARS for ch in token):
            return token
    return None


def argv_matches_denied(argv: Sequence[str], denied: Iterable[str]) -> str | None:
    """Return the denied-command prefix this argv matches, or ``None``.

    Mirrors the provider adapters' rule (whitespace-normalized prefix match) so a check can never be
    a forbidden command such as ``git commit`` / ``git push``.
    """
    joined = " ".join(argv)
    for entry in denied:
        prefix = " ".join(entry.split())
        if prefix and (joined == prefix or joined.startswith(prefix + " ")):
            return prefix
    return None
