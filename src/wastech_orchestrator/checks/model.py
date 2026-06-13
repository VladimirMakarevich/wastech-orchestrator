"""Canonical, provider-agnostic check model (backlog: automatic check discovery).

The single internal representation of a quality-gate check is :class:`ResolvedCheck` — a logical
name plus an **argv list** (never a shell string). Both the deterministic resolver and the Check
Runner consume this shape, so the legacy-string → argv rule lives here (not in the loader, which
stays shapes-only).

This module holds *shapes and normalization only*: no provider/CLI syntax, no process launching,
no filesystem I/O.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

# Characters that only mean something to a shell. We never launch through a shell, so any of these
# in an argv token is a configuration/discovery mistake (it would be passed literally and fail) —
# reject it as defense in depth against shell-injection-shaped commands (§7, §12).
_SHELL_METACHARS: frozenset[str] = frozenset(";|&$`><(){}*?\n\r")


class CheckSource(StrEnum):
    """Where a resolved profile's checks came from (recorded in the profile artifact)."""

    CONFIGURED = "configured"  # explicit operator override in config.yaml
    DETECTED = "detected"  # deterministic ecosystem detection
    AGENT = "agent"  # agent-assisted read-only discovery
    DISABLED = "disabled"  # discovery mode `disabled` — an explicit no-check profile


class Confidence(StrEnum):
    """How strongly the evidence supports a candidate (drives ordering and agent fallback)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ProbeStatus(StrEnum):
    """The result of a lightweight launchability probe (the full suite is never run for it)."""

    LAUNCHABLE = "launchable"
    NOT_LAUNCHABLE = "not_launchable"
    UNSUPPORTED = "unsupported"  # the probe cannot decide for this candidate kind


class CheckCommandError(ValueError):
    """A configured/discovered check command is malformed (empty argv or an unsupported shape)."""


@dataclass(frozen=True)
class ResolvedCheck:
    """A logical check resolved to an executable argv list (no shell interpolation)."""

    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class CheckCandidate:
    """A proposed check (deterministic or agent-supplied), before validation and probing."""

    name: str
    argv: tuple[str, ...]
    source: CheckSource
    evidence: tuple[str, ...] = ()
    confidence: Confidence = Confidence.MEDIUM
    probe_status: ProbeStatus | None = None


def _name_from_argv(argv: tuple[str, ...]) -> str:
    """Derive a logical name from ``argv[0]`` — the executable's basename (both path flavors)."""
    head = argv[0]
    # Strip POSIX then Windows separators so a host of either flavor names the same thing.
    base = PureWindowsPath(PurePosixPath(head).name).name
    return base or head


def _structured_fields(item: Any) -> tuple[str | None, tuple[str, ...]]:
    """Pull ``(name, argv)`` from a mapping or a ``name``/``argv`` object (``CheckCommandSpec``)."""
    if isinstance(item, Mapping):
        raw_name = item.get("name")
        raw_argv = item.get("argv")
    else:
        raw_name = getattr(item, "name", None)
        raw_argv = getattr(item, "argv", None)
    if not isinstance(raw_argv, list | tuple) or not raw_argv:
        raise CheckCommandError(f"check command requires a non-empty 'argv' list: {item!r}")
    argv = tuple(str(token) for token in raw_argv)
    name = str(raw_name) if raw_name else None
    return name, argv


def normalize_check_command(item: Any) -> ResolvedCheck:
    """Normalize one configured/discovered command into a :class:`ResolvedCheck`.

    Accepts a legacy shell-style string (split with ``shlex``; name from ``argv[0]``), a structured
    mapping ``{name, argv: [...]}``, a ``CheckCommandSpec``-like object, or an existing
    :class:`ResolvedCheck`. Raises :class:`CheckCommandError` on an empty/blank command or a
    malformed mapping.
    """
    if isinstance(item, ResolvedCheck):
        return item
    if isinstance(item, str):
        argv = tuple(shlex.split(item, posix=True))
        if not argv:
            raise CheckCommandError(f"empty check command: {item!r}")
        return ResolvedCheck(name=_name_from_argv(argv), argv=argv)
    name, argv = _structured_fields(item)
    return ResolvedCheck(name=name or _name_from_argv(argv), argv=argv)


def normalize_commands(items: Iterable[Any]) -> list[ResolvedCheck]:
    """Normalize a sequence of configured commands, skipping blank legacy strings (a no-op)."""
    out: list[ResolvedCheck] = []
    for item in items:
        if isinstance(item, str) and not item.strip():
            continue
        out.append(normalize_check_command(item))
    return out


def shell_metachars(argv: Sequence[str]) -> str | None:
    """Return the first argv token with a shell metacharacter, or ``None`` when all are safe."""
    for token in argv:
        if any(ch in _SHELL_METACHARS for ch in token):
            return token
    return None


def argv_matches_denied(argv: Sequence[str], denied: Iterable[str]) -> str | None:
    """Return the denied-command prefix this argv matches, or ``None``.

    Mirrors the provider adapters' rule (whitespace-normalized prefix match) so a check can never be
    a forbidden command such as ``git commit`` / ``git push`` (§12).
    """
    joined = " ".join(argv)
    for entry in denied:
        prefix = " ".join(entry.split())
        if prefix and (joined == prefix or joined.startswith(prefix + " ")):
            return prefix
    return None
