"""Canonical memory-store layout (design §3).

Resolves the **task-independent**, gitignored ``<repo>/.worc/memory/`` tree and seeds it on demand.
Two hard constraints from the design:

* The store is **never** routed through ``task_artifact_dir`` — it lives under the ``.worc``
  home and outlives any single task. :class:`MemoryLayout` is therefore built from the ``.worc``
  home, not a per-task directory.
* No new ``.gitignore`` rule is needed: ``install`` already ignores the whole ``.worc/`` home
  wholesale (``git_manager.RUNTIME_GITIGNORE_LINES``), so the entire store — and the per-task
  packets under ``.worc/logs/<task-id>/`` — are already covered.

All stored/compared path strings use POSIX form (``Path.as_posix()``) so records round-trip
identically on Windows and POSIX.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wastech_orchestrator.memory._io import atomic_write_json, atomic_write_text

# On-disk store format version — distinct from ``config.CONFIG_SCHEMA_VERSION``. Bumped only when
# the *layout/record* format changes; recorded in ``manifest.json`` as the hook for a future
# migration.
MEMORY_SCHEMA_VERSION = 1

# The ``.worc``-relative directory holding the canonical store.
_MEMORY_DIRNAME = "memory"

_README_TEXT = """\
# .worc/memory — canonical orchestrator memory (do not edit by hand)

This directory is the **canonical, task-independent** store for the orchestrator's persistent,
repo-scoped memory. It is part of the gitignored `.worc/` home and is **never committed** and never
appears in a PR diff.

Contents are written deterministically by the orchestrator (redacted, atomic, audited). Editing
files here by hand bypasses redaction and the audit log — use `worc memory ...` instead. Everything
under `derived/` is a rebuildable cache, not memory truth.
"""


class MemoryLayout:
    """Resolves the canonical ``.worc/memory/`` tree from the ``.worc`` home (design §3).

    Pure path resolution — no I/O happens on construction. Call :meth:`ensure_tree` (or
    :func:`ensure_store`) to materialize the directories.
    """

    def __init__(self, private_home: str | Path) -> None:
        # The resolved private runtime home (``layout.private_home``, WRI-004), injected by the
        # caller — the memory store lives under ``<private_home>/memory/``. No literal ``.worc`` is
        # reconstructed here; the layout owns that name.
        self._worc_home = Path(private_home)

    @property
    def root(self) -> Path:
        return self._worc_home / _MEMORY_DIRNAME

    @property
    def long_term(self) -> Path:
        return self.root / "long_term"

    @property
    def short_term(self) -> Path:
        return self.root / "short_term"

    @property
    def entities(self) -> Path:
        return self.root / "entities"

    @property
    def audit(self) -> Path:
        return self.root / "audit"

    @property
    def quarantine(self) -> Path:
        return self.root / "quarantine"

    @property
    def derived(self) -> Path:
        return self.root / "derived"

    @property
    def snapshots(self) -> Path:
        """Snapshot home, created lazily per cleanup batch (not by :meth:`ensure_tree`)."""
        return self.audit / "snapshots"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def readme_path(self) -> Path:
        return self.root / "README.md"

    # The canonical tier directories created up front. Per-task ``short_term/runs/<id>/`` and
    # ``audit/snapshots/<ts>/`` are created lazily by the code that writes into them.
    def _tier_dirs(self) -> tuple[Path, ...]:
        return (
            self.long_term,
            self.short_term,
            self.entities,
            self.audit,
            self.quarantine,
            self.derived,
        )

    def ensure_tree(self) -> None:
        """Idempotently create the canonical tier directories (safe to call repeatedly)."""
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in self._tier_dirs():
            directory.mkdir(parents=True, exist_ok=True)

    def as_posix(self) -> str:
        """The store root as a POSIX path string (the form stored/compared everywhere)."""
        return self.root.as_posix()


def build_manifest(*, created_at: str) -> dict[str, Any]:
    """The ``manifest.json`` payload: store-format version, creation stamp, and tier placeholders.

    ``created_at`` is injected by the caller (never read from a hidden clock) so writes are
    deterministic and testable. The ``tiers`` block holds placeholders now; a later phase populates
    the real caps/TTLs from ``MemoryConfig``.
    """
    return {
        "memory_schema_version": MEMORY_SCHEMA_VERSION,
        "created_at": created_at,
        "tiers": {
            "short_term": {"ttl_days": None},
            "long_term": {"ttl_days": None},
            "entity": {"validate_on_touch": True},
        },
    }


def ensure_store(layout: MemoryLayout, *, created_at: str) -> None:
    """Create the tree, then seed ``manifest.json`` and ``README.md`` when they are absent.

    Idempotent and non-destructive — an existing manifest/README is **never** clobbered (so the
    original ``created_at`` survives a re-run). ``created_at`` is injected for determinism.
    """
    layout.ensure_tree()
    if not layout.manifest_path.exists():
        atomic_write_json(layout.manifest_path, build_manifest(created_at=created_at))
    if not layout.readme_path.exists():
        atomic_write_text(layout.readme_path, _README_TEXT)
