"""Shared primitives for the per-task frozen bundles (the control plane and the agent inputs).

Two independent freezers snapshot different-but-adjacent inputs at task start:

* :mod:`~wastech_orchestrator.core.flow.control_bundle` freezes the *control plane* — the
  flow YAML, role/supervisor prompts, and tool executables — because a later orchestrator node
  reads/executes those bytes with the orchestrator's own authority, so a mid-run mutation is an
  execution-boundary violation routed to ``manual_action_required``.
* :mod:`~wastech_orchestrator.core.flow.instruction_bundle` freezes the *agent inputs* —
  the task packet and the root repository instruction files — so a later
  agent/evaluator/supervisor/resume/fallback call cannot receive instructions the running task was
  never validated against.

Both need the identical fail-closed file-identity gate, the stable content digest, and the
case-fold/NFC collision guard. Those live here (one implementation, one behaviour) rather than being
duplicated or cross-imported through a private name. This module owns no policy of its own — it is a
leaf that only depends on the ``providers`` interface (``exchange``/``artifacts``), keeping the
import-linter contract green (``core.flow`` may import those leaves; they never import ``core``).

The shared error type is :class:`FrozenBundleError`; each freezer subclasses it
(``ControlBundleError`` / ``InstructionBundleError``) so a caller can still catch the specific kind,
while a caller that handles both catches the base. Helpers raise the caller-supplied ``error_cls``
(default the base) so a ``pytest.raises``/``except`` on the specific type still matches.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from pathlib import Path

from wastech_orchestrator.providers.exchange import FileInspector


class FrozenBundleError(Exception):
    """A frozen bundle (control plane or agent inputs) could not be built/verified (fail-closed)."""


def inspect_frozen_source(
    path: Path,
    inspector: FileInspector,
    *,
    label: str = "input",
    error_cls: type[FrozenBundleError] = FrozenBundleError,
) -> None:
    """Fail closed unless ``path`` is an existing regular, single-link, ADS-free, non-symlink file.

    A no-follow inspection (the exchange's seam): the source must not be a symlink/reparse point,
    must
    be a regular file (not a fifo/socket/device), must have exactly one hard link (no alias back to
    another live location), and must carry no NTFS alternate data stream. ``label`` names the input
    class in the error message (e.g. ``"control input"``, ``"repository instruction"``) so the
    message reads
    naturally for whichever freezer called it; ``error_cls`` selects the caller's subclass so a
    ``pytest.raises``/``except`` on the specific type still matches.
    """
    if not os.path.lexists(path):
        raise error_cls(f"{label} does not exist: {path.as_posix()}")
    facts = inspector(path)
    if facts.is_symlink:
        raise error_cls(f"{label} is a symlink/reparse point: {path.as_posix()}")
    if not facts.is_regular:
        raise error_cls(f"{label} is not a regular file: {path.as_posix()}")
    if facts.link_count != 1:
        raise error_cls(f"{label} is hard-linked ({facts.link_count}): {path.as_posix()}")
    if facts.alt_streams:
        raise error_cls(
            f"{label} has NTFS alternate data streams {facts.alt_streams!r}: {path.as_posix()}"
        )


def reject_key_collisions(
    keys: list[str],
    *,
    label: str = "input",
    error_cls: type[FrozenBundleError] = FrozenBundleError,
) -> None:
    """Fail closed if two bundle-relative keys collide under case-fold/NFC normalization.

    On a case-insensitive filesystem two keys differing only in case/NFC form would clobber each
    other during the copy, so a collision is rejected before any byte is written. ``label`` names
    the input class; ``error_cls`` selects the caller's subclass (see the inspector helper above).
    """
    seen: dict[str, str] = {}
    for key in keys:
        norm = unicodedata.normalize("NFC", key).casefold()
        if norm in seen:
            raise error_cls(f"{label} name collision: {key!r} vs {seen[norm]!r}")
        seen[norm] = key


def digest_entries(entries: list[tuple[str, str]]) -> str:
    """A stable SHA-256 over the sorted ``(bundle-key, file-sha256)`` pairs — a bundle identity.

    Deterministic regardless of insertion order (sorted before hashing), NUL-separated so a key and
    a digest can never run together ambiguously.
    """
    payload = "\n".join(f"{key}\x00{digest}" for key, digest in sorted(entries))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
