"""Portable artifact path-identity validators.

Reject-not-sanitize leaf helpers shared by every layer that turns a dynamic identifier — a task id
or a flow node id — or a relative name into an artifact path component. Kept **host-independent**: a
name rejected for native-Windows portability (a reserved device name, a trailing dot) is rejected on
macOS and Linux too, so the same tracked task/flow behaves identically on every supported OS instead
of loading on one host and failing to write its artifacts on another.

This is a **leaf** module: it imports only the standard library, so ``task``, ``core``, and
``providers`` may all depend on it without an import cycle (import-linter keeps ``providers`` and
``core`` from importing each other, and ``providers`` from importing ``task``). Path *containment*
(a built path proven to resolve under its root) is a separate, filesystem-touching concern and lives
at the artifact write boundaries in :mod:`~wastech_orchestrator.providers.artifacts`; identity
validation here is the cheap, pure first line that runs before any state row, branch, directory, or
provider run exists.
"""

from __future__ import annotations

import re

# Windows reserves these device names case-insensitively, and reserves them even with a trailing
# extension: ``con``, ``con.txt``, and ``com1.log`` all resolve to the device, never to a file. The
# identifier vocabularies validated here are ASCII, so the superscript ``COM²``/``COM³`` forms that
# some Windows builds also reserve cannot occur.
_WINDOWS_RESERVED_STEMS: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{d}" for d in "123456789"}
    | {f"LPT{d}" for d in "123456789"}
)

# A task id: a lowercase-alphanumeric first char, then up to 63 of ``[a-z0-9._-]`` (1..64 total).
# The dot is allowed mid-value, but a *trailing* dot and a device name are rejected separately by
# :func:`is_valid_task_id` because the id becomes a directory/file component and a branch fragment.
TASK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# A flow node id: one bounded lowercase token that is simultaneously (a) a single portable path
# component and (b) the ``{<node-id>_path}`` prompt token — so it must match the renderer's
# ``[a-z0-9_-]+`` shape (:data:`~wastech_orchestrator.core.prompts._VAR_RE`). No ``.`` (a dot cannot
# appear in the prompt token), no separators, no leading ``_``/``-``; 1..64 chars.
NODE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Characters that can never appear in a portable path component: the POSIX and Windows separators,
# the drive/stream ``:``, and the characters Windows forbids in a filename. Control chars are caught
# by ordinal in :func:`is_portable_path_segment`.
_FORBIDDEN_SEGMENT_CHARS: frozenset[str] = frozenset('/\\:<>"|?*')


def is_windows_reserved_name(name: str) -> bool:
    """Return True iff ``name``'s stem is a reserved Windows device name (case-insensitive).

    The *stem* is the portion before the first dot, because Windows resolves ``con.txt`` to the CON
    device exactly as it does bare ``con``. Applied on every OS so a device-named identifier is
    rejected host-independently — not only when a run happens to execute on Windows.
    """
    stem = name.split(".", 1)[0]
    return stem.upper() in _WINDOWS_RESERVED_STEMS


def is_valid_task_id(task_id: str) -> bool:
    """Return True iff ``task_id`` is a portable task identity.

    The lowercase ``[a-z0-9._-]`` vocabulary and 1..64 length are the long-standing shape; on top of
    them the id must not end in a dot and must not be a Windows device name, because it becomes a
    directory/file component and a branch fragment. Reject, never sanitize — a value that would only
    be usable after rewriting is refused.
    """
    if TASK_ID_PATTERN.fullmatch(task_id) is None:
        return False
    if task_id.endswith("."):
        return False
    return not is_windows_reserved_name(task_id)


def is_valid_node_id(node_id: str) -> bool:
    """Return True iff ``node_id`` is a portable single-segment lowercase token, not a device name.

    The grammar keeps the id usable both as one artifact path component and as the renderer's
    ``{<node-id>_path}`` token; the device-name check keeps it writable on Windows. Reject, never
    sanitize — an incompatible custom id gets a precise load error, not a silently rewritten name.
    """
    return NODE_ID_PATTERN.fullmatch(node_id) is not None and not is_windows_reserved_name(node_id)


def is_portable_path_segment(segment: str) -> bool:
    """Return True iff ``segment`` is safe as one component of an artifact path on every OS.

    More permissive than :func:`is_valid_node_id` — it accepts the fixed names the orchestrator
    joins onto its layout (``plan.md``, ``run-000001``, ``1-claude``), which carry dots and mixed
    case — but still reject-not-sanitize: no empty / ``.`` / ``..`` component, no path, drive, or
    stream separator, none of the Windows-forbidden characters, no control character, and no
    trailing dot or space (Windows strips those, yielding a different on-disk name), and no reserved
    device name. Case is *not* constrained here; case-insensitive sibling collisions are a
    directory-level concern handled where a whole directory is inspected.
    """
    if not segment or segment in (".", ".."):
        return False
    if segment[-1] in (".", " "):
        return False
    if any(ch in _FORBIDDEN_SEGMENT_CHARS or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in segment):
        return False
    return not is_windows_reserved_name(segment)
