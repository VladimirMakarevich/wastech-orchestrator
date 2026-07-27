"""Seal terminal exchanges and restore only for continue.

The exchange ``<repo>/.worc-io/<task-id>/`` is an agent-readable, in-repository surface. The
single-active-exchange model requires that, immediately before any provider launch, the exchange
root holds at most the current task's verified active exchange — a terminal task must not leave its
curated plan/diff/findings visible to the next task. The interim teardown simply
``rmtree``-d the exchange at terminal, discarding the audit trail; this module replaces it with the
real sealing protocol and adds the restore-for-continue path.

Four operations, owned by the artifact/lifecycle layer:

* :func:`seal_exchange` — after the quiescence barrier has proven the provider tree empty, build
  and verify a
  checksum manifest of the active exchange, copy it into a fresh versioned private snapshot
  (``<private_home>/exchange-seals/<task-id>/seal-<NNNNNN>/`` + ``manifest.json``), re-verify, then
  remove the active in-repo directory. Cross-volume safe (copy → verify → atomic rename → remove).
* :func:`restore_for_continue` — restore the latest verified sealed snapshot into a clean active
  exchange for an authorized ``rerun --continue`` of a terminal resumable task.
* :func:`ensure_current_exchange` — the resume decision seam: a parked/crashed nonterminal task
  verifies and reuses its still-active exchange; a terminal resumable task restores the latest seal;
  a contaminated or unsafe task is refused (fresh/restart required).
* :func:`quarantine_contaminated` — when the tamper check reports an agent-side exchange
  mutation, move the
  tree to a clearly contaminated private evidence location with the parent-held expected and
  observed manifests; it is never sealed and never restore-eligible.

Identity is enforced by reusing the exchange's no-follow inspector, the shared containment belt
(:func:`~wastech_orchestrator.providers.artifacts.assert_contained_path`), and the exchange manifest
(:func:`~wastech_orchestrator.providers.exchange.build_exchange_manifest`, which already refuses a
symlink/reparse point, hard link, special file, or NTFS alternate data stream) — no new identity
code. ``core.flow`` may import the ``providers`` interface leaves (``exchange``/``artifacts``) and
the stdlib-only ``runtime_layout``; they never import ``core``, so the import-linter stays green.

Cross-platform: ``pathlib`` throughout, POSIX-form stored paths, ``newline=""``/bytes for copied
files, no ``ignore_errors=True`` (a swallowed failure makes the launch-blocking state unknowable),
and bounded/observable retries plus a read-only-attribute clear for Windows sharing violations.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.core.flow.frozen_bundle import (
    FrozenBundleError,
    digest_entries,
    inspect_frozen_source,
)
from wastech_orchestrator.providers.artifacts import assert_contained_path, exchange_task_dir
from wastech_orchestrator.providers.exchange import (
    ExchangeManifest,
    FileInspector,
    build_exchange_manifest,
    default_file_inspector,
)
from wastech_orchestrator.runtime_layout import (
    EXCHANGE_QUARANTINE_DIRNAME,
    EXCHANGE_SEAL_DIRNAME,
)

#: Bump when the on-disk snapshot layout / manifest schema changes (an older snapshot then fails to
#: verify and continue is refused — fresh/restart required).
_SEAL_FORMAT = 1

MANIFEST_NAME = "manifest.json"

#: Bounded retry budget for a transient Windows sharing violation during active-dir removal / move.
_REMOVE_ATTEMPTS = 5
_REMOVE_BACKOFF_SECONDS = 0.1

#: An injectable clock hook so tests can drive the retry loop without real sleeps.
Sleeper = Callable[[float], None]
#: An injectable tree-remover so tests can simulate a Windows lock / retry exhaustion.
TreeRemover = Callable[[Path], None]


class ExchangeSealError(FrozenBundleError):
    """A terminal exchange could not be sealed, verified, or restored (fail-closed)."""


class ExchangeCleanupBlocked(ExchangeSealError):
    """The active exchange was sealed but could not be safely removed/relocated.

    Raised after a Windows lock/read-only exhausts the bounded retries (or a contaminated tree
    cannot be moved). The already-recorded terminal status must not change; the caller marks the
    task ``exchange_active_unsafe`` so every later provider launch is blocked until it is resolved.
    """


@dataclass(frozen=True)
class SealResult:
    """The outcome of :func:`seal_exchange` for one terminal task."""

    seal_dir: Path
    seal_no: int
    manifest_digest: str
    entry_count: int


@dataclass(frozen=True)
class RestoreResult:
    """The outcome of :func:`ensure_current_exchange` / :func:`restore_for_continue`."""

    task_dir: Path
    manifest_digest: str
    #: ``True`` when a sealed snapshot was materialized; ``False`` when a still-active nonterminal
    #: exchange was verified and reused unchanged.
    restored: bool


# --- Private layout ------------------------------------------------------------------------------


def exchange_seal_root(private_home: str | Path, task_id: str) -> Path:
    """The per-task private root holding every sealed snapshot (a provider deny target)."""
    return Path(private_home) / EXCHANGE_SEAL_DIRNAME / task_id


def exchange_quarantine_root(private_home: str | Path, task_id: str) -> Path:
    """The per-task private root holding quarantined contaminated exchange evidence."""
    return Path(private_home) / EXCHANGE_QUARANTINE_DIRNAME / task_id


def _next_index(root: Path, prefix: str) -> int:
    """The next ``<prefix><NNNNNN>`` sequence number under ``root`` (highest existing + 1)."""
    if not root.exists():
        return 1
    highest = 0
    for child in root.iterdir():
        if child.is_dir() and child.name.startswith(prefix):
            try:
                highest = max(highest, int(child.name[len(prefix) :]))
            except ValueError:
                continue
    return highest + 1


def _latest_seal_dir(seals_root: Path) -> tuple[int, Path] | None:
    """The highest-numbered ``seal-<NNNNNN>`` directory under ``seals_root``, or ``None``."""
    if not seals_root.exists():
        return None
    best: tuple[int, Path] | None = None
    for child in seals_root.iterdir():
        if child.is_dir() and child.name.startswith("seal-"):
            try:
                no = int(child.name[len("seal-") :])
            except ValueError:
                continue
            if best is None or no > best[0]:
                best = (no, child)
    return best


# --- Manifest (de)serialization ------------------------------------------------------------------


def _manifest_digest(manifest: ExchangeManifest) -> str:
    """A stable identity digest over the manifest's ``(relname, sha256)`` pairs.

    Shares the control bundle's digest helper, so both surfaces hash identically.
    """
    return digest_entries([(e.relname, e.sha256) for e in manifest.entries])


def _write_manifest(
    path: Path, manifest: ExchangeManifest, *, seal_no: int, metadata: Mapping[str, str]
) -> str:
    """Write the snapshot ``manifest.json`` and return its content digest."""
    digest = _manifest_digest(manifest)
    doc = {
        "format": _SEAL_FORMAT,
        "task_id": manifest.task_id,
        "seal_no": seal_no,
        "manifest_digest": digest,
        "entries": [
            {"path": e.relname, "size": e.size, "link_count": e.link_count, "sha256": e.sha256}
            for e in sorted(manifest.entries, key=lambda e: e.relname)
        ],
        "metadata": dict(metadata),
    }
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")
    return digest


def _read_manifest_digest(seal_dir: Path) -> str:
    """Read + shape-check a snapshot ``manifest.json``; return its recorded ``manifest_digest``."""
    path = seal_dir / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExchangeSealError(
            f"cannot read exchange seal manifest {path.as_posix()}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise ExchangeSealError(f"malformed exchange seal manifest {path.as_posix()}")
    if raw.get("format") != _SEAL_FORMAT:
        raise ExchangeSealError(
            f"unsupported exchange seal format {raw.get('format')!r} (expected {_SEAL_FORMAT})"
        )
    recorded = raw.get("manifest_digest")
    if not isinstance(recorded, str):
        raise ExchangeSealError(f"exchange seal manifest missing its digest {path.as_posix()}")
    return recorded


# --- Copy / verify / remove primitives (cross-platform) ------------------------------------------


def _copy_tree_verified(
    src_manifest: ExchangeManifest, src_dir: Path, dest_dir: Path, inspector: FileInspector
) -> None:
    """Copy every manifest entry ``src_dir`` → ``dest_dir`` (bytes+mode) and verify size+digest.

    Each source is inspected no-follow before the copy (a symlink/hard-link/special/ADS is a
    security failure, never a copy target); ``shutil.copy2`` writes a fresh regular file, so a copy
    can never be a hard link/symlink back to live data. The destination is re-fingerprinted against
    the source manifest so a short read or a swapped byte fails closed before the snapshot seals.
    """
    for entry in src_manifest.entries:
        source = assert_contained_path(src_dir, src_dir / entry.relname)
        inspect_frozen_source(
            Path(source), inspector, label="exchange artifact", error_cls=ExchangeSealError
        )
        dest = assert_contained_path(dest_dir, dest_dir / entry.relname)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    verified = build_exchange_manifest(dest_dir, src_manifest.task_id, inspect=inspector)
    if _manifest_digest(verified) != _manifest_digest(src_manifest):
        raise ExchangeSealError(
            f"copied exchange snapshot {dest_dir.as_posix()} does not match its source manifest"
        )


def _default_remove_tree(path: Path) -> None:
    """Recursively delete a *known-clean* (already manifest-verified) real directory tree.

    Manual recursion rather than ``shutil.rmtree(ignore_errors=True)``: a swallowed error would make
    the launch-blocking state unknowable. A Windows read-only attribute is cleared once (``chmod``
    ``S_IWRITE``) and the unlink/rmdir retried; a symlink is unlinked, never followed.
    """
    if path.is_symlink() or not path.is_dir():
        _unlink_with_readonly_clear(path)
        return
    for child in path.iterdir():
        _default_remove_tree(child)
    _rmdir_with_readonly_clear(path)


def _unlink_with_readonly_clear(path: Path) -> None:
    try:
        path.unlink()
    except PermissionError:
        path.chmod(stat.S_IWRITE)
        path.unlink()


def _rmdir_with_readonly_clear(path: Path) -> None:
    try:
        path.rmdir()
    except PermissionError:
        path.chmod(stat.S_IWRITE)
        path.rmdir()


def _remove_active_dir(
    path: Path, *, remover: TreeRemover, sleeper: Sleeper, attempts: int = _REMOVE_ATTEMPTS
) -> None:
    """Remove ``path`` with bounded, observable retries for a transient Windows sharing violation.

    Never swallows the final failure: after the budget is exhausted it raises
    :class:`ExchangeCleanupBlocked` naming the exact target, so the caller can mark the task unsafe
    and block later launches instead of falsely reporting a clean teardown.
    """
    last: OSError | None = None
    for attempt in range(attempts):
        try:
            remover(path)
            return
        except OSError as exc:
            last = exc
            if attempt + 1 < attempts:
                sleeper(_REMOVE_BACKOFF_SECONDS * (attempt + 1))
    raise ExchangeCleanupBlocked(
        f"could not remove active exchange {path.as_posix()} after {attempts} attempts: {last}"
    )


def _atomic_promote(staging: Path, final: Path) -> None:
    """Atomically rename a completed ``staging`` dir into ``final`` (same private filesystem)."""
    staging.replace(final)


# --- Operations ----------------------------------------------------------------------------------


def seal_exchange(
    exchange_root: str | Path,
    private_home: str | Path,
    task_id: str,
    *,
    metadata: Mapping[str, str] | None = None,
    inspect: FileInspector | None = None,
    remover: TreeRemover | None = None,
    sleeper: Sleeper | None = None,
) -> SealResult | None:
    """Seal the active exchange into a fresh private snapshot, then remove the in-repo directory.

    Returns ``None`` when there is no active exchange to seal (idempotent — a terminal path that
    already ran its teardown, or a task that never published). Otherwise builds+verifies the active
    exchange manifest (a path-safety violation raised by the walk propagates as a security failure),
    copies the tree into ``seal-<NNNNNN>/`` under a private temp sibling, verifies it, atomically
    promotes it, and removes the active directory. If removal is blocked (Windows lock), the sealed
    snapshot still exists and :class:`ExchangeCleanupBlocked` is raised so the caller blocks later
    launches without losing the seal.
    """
    inspector = inspect or default_file_inspector()
    remove = remover or _default_remove_tree
    sleep = sleeper or time.sleep
    task_dir = exchange_task_dir(exchange_root, task_id)
    if not os.path.lexists(task_dir):
        return None

    manifest = build_exchange_manifest(task_dir, task_id, inspect=inspector)
    seals_root = exchange_seal_root(private_home, task_id)
    seals_root.mkdir(parents=True, exist_ok=True)
    seal_no = _next_index(seals_root, "seal-")
    final = assert_contained_path(seals_root, seals_root / f"seal-{seal_no:06d}")
    staging = assert_contained_path(seals_root, seals_root / f".seal-{seal_no:06d}.tmp")
    if os.path.lexists(staging):
        _remove_active_dir(Path(staging), remover=remove, sleeper=sleep)
    Path(staging).mkdir(parents=False, exist_ok=False)

    _copy_tree_verified(manifest, Path(task_dir), Path(staging), inspector)
    digest = _write_manifest(
        Path(staging) / MANIFEST_NAME, manifest, seal_no=seal_no, metadata=metadata or {}
    )
    _atomic_promote(Path(staging), Path(final))

    # Snapshot is sealed and verified; now drop the active in-repo exchange. A blocked removal keeps
    # the seal (restore stays possible) but must block later launches — surfaced via the exception.
    _remove_active_dir(Path(task_dir), remover=remove, sleeper=sleep)
    return SealResult(
        seal_dir=Path(final),
        seal_no=seal_no,
        manifest_digest=digest,
        entry_count=len(manifest.entries),
    )


def restore_for_continue(
    exchange_root: str | Path,
    private_home: str | Path,
    task_id: str,
    *,
    inspect: FileInspector | None = None,
) -> RestoreResult:
    """Restore the latest verified sealed snapshot into a clean active exchange for continue.

    Verifies the snapshot against its recorded digest with the no-follow identity checks, then it
    it into a private temp sibling under the exchange root, atomically promotes it into
    ``<exchange_root>/<task-id>/`` and re-verifies. Refuses (state-conflict) if the active directory
    already exists (never merges unrelated contents) or if no sealed snapshot exists.
    """
    inspector = inspect or default_file_inspector()
    seals_root = exchange_seal_root(private_home, task_id)
    latest = _latest_seal_dir(seals_root)
    if latest is None:
        raise ExchangeSealError(
            f"no sealed exchange snapshot to restore for task {task_id!r}; fresh/restart required"
        )
    seal_no, seal_dir = latest
    recorded = _read_manifest_digest(seal_dir)
    sealed = build_exchange_manifest(seal_dir, task_id, inspect=inspector)
    # The seal dir also holds manifest.json; exclude it from the content fingerprint comparison.
    sealed_entries = tuple(e for e in sealed.entries if e.relname != MANIFEST_NAME)
    content = ExchangeManifest(task_id=task_id, entries=sealed_entries)
    if _manifest_digest(content) != recorded:
        raise ExchangeSealError(
            f"sealed exchange snapshot {seal_dir.as_posix()} drifted from its recorded digest"
        )

    task_dir = Path(exchange_task_dir(exchange_root, task_id))
    if os.path.lexists(task_dir):
        raise ExchangeSealError(
            f"cannot restore over an existing active exchange {task_dir.as_posix()} "
            f"(state conflict — refusing to merge)"
        )
    root = Path(exchange_root)
    root.mkdir(parents=True, exist_ok=True)
    staging = assert_contained_path(root, root / f".restore-{task_id}.tmp")
    if os.path.lexists(staging):
        _default_remove_tree(Path(staging))
    Path(staging).mkdir(parents=False, exist_ok=False)
    _copy_tree_verified(content, seal_dir, Path(staging), inspector)
    _atomic_promote(Path(staging), task_dir)

    verified = build_exchange_manifest(task_dir, task_id, inspect=inspector)
    if _manifest_digest(verified) != recorded:
        raise ExchangeSealError(
            f"restored exchange {task_dir.as_posix()} does not match sealed snapshot {seal_no}"
        )
    return RestoreResult(task_dir=task_dir, manifest_digest=recorded, restored=True)


def ensure_current_exchange(
    exchange_root: str | Path,
    private_home: str | Path,
    task_id: str,
    *,
    contaminated: bool,
    active_unsafe: bool,
    inspect: FileInspector | None = None,
) -> RestoreResult:
    """Establish the verified current-task exchange before a resume launch (the decision seam).

    * ``active_unsafe`` — a prior seal/removal was blocked; refuse until the operator resolves it.
    * ``contaminated`` — the tamper check flagged an agent-side mutation; continue is refused.
    * a still-active exchange (parked/crashed nonterminal continue) is verified and reused unchanged
      — never overwritten from an older seal.
    * otherwise (terminal resumable continue) restore the latest verified sealed snapshot.
    """
    if active_unsafe:
        raise ExchangeSealError(
            f"task {task_id!r} has an unsafe active exchange (a prior seal/cleanup was blocked); "
            f"resolve it before continuing"
        )
    if contaminated:
        raise ExchangeSealError(
            f"task {task_id!r} exchange was flagged contaminated by mutation detection; "
            f"continue is refused — a fresh/restart rerun is required"
        )
    inspector = inspect or default_file_inspector()
    task_dir = Path(exchange_task_dir(exchange_root, task_id))
    if os.path.lexists(task_dir):
        # Parked/crashed nonterminal continue: the same-task active exchange is still here. Verify
        # it is intact (a path-safety/identity violation fails closed) and reuse it — do not replace
        # a live exchange with an older snapshot.
        manifest = build_exchange_manifest(task_dir, task_id, inspect=inspector)
        return RestoreResult(
            task_dir=task_dir, manifest_digest=_manifest_digest(manifest), restored=False
        )
    if _latest_seal_dir(exchange_seal_root(private_home, task_id)) is None:
        # No active exchange and nothing sealed: a continue for a task that never published to the
        # exchange. Proceed with an empty exchange — the downstream input restore rebuilds the
        # required paths from the private audit. (A stale/foreign exchange is caught separately by
        # the pre-launch ``assert_exchange_current_task_only`` gate before any provider runs.)
        return RestoreResult(task_dir=task_dir, manifest_digest="", restored=False)
    return restore_for_continue(exchange_root, private_home, task_id, inspect=inspector)


def quarantine_contaminated(
    exchange_root: str | Path,
    private_home: str | Path,
    task_id: str,
    *,
    expected: ExchangeManifest | None,
    observed_changes: tuple[str, ...],
) -> Path:
    """Move a mutation-flagged exchange tree to private contaminated evidence; never seal/restore.

    The active tree is relocated wholesale (a same-filesystem rename — the tree is agent-mutated and
    may contain planted links, so it is never walked/copied by following contents). The parent-held
    expected manifest and the observed change list are recorded alongside it. Returns the evidence
    directory. A cross-volume relocation that cannot rename raises :class:`ExchangeCleanupBlocked`,
    the caller marks the task unsafe (the contaminated tree stays put, later launches are blocked).
    """
    task_dir = Path(exchange_task_dir(exchange_root, task_id))
    quarantine_root = exchange_quarantine_root(private_home, task_id)
    quarantine_root.mkdir(parents=True, exist_ok=True)
    index = _next_index(quarantine_root, "")
    evidence = assert_contained_path(quarantine_root, quarantine_root / f"{index:06d}")
    Path(evidence).mkdir(parents=False, exist_ok=False)

    _write_evidence(
        Path(evidence),
        expected=expected,
        observed_changes=observed_changes,
        task_id=task_id,
    )
    if os.path.lexists(task_dir):
        dest = assert_contained_path(evidence, Path(evidence) / "tree")
        try:
            task_dir.replace(dest)
        except OSError as exc:
            raise ExchangeCleanupBlocked(
                f"could not quarantine contaminated exchange {task_dir.as_posix()}: {exc}"
            ) from exc
    return Path(evidence)


def _write_evidence(
    evidence: Path,
    *,
    expected: ExchangeManifest | None,
    observed_changes: tuple[str, ...],
    task_id: str,
) -> None:
    """Record the parent-held expected manifest and the observed change list beside the evidence."""
    doc = {
        "format": _SEAL_FORMAT,
        "task_id": task_id,
        "expected_manifest": (
            None
            if expected is None
            else {
                "manifest_digest": _manifest_digest(expected),
                "entries": [
                    {"path": e.relname, "size": e.size, "sha256": e.sha256}
                    for e in sorted(expected.entries, key=lambda e: e.relname)
                ],
            }
        ),
        "observed_changes": list(observed_changes),
    }
    (evidence / "evidence.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline=""
    )
