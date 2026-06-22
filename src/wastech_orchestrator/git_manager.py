"""Git Manager (.agents/rules/git-workflow.md).

The **only** component that commits, pushes, or opens pull requests — agents never do. Every git
and ``gh`` invocation goes through the P2 safe process runner as an **argv list** (no shell string,
no user-string interpolation), with an allowlisted environment.

Responsibilities:

* branch flow: ``fetch`` → checkout ``base_branch`` → ``pull`` → create ``agent/<task-id>-<slug>``;
* **scoped staging**: stage only the agent's code paths via an explicit pathspec plus a
  belt-and-braces ``:(exclude)tasks/`` guard — **never** ``git add .``;
* the canonical footprint: the orchestrator's runtime files live under the gitignored
  ``<repo>/.worc/`` home, and a separate orchestrator-made audit commit captures the task file plus
  its ``<id>.summary.md`` at the repo root under ``tasks/``;
* idempotent commit/push/PR via an operation fingerprint + remote-state check;
* terminal cleanup back to ``repo.base_branch`` when provably safe;
* the :class:`~wastech_orchestrator.routing.snapshots.SnapshotHook` for partial-change capture.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.config.schema import (
    AuditBranch,
    MergeStrategy,
    OrchestratorConfig,
)
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.observability.progress import run_with_heartbeat
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.process import ProcessResult, run_process
from wastech_orchestrator.providers.redaction import read_denied_secrets, redact_text
from wastech_orchestrator.routing.snapshots import PartialChange, WorkingTreeSnapshot
from wastech_orchestrator.security.env import build_child_env
from wastech_orchestrator.state_store import PublishOpRow, StateStore

# Git/gh operations are bounded but slower than a trivial command (network fetch/push allowed).
GIT_TIMEOUT_SECONDS = 300

# The directories that must never enter a code commit. `.worc/` is the gitignored runtime
# home (state.db, logs/, workspace/, checks/, config.yaml, orchestrator.pid, …); `tasks/` is tracked
# but rides the separate audit commit, so it is kept out of the code commit too.
EXCLUDED_DIRS = (".worc", "tasks")

# The single ignore line `install` appends to a target repo's tracked `.gitignore`: the
# whole `.worc/` runtime home, so an operator's `git status` stays clean. `tasks/` is intentionally
# NOT ignored — it carries the committed audit trail.
RUNTIME_GITIGNORE_LINES: tuple[str, ...] = (
    "# wastech-orchestrator runtime home (auto-appended by `worc install`)",
    ".worc/",
)


def _append_missing_lines(target: Path, lines: Sequence[str]) -> list[str]:
    """Idempotently append the ``lines`` not already present in ``target`` (one entry per line).

    Creates parent dirs and the file as needed, preserves existing content, and separates the
    appended block with a blank line when the file does not already end on one. Returns the lines
    actually appended (empty when all were present).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    present = {line.strip() for line in existing}
    additions = [line for line in lines if line.strip() not in present]
    if additions:
        with target.open("a", encoding="utf-8") as fh:
            if existing and existing[-1].strip():
                fh.write("\n")
            fh.write("\n".join(additions) + "\n")
    return additions


def append_runtime_excludes(repo_root: str | Path) -> list[str]:
    """Idempotently add the ``.worc/`` ignore line to the repo's tracked ``.gitignore``.

    Returns the lines actually appended — empty when everything was already present.
    """
    return _append_missing_lines(Path(repo_root) / ".gitignore", RUNTIME_GITIGNORE_LINES)


# publish_operations.kind values (idempotency keys).
KIND_CODE_COMMIT = "code_commit"
KIND_SUBTASK_COMMIT = "subtask_commit"
KIND_AUDIT_COMMIT = "audit_commit"
KIND_PUSH = "push"
KIND_PR = "pr"
KIND_PR_MERGE = "pr_merge"

# Substrings in a (redacted) ``gh pr merge`` failure that mean the PR is already merged/closed — an
# idempotent success (a crash dropped the op row after a real merge, or a human merged out of band),
# never a re-merge. Conflict/branch-protection failures deliberately do NOT match.
_ALREADY_MERGED_MARKERS = ("already merged", "already been merged", "not open", "was merged")

_STATUS_STARTED = "started"
_STATUS_COMPLETED = "completed"
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitResult:
    """Raw outcome of one git/gh invocation."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    launch_error: str | None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.launch_error is None


@dataclass(frozen=True)
class CleanupOutcome:
    """The terminal-cleanup decision."""

    safe: bool
    target_branch: str
    error: str | None = None


@dataclass(frozen=True)
class ChangedPath:
    """One repository path changed against HEAD, normalized from Git's name-status output."""

    status: str
    path: str
    previous_path: str | None = None


class GitCommandError(Exception):
    """A git/gh command exited non-zero (or failed to launch) where success was required."""


class ManualActionRequired(Exception):
    """A condition the Core must surface as ``manual_action_required`` (e.g. preflight)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


CommandRunner = Callable[[Sequence[str]], GitResult]


@dataclass
class _ActiveTask:
    task_id: str
    slug: str
    branch: str
    partial_counter: int = 0


class GitManager:
    """Drives all git/gh operations for the target clone. Also satisfies ``SnapshotHook``."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        store: StateStore,
        artifacts_root: str | Path,
        gh_runner: CommandRunner | None = None,
        run_process: Callable[..., ProcessResult] = run_process,
        heartbeat_seconds: float = 30.0,
    ) -> None:
        self._config = config
        self._store = store
        self._artifacts_root = artifacts_root
        self._clone = config.repo.local_path
        self._env = build_child_env(config.security.allowed_environment)
        self._run_process = run_process
        self._gh_runner = gh_runner
        self._heartbeat_seconds = heartbeat_seconds
        self._active: _ActiveTask | None = None

    # --- low-level command execution ------------------------------------------------------

    def _run(self, argv: Sequence[str]) -> GitResult:
        """Run an argv list in the clone via the safe process runner; capture stdout + stderr."""
        with tempfile.TemporaryDirectory() as scratch:
            stdout_path = Path(scratch) / "stdout"
            context: dict[str, object] = {"component": argv[0] if argv else "process"}
            if self._active is not None:
                context["task_id"] = self._active.task_id
            log = bind(_LOG, **context)
            operation = argv[1] if len(argv) > 1 else "launch"
            result = run_with_heartbeat(
                lambda: self._run_process(
                    list(argv),
                    cwd=self._clone,
                    env=self._env,
                    timeout_seconds=GIT_TIMEOUT_SECONDS,
                    stdout_path=str(stdout_path),
                ),
                logger=log,
                message="git operation heartbeat",
                interval_seconds=self._heartbeat_seconds,
                fields={"operation": operation, "timeout_seconds": GIT_TIMEOUT_SECONDS},
            )
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        return GitResult(
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=redact_text(result.stderr_text or ""),
            timed_out=result.timed_out,
            launch_error=result.launch_error,
        )

    def _git(self, *args: str) -> GitResult:
        return self._run(["git", *args])

    def _git_checked(self, *args: str) -> str:
        result = self._git(*args)
        if not result.ok:
            raise GitCommandError(
                f"git {' '.join(args)} failed (exit={result.exit_code}): {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def _gh(self, args: Sequence[str]) -> GitResult:
        """Run GitHub CLI arguments, adding the ``gh`` executable exactly once."""
        if self._gh_runner is not None:
            return self._gh_runner(args)
        return self._run(["gh", *args])

    # --- branch flow ----------------------------------------------------------------------

    def branch_name(self, task_id: str, slug: str) -> str:
        return f"{self._config.repo.branch_prefix}/{task_id}-{slug}"

    def prepare_branch(self, task_id: str, slug: str) -> str:
        """Fetch, sync ``base_branch``, and create (or reuse) the task branch. Returns its name."""
        base = self._config.repo.base_branch
        branch = self.branch_name(task_id, slug)
        self._active = _ActiveTask(task_id=task_id, slug=slug, branch=branch)

        # Fetch is best-effort: a repo without a remote (some tests) still proceeds locally.
        self._git("fetch", "origin")
        self._git_checked("checkout", base)
        self._git("pull", "--ff-only")

        if self._branch_exists(branch):
            self._git_checked("checkout", branch)  # reuse on restart, never recreate
        else:
            self._git_checked("checkout", "-b", branch)
        return branch

    def reset_branch_to_base(
        self, task_id: str, slug: str, *, force_reset_remote: bool = False
    ) -> str:
        """Delete the stale task branch so a fresh ``rerun`` rebuilds it from the current base.

        The complement of ``prepare_branch``'s deliberate *reuse*: a failed attempt's commits would
        otherwise be reused (and stack on a base that moved on). The caller has already verified the
        tree is clean (fail-closed), so ``checkout base`` is safe; deleting the branch while on base
        lets the subsequent ``prepare_branch`` take its ``checkout -b`` arm from current base. Force
        (``-D``) because a failed attempt's commits are unmerged. Idempotent: a missing branch is a
        no-op, so re-running ``rerun`` after an interruption is safe. Returns the branch name.
        """
        base = self._config.repo.base_branch
        branch = self.branch_name(task_id, slug)
        self._git("fetch", "origin")
        self._git_checked("checkout", base)
        self._git("pull", "--ff-only")
        if force_reset_remote and self._remote_branch_exists(branch):
            # Best-effort: deleting the remote branch makes GitHub auto-close any open PR on it.
            self._git("push", "origin", "--delete", branch)
        self.delete_branch(branch)
        return branch

    def delete_branch(self, branch: str) -> bool:
        """Force-delete a local branch if it exists (idempotent). Returns whether it deleted.

        Used by ``finalize`` to tidy the now-unneeded agent branch (opt-in) and by
        ``reset_branch_to_base`` for the ``rerun`` reset. ``-D`` because a terminal task's commits
        may be unmerged; the caller must already be on another branch (``checkout base`` first).
        """
        if not self._branch_exists(branch):
            return False
        self._git_checked("branch", "-D", branch)
        return True

    def unaccounted_dirty_paths(self) -> set[str]:
        """Public read probe for the ``rerun`` fail-closed dirty-tree gate (no mutation)."""
        return self._unaccounted_dirty_paths()

    def remote_branch_exists(self, branch: str) -> bool:
        """Public read probe: does ``origin`` still carry this branch? (``rerun`` refuse-gate)."""
        return self._remote_branch_exists(branch)

    def recorded_pr_url(self, task_id: str) -> str | None:
        """The PR URL a prior attempt recorded (completed ``pr`` publish op), or ``None``."""
        existing = self._store.get_publish_op(task_id, KIND_PR, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref
        return None

    def verify_pr_state(self, pr_url: str) -> str | None:
        """Read-only PR state for ``finalize``'s merge check: ``MERGED``/``OPEN``/``CLOSED``/None.

        Runs `gh pr view <url> --json state` — strictly **read-only** (never creates/pushes/merges),
        so it does not weaken the security policy. Best-effort: returns ``None`` when ``gh`` is
        missing / unauthenticated / offline or the PR is gone, so the caller can skip the check.
        """
        result = self._gh(["pr", "view", pr_url, "--json", "state", "-q", ".state"])
        if not result.ok:
            return None
        return result.stdout.strip() or None

    def pr_merge_state(self, pr_url: str) -> tuple[str | None, str | None]:
        """Read-only ``(state, merge_commit_sha)`` for a PR: the dependency-readiness probe.

        Runs a single `gh pr view <url> --json state,mergeCommit` — strictly **read-only** (no
        ``--admin``, never creates/pushes/merges), so it does not weaken the security policy. The
        SHA is present only once the PR is ``MERGED`` (``None`` otherwise). Best-effort: returns
        ``(None, None)`` when ``gh`` is missing / unauthenticated / offline or the PR is gone, so
        the caller can treat an unconfirmable merge as "not yet" and skip.
        """
        result = self._gh(["pr", "view", pr_url, "--json", "state,mergeCommit"])
        if not result.ok:
            return None, None
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            return None, None
        state = data.get("state") or None
        merge_commit = data.get("mergeCommit") or {}
        sha = (merge_commit.get("oid") if isinstance(merge_commit, dict) else None) or None
        return state, sha

    def backfill_merge_sha(self, task_id: str, sha: str) -> None:
        """Replace an armed ``pr_merge`` outcome with the real merge SHA, once observed merged.

        Closes the "armed PR never records its real SHA" gap for any task that has a dependent: the
        readiness probe (:meth:`pr_merge_state`) observed ``MERGED`` and hands the merge oid here.
        Updates **only** the SQLite ``pr_merge`` publish op (the authoritative merge-outcome store);
        the append-only ledger keeps its point-in-time ``"armed"`` record untouched. Idempotent: a
        no-op when there is no recorded merge op or its ``result_ref`` is already ``sha``.
        """
        if not sha:
            return
        existing = self._store.get_publish_op(task_id, KIND_PR_MERGE, None)
        if existing is None or existing.status != _STATUS_COMPLETED or existing.result_ref == sha:
            return
        self._record_completed(task_id, KIND_PR_MERGE, existing.fingerprint, sha)

    def refresh_base(self) -> None:
        """Best-effort fetch + ff-only pull of ``base_branch`` so git-pushed tasks become visible.

        Periodic discovery for the ``watch`` loop. A no-op unless HEAD is already on
        ``base_branch`` (i.e. the slot is free after terminal cleanup), so it never disturbs an
        active task branch. Both git calls are best-effort: a repo without a remote or a
        non-fast-forwardable base simply leaves the working copy untouched.
        """
        base = self._config.repo.base_branch
        current = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if current != base:
            return
        self._git("fetch", "origin")
        self._git("pull", "--ff-only")

    def _branch_exists(self, branch: str) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").ok

    def commit_on_branch(self, sha: str, branch: str) -> bool:
        """True iff ``sha`` is an ancestor of ``branch`` (recovery subtask verification)."""
        if not sha:
            return False
        return self._git("merge-base", "--is-ancestor", sha, branch).ok

    # --- footprint ------------------------------------------------------------------------

    def ensure_runtime_excludes(self) -> None:
        """Ensure the repo's tracked ``.gitignore`` ignores the ``.worc/`` runtime home.

        ``install`` normally writes this, but a clone scaffolded another way may lack it.
        Idempotent; keeps ``.worc/`` (state.db, logs/, workspace/, checks/, config.yaml, …) out of
        the operator's ``git status``. ``tasks/`` stays trackable — it carries the audit trail.
        """
        append_runtime_excludes(self._clone)

    # --- SnapshotHook --------------------------------------------------------------

    def capture(self) -> WorkingTreeSnapshot:
        commit_sha = self._git("rev-parse", "HEAD").stdout.strip()
        porcelain = self._git("status", "--porcelain").stdout
        diff = self._git("diff", "HEAD").stdout
        checksum = hashlib.sha256((porcelain + "\n" + diff).encode("utf-8")).hexdigest()
        return WorkingTreeSnapshot(
            commit_sha=commit_sha, porcelain_status=porcelain, diff_checksum=checksum, artifacts=()
        )

    def partial_change_since(self, before: WorkingTreeSnapshot) -> PartialChange | None:
        after = self.capture()
        if after.diff_checksum == before.diff_checksum:
            return None
        diff_text = self._git("diff", "HEAD").stdout
        diff_path = self._write_partial_diff(diff_text)
        return PartialChange(
            before=before,
            after=after,
            diff_path=diff_path,
            note="partial attempt from a prior provider; build on it rather than restart",
        )

    def _write_partial_diff(self, diff_text: str) -> str:
        if self._active is not None:
            self._active.partial_counter += 1
            base = task_artifact_dir(self._artifacts_root, self._active.task_id) / "partial"
            name = f"{self._active.partial_counter:03d}.diff"
        else:
            base = Path(self._artifacts_root) / "logs" / "_partial"
            name = "partial.diff"
        base.mkdir(parents=True, exist_ok=True)
        path = base / name
        path.write_text(diff_text, encoding="utf-8")
        return str(path)

    # --- staging + commit ---------------------------------------------------------

    def changed_code_paths(self) -> list[str]:
        """The changed paths that are *not* orchestration artifacts (the code staging set)."""
        porcelain = self._git("status", "--porcelain").stdout
        paths: list[str] = []
        for line in porcelain.splitlines():
            if not line.strip():
                continue
            path = line[3:].strip()
            if " -> " in path:  # a rename: take the destination
                path = path.split(" -> ", 1)[1]
            path = path.strip('"')
            if self._is_artifact_path(path):
                continue
            paths.append(path)
        return paths

    def changed_code_entries(self) -> tuple[ChangedPath, ...]:
        """Return tracked and untracked code changes for deterministic output guardrails."""
        entries: list[ChangedPath] = []
        tracked = self._git("diff", "--name-status", "HEAD", "--").stdout
        for line in tracked.splitlines():
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            status = fields[0]
            if status.startswith(("R", "C")) and len(fields) >= 3:
                previous = fields[1]
                path = fields[2]
            else:
                previous = None
                path = fields[1]
            if self._is_artifact_path(path):
                continue
            entries.append(ChangedPath(status=status, path=path, previous_path=previous))

        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z").stdout
        for path in (item for item in untracked.split("\0") if item):
            if not self._is_artifact_path(path):
                entries.append(ChangedPath(status="??", path=path))
        return tuple(entries)

    def _is_artifact_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/")
        return any(normalized == d or normalized.startswith(f"{d}/") for d in EXCLUDED_DIRS)

    def staged_pathspec(self, paths: Sequence[str]) -> list[str]:
        """Build the scoped ``git add`` pathspec: code paths plus a belt-and-braces guard.

        ``.worc/`` is gitignored, so ``git add`` skips it without a guard; ``tasks/`` is tracked (it
        rides the separate audit commit), so it is guarded with ``:(exclude)`` to ensure it never
        slips into the *code* commit.
        """
        return [*paths, ":(exclude)tasks/"]

    def commit_code(self, task_id: str, message: str) -> str | None:
        """Stage the agent's code paths and make one commit. Idempotent. Returns the commit SHA.

        Returns the current HEAD when there is nothing to commit (e.g. a decomposed task whose code
        was already committed per subtask).
        """
        paths = self.changed_code_paths()
        if not paths:
            return self._git("rev-parse", "HEAD").stdout.strip() or None
        return self._commit(task_id, KIND_CODE_COMMIT, None, message, paths)

    def commit_subtask(self, task_id: str, order: int, slug: str, message: str) -> str:
        """Make the single local commit for a completed subtask on the task branch."""
        paths = self.changed_code_paths()
        sha = self._commit(task_id, KIND_SUBTASK_COMMIT, order, message, paths)
        if sha is None:  # nothing changed — fall back to HEAD so the marker is always set
            sha = self._git("rev-parse", "HEAD").stdout.strip()
        return sha

    def _commit(
        self,
        task_id: str,
        kind: str,
        subtask: int | None,
        message: str,
        paths: Sequence[str],
    ) -> str | None:
        existing = self._store.get_publish_op(task_id, kind, subtask)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref  # already committed (restart) — never double-commit

        head_before = self._git("rev-parse", "HEAD").stdout.strip()
        fingerprint = self._fingerprint(task_id, kind, subtask, head_before, paths)
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id,
                kind=kind,
                subtask_order=subtask,
                fingerprint=fingerprint,
                status=_STATUS_STARTED,
            )
        )
        if not paths:
            return None
        self._git_checked("add", "--", *self.staged_pathspec(paths))
        self._git_checked("commit", "-m", message)
        sha = self._git_checked("rev-parse", "HEAD")
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id,
                kind=kind,
                subtask_order=subtask,
                fingerprint=fingerprint,
                status=_STATUS_COMPLETED,
                result_ref=sha,
            )
        )
        return sha

    def commit_audit(self, task_id: str) -> str | None:
        """Make the orchestrator-only commit of the task lifecycle.

        Stages **only this task's** moved task file plus its `<id>.summary.md` (in ``tasks/done`` or
        ``tasks/failed``) — never the whole ``tasks/`` tree, so a concurrently-pending task is never
        swept into this commit. Working artifacts (plan, review, stage logs, diffs, summary.json)
        live under the gitignored ``.worc/`` home and are never committed. The code change rides in
        the separate scoped code commit, so this never touches code paths.
        """
        footprint = self._config.git.footprint
        existing = self._store.get_publish_op(task_id, KIND_AUDIT_COMMIT, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref

        code_branch = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        audit_branch = code_branch
        if footprint.audit_on_branch is AuditBranch.SIBLING:
            audit_branch = f"{code_branch}-audit"
            if not self._branch_exists(audit_branch):
                self._git_checked("checkout", "-b", audit_branch)
            else:
                self._git_checked("checkout", audit_branch)

        message = footprint.audit_commit_message.format(task_id=task_id)
        audit_files = [
            f"tasks/{state}/{task_id}{suffix}"
            for state in ("done", "failed")
            for suffix in (".md", ".summary.md")
        ]
        present = [rel for rel in audit_files if (Path(self._clone) / rel).exists()]
        sha: str | None = None
        if present and self._git("add", "--", *present).ok:
            commit = self._git("commit", "-m", message)
            if commit.ok:
                sha = self._git_checked("rev-parse", "HEAD")

        if footprint.audit_on_branch is AuditBranch.SIBLING:
            self._git_checked("checkout", code_branch)

        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id,
                kind=KIND_AUDIT_COMMIT,
                fingerprint=sha or "noop",
                status=_STATUS_COMPLETED,
                result_ref=sha,
            )
        )
        return sha

    # --- publish (idempotent) --------------------------------------------------------

    def push(self, task_id: str, branch: str) -> bool:
        """Push the task branch to ``origin``. Idempotent via the publish op + remote check.

        Refuses to push directly to ``base_branch``: publishing is PR-only, and the task
        branch is always ``agent/<task-id>-<slug>``, so a push targeting the base branch signals a
        corrupted branch state rather than a normal publish.
        """
        base = self._config.repo.base_branch
        if branch == base:
            raise GitCommandError(
                f"refusing to push directly to base branch {base!r}; publishing is PR-only"
            )
        existing = self._store.get_publish_op(task_id, KIND_PUSH, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return True
        if self._remote_branch_exists(branch):
            self._record_completed(task_id, KIND_PUSH, branch, branch)
            return True
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id, kind=KIND_PUSH, fingerprint=branch, status=_STATUS_STARTED
            )
        )
        self._git_checked("push", "--set-upstream", "origin", branch)
        self._record_completed(task_id, KIND_PUSH, branch, branch)
        return True

    def _remote_branch_exists(self, branch: str) -> bool:
        result = self._git("ls-remote", "--heads", "origin", branch)
        return result.ok and bool(result.stdout.strip())

    def create_pr(self, task_id: str, branch: str, *, title: str, body_path: str) -> str | None:
        """Open a PR with ``summary.md`` as the body. Idempotent. None when PRs are disabled."""
        if not self._config.git.create_pull_request:
            return None
        existing = self._store.get_publish_op(task_id, KIND_PR, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref

        self._store.record_publish_op(
            PublishOpRow(task_id=task_id, kind=KIND_PR, fingerprint=branch, status=_STATUS_STARTED)
        )
        result = self._gh(
            [
                "pr",
                "create",
                "--base",
                self._config.git.pr_base,
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                body_path,
            ]
        )
        if not result.ok:
            raise GitCommandError(f"gh pr create failed: {result.stderr.strip()}")
        pr_url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        self._record_completed(task_id, KIND_PR, branch, pr_url)
        return pr_url

    def merge_pr(
        self, task_id: str, pr_url: str, *, strategy: MergeStrategy, wait_for_checks: bool
    ) -> str | None:
        """Merge an open PR via ``gh pr merge``. Idempotent via the publish op.

        Returns a merge-outcome marker: the merge commit SHA (immediate mode), ``"merged"`` when the
        SHA is unreadable, or ``"armed"`` when GitHub-native auto-merge was armed (``--auto``);
        ``None`` when there is no PR. Reached only when ``git.auto_merge`` resolves true.

        DANGER: this bypasses the human review gate. It never weakens safety — **no** ``--admin``
        (branch protection is respected), no force-push, exactly one attempt (no retry). A blocked
        merge raises :class:`GitCommandError`; the Core surfaces that as ``manual_action_required``
        and leaves the PR open for a human to merge.
        """
        if not pr_url:
            return None
        existing = self._store.get_publish_op(task_id, KIND_PR_MERGE, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id, kind=KIND_PR_MERGE, fingerprint=pr_url, status=_STATUS_STARTED
            )
        )
        # Fixed argv (no shell, no interpolation); strategy comes from the validated MergeStrategy
        # enum. ``--admin`` is never emitted, so a protected branch's checks remain the real gate.
        args = ["pr", "merge", pr_url, f"--{strategy.value}"]
        if wait_for_checks:
            args.append("--auto")
        result = self._gh(args)
        if not result.ok:
            haystack = f"{result.stderr}\n{result.stdout}".lower()
            if any(marker in haystack for marker in _ALREADY_MERGED_MARKERS):
                self._record_completed(task_id, KIND_PR_MERGE, pr_url, "merged")
                return "merged"
            # ``result.stderr`` is already redacted by ``_run`` — never surface raw process output.
            raise GitCommandError(f"gh pr merge failed: {result.stderr.strip()}")
        outcome = "armed" if wait_for_checks else (self._merge_commit_sha(pr_url) or "merged")
        self._record_completed(task_id, KIND_PR_MERGE, pr_url, outcome)
        return outcome

    def _merge_commit_sha(self, pr_url: str) -> str | None:
        """Best-effort merge commit SHA after an immediate merge; ``None`` when unavailable."""
        result = self._gh(["pr", "view", pr_url, "--json", "mergeCommit", "-q", ".mergeCommit.oid"])
        if not result.ok:
            return None
        return result.stdout.strip() or None

    def _record_completed(self, task_id: str, kind: str, fingerprint: str, result_ref: str) -> None:
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id,
                kind=kind,
                fingerprint=fingerprint,
                status=_STATUS_COMPLETED,
                result_ref=result_ref,
            )
        )

    # --- diffs ----------------------------------------------------------------------------

    def write_current_diff(self, task_id: str) -> str:
        """Write ``logs/<task-id>/current.diff`` (working tree vs HEAD) and return its path.

        The diff is redacted before writing: the failure report reads it back, so this is
        the single place that keeps a leaked secret out of both ``current.diff`` and the report.
        """
        diff = self._git("diff", "HEAD").stdout
        task_dir = task_artifact_dir(self._artifacts_root, task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / "current.diff"
        path.write_text(redact_text(diff, extra_secrets=self._diff_secrets()), encoding="utf-8")
        return str(path)

    def _diff_secrets(self) -> tuple[str, ...]:
        """Denied-file secret values present in the clone, to redact from written diffs."""
        return read_denied_secrets(self._clone, self._config.security.denied_read_paths)

    def cumulative_committed_diff(self) -> str:
        """The diff of all task-branch commits vs ``base_branch`` (decomposed context)."""
        base = self._config.repo.base_branch
        result = self._git("diff", f"{base}...HEAD")
        return result.stdout

    def diff_stat(self) -> str:
        """``git diff --stat base...HEAD`` — changed files + line counts only, no patch body.

        Used by the deterministic minimal summary so the committed ``summary.md`` stays
        compact. ``--stat`` carries only file paths and counts (never patch content), so unlike
        :meth:`cumulative_committed_diff` there is nothing secret to redact.
        """
        base = self._config.repo.base_branch
        return self._git("diff", "--stat", f"{base}...HEAD").stdout

    # --- terminal cleanup ----------------------------------------------------------

    def terminal_cleanup(self, task_id: str) -> CleanupOutcome:
        """Safely checkout ``base_branch`` after a terminal outcome, or report why it is unsafe."""
        base = self._config.repo.base_branch
        dirty = self._unaccounted_dirty_paths()
        if dirty:
            outcome = CleanupOutcome(
                safe=False,
                target_branch=base,
                error=f"working tree has unaccounted changes: {', '.join(sorted(dirty))}",
            )
            self._write_cleanup_artifact(task_id, outcome, completed=False)
            return outcome

        checkout = self._git("checkout", base)
        if not checkout.ok:
            outcome = CleanupOutcome(
                safe=False, target_branch=base, error=f"checkout failed: {checkout.stderr.strip()}"
            )
            self._write_cleanup_artifact(task_id, outcome, completed=False)
            return outcome

        outcome = CleanupOutcome(safe=True, target_branch=base)
        self._write_cleanup_artifact(task_id, outcome, completed=True)
        self._active = None
        return outcome

    def _unaccounted_dirty_paths(self) -> set[str]:
        """Tracked, uncommitted changes (artifact dirs are expected and so are ignored)."""
        porcelain = self._git("status", "--porcelain").stdout
        dirty: set[str] = set()
        for line in porcelain.splitlines():
            if not line.strip():
                continue
            code = line[:2]
            path = line[3:].strip().strip('"')
            if "->" in path:
                path = path.split("->", 1)[1].strip()
            if self._is_artifact_path(path):
                continue
            # An untracked non-artifact file (``??``) is unexpected; any tracked change is dirty.
            if code == "??" or code.strip():
                dirty.add(path)
        return dirty

    def _write_cleanup_artifact(
        self, task_id: str, outcome: CleanupOutcome, *, completed: bool
    ) -> str:
        publish_dir = task_artifact_dir(self._artifacts_root, task_id) / "publish"
        publish_dir.mkdir(parents=True, exist_ok=True)
        path = publish_dir / "terminal-cleanup.json"
        path.write_text(
            json.dumps(
                {
                    "target_branch": outcome.target_branch,
                    "completed": completed,
                    "safe": outcome.safe,
                    "error": outcome.error,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return str(path)

    def _fingerprint(
        self, task_id: str, kind: str, subtask: int | None, head: str, paths: Sequence[str]
    ) -> str:
        material = "|".join([task_id, kind, str(subtask), head, ",".join(sorted(paths))])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
