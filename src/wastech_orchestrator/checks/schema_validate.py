"""Strict structural validation of agent discovery output (automatic check discovery §6, §12).

The Core does not trust an agent's structured output: it is fail-closed validated here against the
discovery schema (types, required keys, no unknown keys, bounds, confidence enum) and given a cheap
argv safety pre-filter (no shell metacharacters, no absolute paths). The authoritative security
validation still happens in the deterministic ``CheckCandidateValidator``; this is a first gate so a
malformed or unsafe payload never reaches probing. Dependency-light by design (no ``jsonschema``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from wastech_orchestrator.checks.model import shell_metachars

_MAX_NAME = 64
_MAX_TOKEN = 256
_MAX_ARGV = 16
_MAX_ITEMS = 12
_CONFIDENCE = {"low", "medium", "high"}


@dataclass(frozen=True)
class Proposal:
    """One agent-proposed command (a check or a setup step)."""

    name: str
    argv: tuple[str, ...]
    evidence: tuple[str, ...]
    confidence: str


@dataclass(frozen=True)
class DiscoveryDoc:
    """A schema-valid agent discovery response."""

    checks: tuple[Proposal, ...]


def validate_discovery_output(structured: Any) -> DiscoveryDoc | None:
    """Validate raw structured output into a :class:`DiscoveryDoc`, or ``None`` on any violation."""
    if not isinstance(structured, dict) or set(structured) - {"checks"}:
        return None
    checks = _parse_list(structured.get("checks"))
    if checks is None or not checks:
        return None  # at least one check is required
    return DiscoveryDoc(checks=tuple(checks))


def _parse_list(value: Any) -> list[Proposal] | None:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        return None
    out: list[Proposal] = []
    for item in value:
        proposal = _parse_item(item)
        if proposal is None:
            return None
        out.append(proposal)
    return out


def _parse_item(item: Any) -> Proposal | None:
    if not isinstance(item, dict) or set(item) - {"name", "argv", "evidence", "confidence"}:
        return None
    name = item.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= _MAX_NAME:
        return None
    argv = item.get("argv")
    if not isinstance(argv, list) or not 1 <= len(argv) <= _MAX_ARGV:
        return None
    if not all(isinstance(t, str) and 1 <= len(t) <= _MAX_TOKEN for t in argv):
        return None
    if shell_metachars(argv) is not None or _is_absolute(argv[0]):
        return None  # cheap safety gate; the deterministic validator is authoritative
    evidence = item.get("evidence", [])
    if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
        return None
    if item.get("confidence") not in _CONFIDENCE:
        return None
    return Proposal(
        name=name,
        argv=tuple(argv),
        evidence=tuple(evidence),
        confidence=item["confidence"],
    )


def _is_absolute(token: str) -> bool:
    """An absolute path is rejected — a portable relative path or bare command is required (§7)."""
    return PurePosixPath(token).is_absolute() or PureWindowsPath(token).is_absolute()
