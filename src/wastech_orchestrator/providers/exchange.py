"""Exchange publication boundary (WRI-001, .agents/rules/security.md).

The exchange root ``<repo>/.worc-io/<task-id>/`` is the **only** provider-readable orchestration
surface. Everything that crosses into it does so through :func:`publish_to_exchange`, a single
redaction + path-safety seam:

* content is scrubbed through :func:`~wastech_orchestrator.providers.redaction.redact_text` before
  it lands on disk (the exchange is the sanctioned readable surface — "the agent could already read
  it" is never an argument for skipping redaction);
* the destination is proven to be a real, contained, single-link regular file — no symlink /
  junction / reparse-point escape on any path component, no ``..``/absolute/drive/``:`` relative
  name, no NTFS alternate data stream, no case-fold/NFC sibling collision;
* the write is atomic (temp + :func:`os.replace`) and LF byte-stable.

:func:`build_exchange_manifest` re-derives the clean-surface fingerprint (file type, link
identity/count, relative name, size, content digest) and is the seam WRI-002/WRI-007 diff pre/post
attempt and on seal. Per-OS filesystem inspection is behind the injectable :data:`FileInspector`
seam so both the POSIX and native-Windows fail-closed branches are unit-testable on any host.

This module is a leaf (``.importlinter`` ``providers-are-leaf``): it must not import ``core`` /
``memory``, so it carries its own small atomic writer rather than reusing ``memory._io`` /
``core.flow.output_policy``. Path safety is shared rather than duplicated: the per-segment
portability grammar comes from :mod:`~wastech_orchestrator.security.identifiers` and the containment
belt from :mod:`~wastech_orchestrator.providers.artifacts` (both leaf deps), so the exchange and the
private roots reject the same non-portable/traversing identity identically.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from wastech_orchestrator.providers.artifacts import (
    PathIdentityError,
    assert_contained_path,
    exchange_task_dir,
    sha256_file,
)
from wastech_orchestrator.providers.base import AgentRunRequest
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.security.identifiers import is_portable_path_segment


class ExchangeError(Exception):
    """A fail-closed exchange path-safety / publication violation (never a fallback error class)."""


# --- Platform inspection seam --------------------------------------------------------------------


@dataclass(frozen=True)
class FileFacts:
    """No-follow facts about one path, abstracted across POSIX and native Windows."""

    is_symlink: bool  # POSIX symlink OR Windows reparse point / junction (no-follow)
    is_dir: bool  # a real directory (a reparse-point "dir" is reported as a symlink, not a dir)
    is_regular: bool  # stat.S_ISREG — not a fifo/socket/char/block device or reparse point
    link_count: int  # POSIX st_nlink / Windows nNumberOfLinks
    alt_streams: tuple[str, ...]  # named NTFS streams beyond the default; always () on POSIX
    size: int


#: lstat-equivalent of a single path that never follows a symlink/reparse point.
FileInspector = Callable[[Path], FileFacts]


def posix_file_facts(path: Path) -> FileFacts:
    """:data:`FileInspector` for POSIX hosts (``Path.lstat`` + ``stat`` mode bits, no ADS)."""
    st = path.lstat()
    mode = st.st_mode
    return FileFacts(
        is_symlink=stat.S_ISLNK(mode),
        is_dir=stat.S_ISDIR(mode),
        is_regular=stat.S_ISREG(mode),
        link_count=st.st_nlink,
        alt_streams=(),
        size=st.st_size,
    )


def windows_file_facts(path: Path) -> FileFacts:
    """:data:`FileInspector` for native Windows — reparse points, hard-link count, and NTFS ADS.

    Exercised on a real Windows host by the WRI-006 cross-platform gate; on other hosts the fake
    inspector drives the same fail-closed branches. Fails closed (:class:`ExchangeError`) rather
    than guessing when a Win32 query cannot be completed on a regular file.
    """
    st = path.lstat()
    mode = st.st_mode
    attrs = int(getattr(st, "st_file_attributes", 0))
    is_reparse = bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    is_dir = stat.S_ISDIR(mode) and not is_reparse
    is_regular = stat.S_ISREG(mode) and not is_reparse
    link_count = _windows_link_count(path) if is_regular else 1
    alt_streams = _windows_alt_streams(path) if is_regular else ()
    return FileFacts(
        is_symlink=is_reparse,
        is_dir=is_dir,
        is_regular=is_regular,
        link_count=link_count,
        alt_streams=alt_streams,
        size=st.st_size,
    )


def default_file_inspector(system: str | None = None) -> FileInspector:
    """The :data:`FileInspector` for the host, resolved from ``platform.system()`` at call time."""
    name = system if system is not None else platform.system()
    return windows_file_facts if name == "Windows" else posix_file_facts


# --- Publication ---------------------------------------------------------------------------------


def publish_to_exchange(
    task_dir: str | Path,
    relpath: str,
    content: str | bytes,
    *,
    extra_secrets: Iterable[str] = (),
    inspect: FileInspector | None = None,
) -> str:
    """Redact ``content``, write it atomically under ``task_dir/relpath``, return the POSIX path.

    ``task_dir`` is the per-task exchange directory (``exchange_task_dir``); it is passed in rather
    than derived so this module stays decoupled from the (not-yet-typed) layout seam. ``relpath`` is
    a POSIX-relative name within it. ``content`` may be ``str`` or ``bytes`` — ``bytes`` are decoded
    as UTF-8 and redacted like text (no raw passthrough, which would bypass the redaction boundary).
    Fails closed (:class:`ExchangeError`) on an unsafe relative name, a symlink/reparse point on any
    path component, an irreplaceable existing target, or an escape from ``task_dir``.
    """
    inspector = inspect or default_file_inspector()
    base = Path(task_dir)
    _validate_relpath(relpath)
    target = base / relpath
    _assert_contained(base, target)
    text = content if isinstance(content, str) else content.decode("utf-8", errors="replace")
    redacted = redact_text(text, extra_secrets=tuple(extra_secrets))
    _safe_prepare_dirs(base, target.parent, inspector)
    _assert_target_replaceable(target, inspector)
    _atomic_write_text(target, redacted)
    return target.as_posix()


def _validate_relpath(relpath: str) -> None:
    """Reject an absolute / traversing / non-portable relative name.

    The per-segment portability grammar — ``..``/empty, path/drive/stream separators, Windows device
    names and forbidden characters, a trailing dot/space — is delegated to the shared
    :func:`~wastech_orchestrator.security.identifiers.is_portable_path_segment` validator, so the
    exchange and the private roots reject the same non-portable segment identically. Only the
    whole-string checks (empty/untrimmed, absolute) stay here.
    """
    if not relpath or relpath != relpath.strip():
        raise ExchangeError(f"empty or untrimmed exchange relpath: {relpath!r}")
    pure = PurePosixPath(relpath)
    if pure.is_absolute():
        raise ExchangeError(f"absolute exchange relpath: {relpath!r}")
    for part in pure.parts:
        if not is_portable_path_segment(part):
            raise ExchangeError(f"non-portable segment {part!r} in exchange relpath: {relpath!r}")


def _assert_contained(base: Path, target: Path) -> None:
    """Second-belt containment: ``target`` resolves under ``base`` (chain proven symlink-free).

    Delegates to the shared :func:`~wastech_orchestrator.providers.artifacts.assert_contained_path`
    (the one containment belt both roots use) and re-raises its :class:`PathIdentityError` as an
    :class:`ExchangeError` so the exchange keeps its single fail-closed error surface.
    """
    try:
        assert_contained_path(base, target)
    except PathIdentityError as exc:
        raise ExchangeError(str(exc)) from None


def _safe_prepare_dirs(base: Path, leaf_parent: Path, inspector: FileInspector) -> None:
    """Ensure ``base``..``leaf_parent`` are real dirs, creating missing levels (no-follow).

    ``base`` (the per-task exchange dir) and its parents are orchestrator-owned, so they are created
    with ``parents=True`` and then proven not to be a symlink. The ``relpath`` sub-levels below
    ``base`` are the provider-writable region, so each is created one level at a time and inspected
    no-follow to refuse a planted symlink/reparse point.
    """
    base.mkdir(parents=True, exist_ok=True)
    _assert_real_dir(base, inspector)
    current = base
    for part in leaf_parent.relative_to(base).parts:
        current = current / part
        _ensure_real_dir(current, inspector)


def _assert_real_dir(path: Path, inspector: FileInspector) -> None:
    facts = inspector(path)
    if facts.is_symlink:
        raise ExchangeError(
            f"exchange path component is a symlink/reparse point: {path.as_posix()}"
        )
    if not facts.is_dir:
        raise ExchangeError(f"exchange path component is not a directory: {path.as_posix()}")


def _ensure_real_dir(path: Path, inspector: FileInspector) -> None:
    if os.path.lexists(path):
        _assert_real_dir(path, inspector)
    else:
        path.mkdir()


def _assert_target_replaceable(target: Path, inspector: FileInspector) -> None:
    """A pre-existing target may be overwritten only if it is a regular single-link file."""
    if not os.path.lexists(target):
        return
    facts = inspector(target)
    if facts.is_symlink or not facts.is_regular:
        raise ExchangeError(f"exchange target is not a regular file: {target.as_posix()}")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` atomically with deterministic LF bytes (temp in the same dir + os.replace)."""
    tmp = path.with_name(f"{path.name}.worc-io.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


# --- Clean-surface manifest ----------------------------------------------------------------------


@dataclass(frozen=True)
class ExchangeEntry:
    """A regular single-link file in the exchange, fingerprinted for the clean-surface manifest."""

    relname: str  # POSIX, relative to the task exchange dir
    is_regular: bool
    link_count: int
    size: int
    sha256: str


@dataclass(frozen=True)
class ExchangeManifest:
    """The clean-surface fingerprint of one task's exchange (WRI-002/WRI-007 diff two of these)."""

    task_id: str
    entries: tuple[ExchangeEntry, ...]


def build_exchange_manifest(
    task_dir: str | Path, task_id: str, *, inspect: FileInspector | None = None
) -> ExchangeManifest:
    """Walk the task exchange dir and return its manifest, or raise on the first violation.

    Fails closed (:class:`ExchangeError`) on a symlink/reparse point on any component, a non-regular
    (special) file, a hard link (``link_count != 1``), an NTFS alternate data stream, or a
    case-fold/NFC sibling-name collision. Every published object must be a regular single-link file.
    """
    inspector = inspect or default_file_inspector()
    root = Path(task_dir)
    _ensure_existing_real_dir(root, inspector)
    entries: list[ExchangeEntry] = []
    _walk_dir(root, root, inspector, entries)
    return ExchangeManifest(task_id=task_id, entries=tuple(entries))


def _ensure_existing_real_dir(path: Path, inspector: FileInspector) -> None:
    if not os.path.lexists(path):
        raise ExchangeError(f"exchange dir does not exist: {path.as_posix()}")
    facts = inspector(path)
    if facts.is_symlink or not facts.is_dir:
        raise ExchangeError(f"exchange dir is not a real directory: {path.as_posix()}")


def _walk_dir(
    root: Path, current: Path, inspector: FileInspector, entries: list[ExchangeEntry]
) -> None:
    seen: dict[str, str] = {}
    for child in sorted(current.iterdir()):
        norm = unicodedata.normalize("NFC", child.name).casefold()
        if norm in seen:
            raise ExchangeError(f"exchange name collision: {child.name!r} vs {seen[norm]!r}")
        seen[norm] = child.name
        facts = inspector(child)
        if facts.is_symlink:
            raise ExchangeError(f"exchange entry is a symlink/reparse point: {child.as_posix()}")
        if facts.is_dir:
            _walk_dir(root, child, inspector, entries)
        elif facts.is_regular:
            if facts.link_count != 1:
                raise ExchangeError(
                    f"exchange entry is hard-linked ({facts.link_count}): {child.as_posix()}"
                )
            if facts.alt_streams:
                raise ExchangeError(
                    f"exchange entry has NTFS alternate data streams "
                    f"{facts.alt_streams!r}: {child.as_posix()}"
                )
            entries.append(
                ExchangeEntry(
                    relname=child.relative_to(root).as_posix(),
                    is_regular=True,
                    link_count=facts.link_count,
                    size=facts.size,
                    sha256=sha256_file(child),
                )
            )
        else:
            raise ExchangeError(
                f"exchange entry is a special (non-regular) file: {child.as_posix()}"
            )


# --- Pre-launch invariants -----------------------------------------------------------------------


def assert_exchange_current_task_only(
    exchange_root: str | Path, task_id: str, *, inspect: FileInspector | None = None
) -> None:
    """Fail closed unless the exchange root holds at most the current task's directory.

    The pre-launch invariant: the exchange root is a real (non-symlink) directory whose only child
    is ``<task_id>`` (a real directory), or it is absent/empty. A foreign task dir, a stray file, or
    a symlinked root/task dir fails closed. Terminal sealing/restoration is WRI-007.
    """
    inspector = inspect or default_file_inspector()
    root = Path(exchange_root)
    if not os.path.lexists(root):
        return
    facts = inspector(root)
    if facts.is_symlink or not facts.is_dir:
        raise ExchangeError(f"exchange root is not a real directory: {root.as_posix()}")
    for child in sorted(root.iterdir()):
        if child.name != task_id:
            raise ExchangeError(
                f"stale/foreign entry in exchange root: {child.name!r} (expected only {task_id!r})"
            )
        cfacts = inspector(child)
        if cfacts.is_symlink or not cfacts.is_dir:
            raise ExchangeError(f"exchange task entry is not a real directory: {child.as_posix()}")


def assert_orchestration_paths_contained(
    request: AgentRunRequest, exchange_root: str | Path
) -> None:
    """Fail closed unless every non-``None`` provider input path resolves under the exchange root.

    ``working_directory`` (the live repo workspace) is the only permitted non-exchange path. Applies
    identically to the agent, evaluator, and supervisor requests, on fresh and resumed launches.
    """
    root = Path(exchange_root).resolve()
    named: list[tuple[str, str]] = [
        (name, value)
        for name, value in (
            ("task_path", request.task_path),
            ("plan_path", request.plan_path),
            ("diff_path", request.diff_path),
            ("check_artifacts_path", request.check_artifacts_path),
            ("review_artifacts_path", request.review_artifacts_path),
            ("human_input_path", request.human_input_path),
            ("repository_instructions_path", request.repository_instructions_path),
        )
        if value
    ]
    named += [
        (f"skill_reference_paths[{i}]", value)
        for i, value in enumerate(request.skill_reference_paths)
    ]
    for name, value in named:
        try:
            Path(value).resolve().relative_to(root)
        except ValueError:
            raise ExchangeError(
                f"provider request field {name!r} is not under the current exchange: {value}"
            ) from None


# --- Lifecycle -----------------------------------------------------------------------------------


def clear_exchange_task_dir(exchange_root: str | Path, task_id: str) -> None:
    """Remove a task's active exchange dir (fresh/restart start clean; interim terminal reset).

    Interim helper: WRI-007 replaces the terminal use with a quiescence-gated seal → checksum-verify
    into private audit → remove, plus contaminated-tree quarantine. Robust Windows read-only/locked
    handling is likewise WRI-007's; here a failure surfaces rather than being swallowed.
    """
    task_dir = exchange_task_dir(exchange_root, task_id)
    if os.path.lexists(task_dir):
        shutil.rmtree(task_dir)


# --- Native-Windows helpers (exercised on a real Windows host by WRI-006) ------------------------


def _windows_link_count(path: Path) -> int:
    """The NTFS hard-link count via ``GetFileInformationByHandle`` (Windows only)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    generic_read = 0x80000000
    file_share_all = 0x1 | 0x2 | 0x4
    open_existing = 3
    flag_backup_semantics = 0x02000000  # required to open a directory handle

    handle = kernel32.CreateFileW(
        str(path),
        generic_read,
        file_share_all,
        None,
        open_existing,
        flag_backup_semantics,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ExchangeError(f"cannot open exchange file to verify link count: {path.as_posix()}")
    try:

        class _Info(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        info = _Info()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ExchangeError(f"cannot read exchange file link count: {path.as_posix()}")
        return int(info.nNumberOfLinks)
    finally:
        kernel32.CloseHandle(handle)


def _windows_alt_streams(path: Path) -> tuple[str, ...]:
    """Named NTFS alternate data streams beyond the default ``::$DATA`` (Windows only)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    find_first_stream_info_standard = 0

    class _StreamData(ctypes.Structure):
        _fields_ = [
            ("StreamSize", wintypes.LARGE_INTEGER),
            ("StreamName", wintypes.WCHAR * 296),
        ]

    data = _StreamData()
    handle = kernel32.FindFirstStreamW(
        str(path), find_first_stream_info_standard, ctypes.byref(data), 0
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ExchangeError(f"cannot enumerate NTFS streams: {path.as_posix()}")
    streams: list[str] = []
    try:
        while True:
            name = str(data.StreamName)
            if name and name != "::$DATA":
                streams.append(name)
            if not kernel32.FindNextStreamW(handle, ctypes.byref(data)):
                break
    finally:
        kernel32.CloseHandle(handle)
    return tuple(streams)
