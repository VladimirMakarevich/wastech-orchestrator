"""Partial-change snapshot contract.

Defines *only the data and the protocol* the Router and Core exchange around a stage run — the
actual git/snapshot execution is the Git Manager + Core (P5). Capturing this contract now lets P5
wire real git behind it without reshaping the Router.

The rule it encodes: when the primary provider fails with an **infrastructure** error *after*
files were changed, the orchestrator does **not** roll back automatically; instead it snapshots the
post-attempt state and hands the fallback the current diff plus a "partial attempt" note. There is
deliberately no rollback operation on this protocol — its absence is the no-auto-rollback guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class WorkingTreeSnapshot:
    """A point-in-time view of the target working tree (the "before"/"after" capture)."""

    commit_sha: str
    porcelain_status: str  # output of `git status --porcelain`
    diff_checksum: str  # checksum of the working-tree diff, to detect whether files changed
    artifacts: tuple[str, ...]  # the list of existing artifacts at capture time


@dataclass(frozen=True)
class PartialChange:
    """Produced after an infra failure that changed files; consumed by the fallback attempt.

    The Router sets the fallback request's ``diff_path`` to :attr:`diff_path`; ``note`` is the
    "partial attempt" message P5 weaves into the fallback's prompt context.
    """

    before: WorkingTreeSnapshot
    after: WorkingTreeSnapshot
    diff_path: str
    note: str


@runtime_checkable
class SnapshotHook(Protocol):
    """Implemented by the Git Manager / Core (P5); the Router only ever calls it.

    Intentionally has no rollback/restore method: partial changes are never undone automatically.
    """

    def capture(self) -> WorkingTreeSnapshot:
        """Snapshot the current working tree before an attempt."""
        ...

    def partial_change_since(self, before: WorkingTreeSnapshot) -> PartialChange | None:
        """Return the partial change since ``before``, or None when no files changed."""
        ...
