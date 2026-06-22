"""Resolved output policy — where a flow's writing nodes may write, and what they must produce.

The scalar :class:`~wastech_orchestrator.core.flow.contracts.OutputPolicy` declared on a flow
resolves here into a foundation :class:`ResolvedOutputPolicy`: the single repo-relative directory
the flow's writing nodes may write into, the files the flow must produce there, and whether the
deliverable is *private* (must never enter git). The same resolution is the seam shared by P3.1 (the
``citation`` checker reads ``sources.json`` from the report directory) and P3.2 (the after-stage
write guard + the publish node enforce containment / privacy against it).

Pure: no IO, no git, no provider knowledge — only the policy → (path, required files, privacy)
mapping plus a path-containment predicate. The engine carries no domain knowledge of *which* flow
uses *which* policy; a flow selects a policy by name and the core resolves it the same way for all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.core.flow.contracts import OutputPolicy

#: Report directory layout (repo-relative). ``repository_document`` deliverables are committable
#: documents under ``docs/research/``; ``private_control_workspace_report`` deliverables live under
#: the gitignored ``.worc/`` control workspace and never enter a commit.
_RESEARCH_DIR = "docs/research"
_PRIVATE_REPORT_DIR = ".worc/security-reports"


@dataclass(frozen=True, slots=True)
class ResolvedOutputPolicy:
    """The foundation form of a flow's ``output_policy``.

    ``report_subdir`` is the *only* directory the flow's writing nodes may write into (repo-relative
    POSIX), or ``None`` for ``code_change`` (the deliverable is the code change itself — the writes
    are the diff, anywhere in the repo, guarded by the existing dangerous-diff path).
    ``required_files``
    are the deliverables the flow must produce in that directory (checked at publish). ``private``
    marks a deliverable that must never enter git staging / a commit / a PR (fail-closed).
    """

    policy: OutputPolicy
    report_subdir: str | None
    required_files: tuple[str, ...]
    private: bool

    def report_dir(self, repo_dir: str | Path) -> Path | None:
        """The absolute report directory under *repo_dir*, or ``None`` for ``code_change``."""
        if self.report_subdir is None:
            return None
        return Path(repo_dir) / self.report_subdir


def resolve_output_policy(policy: OutputPolicy, task_id: str) -> ResolvedOutputPolicy:
    """Resolve a scalar ``output_policy`` for a task into its foundation form.

    The per-task subdirectory keeps concurrent/serial tasks from colliding and makes the report
    self-identifying. ``task_id`` is the normalized id (``[a-z0-9][a-z0-9._-]*``), so it is a
    safe single path segment — never an absolute path or a traversal.
    """
    if policy is OutputPolicy.REPOSITORY_DOCUMENT:
        return ResolvedOutputPolicy(
            policy=policy,
            report_subdir=f"{_RESEARCH_DIR}/{task_id}",
            required_files=("report.md", "sources.json"),
            private=False,
        )
    if policy is OutputPolicy.PRIVATE_CONTROL_WORKSPACE_REPORT:
        return ResolvedOutputPolicy(
            policy=policy,
            report_subdir=f"{_PRIVATE_REPORT_DIR}/{task_id}",
            required_files=("report.md",),
            private=True,
        )
    # code_change: no dedicated report directory; the deliverable is the code diff.
    return ResolvedOutputPolicy(policy=policy, report_subdir=None, required_files=(), private=False)


def within_subdir(path: str, subdir: str) -> bool:
    """True iff repo-relative *path* is *subdir* itself or a descendant of it (POSIX comparison).

    Used by the after-stage write guard and the private-report publish to test whether a changed
    path falls inside the policy's report directory. Both arguments are repo-relative.
    """
    normalized = path.replace("\\", "/").strip("/")
    base = subdir.replace("\\", "/").strip("/")
    return normalized == base or normalized.startswith(f"{base}/")


def is_within(root: str | Path, target: str | Path) -> bool:
    """True iff *target* resolves to a path inside *root* (no traversal / symlink escape).

    Both paths are fully resolved before comparison, so ``../`` segments and symlinks that would
    escape the root are rejected. The root itself counts as within.
    """
    resolved_root = Path(root).resolve()
    resolved_target = Path(target).resolve()
    return resolved_target == resolved_root or resolved_root in resolved_target.parents
