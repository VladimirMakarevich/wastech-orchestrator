"""Resolved output policy — where a flow's writing nodes may write, and what they must produce.

The scalar :class:`~wastech_orchestrator.core.flow.contracts.OutputPolicy` declared on a flow
resolves here into a foundation :class:`ResolvedOutputPolicy`: the single repo-relative directory
the flow's writing nodes may write into, the files the flow must produce there, and whether the
deliverable is *private* (must never enter git). The same resolution is the seam shared by the
``citation`` checker (which reads ``sources.json`` from the report directory) and the after-stage
write guard + the publish node enforce containment / privacy against it).

Pure: no IO, no git, no provider knowledge — only the policy → (path, required files, privacy)
mapping plus a path-containment predicate. The engine carries no domain knowledge of *which* flow
uses *which* policy; a flow selects a policy by name and the core resolves it the same way for all.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.core.flow.contracts import OutputPolicy
from wastech_orchestrator.runtime_layout import PRIVATE_HOME_DIRNAME

#: Default report directory layout (repo-relative), used when the flow declares no ``report_dir``.
#: ``repository_document`` deliverables are committable documents under ``docs/research/``;
#: ``private_control_workspace_report`` deliverables live under the gitignored private ``.worc/``
#: home (``PRIVATE_HOME_DIRNAME``) and never enter a commit. A flow may name another base for
#: either policy; for the private one the "a private report never enters git" invariant is then
#: carried at run time, in two places that together cover every terminal: the publish node refuses
#: a report with any git-trackable file (so a forgotten ignore rule ends the task at
#: ``manual_action_required``), and the resolved private directory is dropped from the code
#: commit's staging set (``GitManager.set_private_report_dir``), so a terminal that never reaches
#: publish cannot commit it either.
_RESEARCH_DIR = "docs/research"
_PRIVATE_REPORT_DIR = f"{PRIVATE_HOME_DIRNAME}/security-reports"


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

    @property
    def report_base(self) -> str | None:
        """The report directory's **base** — ``report_subdir`` without the per-task segment.

        The engine always appends ``/<task_id>`` to the base a flow named, so the base is that
        segment removed again. It is what the tree-sweeping merge commit has to keep out of its
        staging set: ``git add -A`` cannot tell this task's private report from a sibling task's
        leftover beside it under the same base. ``None`` for ``code_change``.
        """
        if self.report_subdir is None:
            return None
        return self.report_subdir.rsplit("/", 1)[0]

    def report_dir(self, repo_dir: str | Path) -> Path | None:
        """The absolute report directory under *repo_dir*, or ``None`` for ``code_change``."""
        if self.report_subdir is None:
            return None
        return Path(repo_dir) / self.report_subdir


def resolve_output_policy(
    policy: OutputPolicy, task_id: str, report_dir: str | None = None
) -> ResolvedOutputPolicy:
    """Resolve a scalar ``output_policy`` for a task into its foundation form.

    The per-task subdirectory keeps concurrent/serial tasks from colliding and makes the report
    self-identifying. ``task_id`` is the normalized id (``[a-z0-9][a-z0-9._-]*``), so it is a
    safe single path segment — never an absolute path or a traversal.

    ``report_dir`` is the flow's optional ``report_dir`` base (repo-relative POSIX, no task
    segment): it replaces the policy's built-in home for **both** report policies and changes
    nothing else — the required files and the privacy flag are properties of the policy, not of
    where it writes. ``code_change`` has no report directory and ignores it. The base arrives
    already validated (the flow validator refuses an absolute, traversing, non-portable or
    reserved-root value at load), so nothing is normalized here: this function stays a pure
    mapping, and a caller that hand-builds a :class:`~..schema.FlowDoc` gets exactly what it
    declared.
    """
    if policy is OutputPolicy.REPOSITORY_DOCUMENT:
        return ResolvedOutputPolicy(
            policy=policy,
            report_subdir=f"{report_dir or _RESEARCH_DIR}/{task_id}",
            required_files=("report.md", "sources.json"),
            private=False,
        )
    if policy is OutputPolicy.PRIVATE_CONTROL_WORKSPACE_REPORT:
        return ResolvedOutputPolicy(
            policy=policy,
            report_subdir=f"{report_dir or _PRIVATE_REPORT_DIR}/{task_id}",
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
