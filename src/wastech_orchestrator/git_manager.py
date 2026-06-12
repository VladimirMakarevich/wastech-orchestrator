"""Git Manager (spec §21, docs/rules/git-workflow.md).

The **only** component that commits, pushes, or opens pull requests — agents never do. Every git
and ``gh`` invocation goes through the P2 safe process runner as an **argv list** (no shell string,
no user-string interpolation), with an allowlisted environment.

Responsibilities:

* branch flow: ``fetch`` → checkout ``base_branch`` → ``pull`` → create ``agent/<task-id>-<slug>``;
* **scoped staging** (§21.1): stage only the agent's code paths via an explicit pathspec plus
  belt-and-braces ``:(exclude)tasks/`` / ``logs/`` / ``workspace/`` — **never** ``git add .``;
* the three footprint modes (§21): ``external`` (zero footprint), ``in_repo``+``exclude_local``
  (idempotent ``.git/info/exclude`` append), ``in_repo``+``commit`` (a separate orchestrator-made
  audit commit);
* idempotent commit/push/PR via an operation fingerprint + remote-state check (§13);
* terminal cleanup back to ``repo.base_branch`` when provably safe (§8.3);
* the :class:`~wastech_orchestrator.routing.snapshots.SnapshotHook` for partial-change capture.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.config.schema import (
    AuditBranch,
    FootprintTracking,
    OrchestratorConfig,
)
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.process import ProcessResult, run_process
from wastech_orchestrator.providers.redaction import read_denied_secrets, redact_text
from wastech_orchestrator.routing.snapshots import PartialChange, WorkingTreeSnapshot
from wastech_orchestrator.security.env import build_child_env
from wastech_orchestrator.state_store import PublishOpRow, StateStore

# Git/gh operations are bounded but slower than a trivial command (network fetch/push allowed).
GIT_TIMEOUT_SECONDS = 300

# The three artifact directories that must never enter a code commit (§21.1).
EXCLUDED_DIRS = ("tasks", "logs", "workspace")
_EXCLUDE_PATHSPECS = [f":(exclude){d}/" for d in EXCLUDED_DIRS]

# publish_operations.kind values (idempotency keys, §13).
KIND_CODE_COMMIT = "code_commit"
KIND_SUBTASK_COMMIT = "subtask_commit"
KIND_AUDIT_COMMIT = "audit_commit"
KIND_PUSH = "push"
KIND_PR = "pr"

_STATUS_STARTED = "started"
_STATUS_COMPLETED = "completed"


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
class PublishResult:
    """The outcome of the publish sequence for a task."""

    commit_sha: str | None
    pushed: bool
    pr_url: str | None


@dataclass(frozen=True)
class CleanupOutcome:
    """The terminal-cleanup decision (§8.3)."""

    safe: bool
    target_branch: str
    error: str | None = None


class GitCommandError(Exception):
    """A git/gh command exited non-zero (or failed to launch) where success was required."""


class ManualActionRequired(Exception):
    """A condition the Core must surface as ``manual_action_required`` (e.g. §21.4 preflight)."""

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
    ) -> None:
        self._config = config
        self._store = store
        self._artifacts_root = artifacts_root
        self._clone = config.repo.local_path
        self._env = build_child_env(config.security.allowed_environment)
        self._run_process = run_process
        self._gh_runner = gh_runner
        self._active: _ActiveTask | None = None

    # --- low-level command execution ------------------------------------------------------

    def _run(self, argv: Sequence[str]) -> GitResult:
        """Run an argv list in the clone via the safe process runner; capture stdout + stderr."""
        with tempfile.TemporaryDirectory() as scratch:
            stdout_path = Path(scratch) / "stdout"
            result = self._run_process(
                list(argv),
                cwd=self._clone,
                env=self._env,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
                stdout_path=str(stdout_path),
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

    def _gh(self, argv: Sequence[str]) -> GitResult:
        if self._gh_runner is not None:
            return self._gh_runner(argv)
        return self._run(["gh", *argv])

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

    def _branch_exists(self, branch: str) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").ok

    def commit_on_branch(self, sha: str, branch: str) -> bool:
        """True iff ``sha`` is an ancestor of ``branch`` (recovery subtask verification, §13)."""
        if not sha:
            return False
        return self._git("merge-base", "--is-ancestor", sha, branch).ok

    # --- footprint ------------------------------------------------------------------------

    def preflight_footprint(self) -> None:
        """If the repo already tracks a ``tasks/``/``logs/`` path, require manual action (§21.4)."""
        tracked = self._git("ls-files", "--", "tasks/", "logs/").stdout.strip()
        if tracked:
            raise ManualActionRequired(
                "target repo already tracks tasks/ or logs/ paths; "
                ".git/info/exclude cannot untrack them"
            )

    def ensure_exclude_local(self) -> None:
        """Idempotently append the artifact dirs to ``.git/info/exclude`` (§21.2)."""
        if self._config.git.footprint.tracking is not FootprintTracking.EXCLUDE_LOCAL:
            return
        exclude_path = Path(self._clone) / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            exclude_path.read_text(encoding="utf-8").splitlines() if exclude_path.exists() else []
        )
        present = {line.strip() for line in existing}
        additions = [f"{d}/" for d in EXCLUDED_DIRS if f"{d}/" not in present]
        if additions:
            with exclude_path.open("a", encoding="utf-8") as fh:
                if existing and existing[-1].strip():
                    fh.write("\n")
                fh.write("\n".join(additions) + "\n")

    # --- SnapshotHook (§7.4) --------------------------------------------------------------

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
            note="partial attempt from a prior provider; build on it rather than restart (§7.4)",
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

    # --- staging + commit (§21.1) ---------------------------------------------------------

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

    def _is_artifact_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/")
        return any(normalized == d or normalized.startswith(f"{d}/") for d in EXCLUDED_DIRS)

    def staged_pathspec(self, paths: Sequence[str]) -> list[str]:
        """Build the scoped ``git add`` pathspec: the code paths plus the exclude guards (§21.1)."""
        return [*paths, *_EXCLUDE_PATHSPECS]

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
        """Make the single local commit for a completed subtask on the task branch (§5.1)."""
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
            return existing.result_ref  # already committed (restart) — never double-commit (§13)

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
        """Make the orchestrator-only audit commit of the artifact dirs (tracking=commit, §21.3)."""
        footprint = self._config.git.footprint
        if footprint.tracking is not FootprintTracking.COMMIT:
            return None
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
        add = self._git("add", "--", "tasks/", "logs/")
        sha: str | None = None
        if add.ok:
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

    # --- publish (idempotent, §13) --------------------------------------------------------

    def push(self, task_id: str, branch: str) -> bool:
        """Push the task branch to ``origin``. Idempotent via the publish op + remote check.

        Refuses to push directly to ``base_branch`` (§12.12): publishing is PR-only, and the task
        branch is always ``agent/<task-id>-<slug>``, so a push targeting the base branch signals a
        corrupted branch state rather than a normal publish.
        """
        base = self._config.repo.base_branch
        if branch == base:
            raise GitCommandError(
                f"refusing to push directly to base branch {base!r}; publishing is PR-only (§12.12)"
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
                "gh",
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
        """Write ``logs/<task-id>/current.diff`` (working tree vs HEAD) and return its path (§6).

        The diff is redacted before writing (§12.6): the failure report reads it back, so this is
        the single place that keeps a leaked secret out of both ``current.diff`` and the report.
        """
        diff = self._git("diff", "HEAD").stdout
        task_dir = task_artifact_dir(self._artifacts_root, task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / "current.diff"
        path.write_text(redact_text(diff, extra_secrets=self._diff_secrets()), encoding="utf-8")
        return str(path)

    def _diff_secrets(self) -> tuple[str, ...]:
        """Denied-file secret values present in the clone, to redact from written diffs (§12.6)."""
        return read_denied_secrets(self._clone, self._config.security.denied_read_paths)

    def cumulative_committed_diff(self) -> str:
        """The diff of all task-branch commits vs ``base_branch`` (decomposed context, §6)."""
        base = self._config.repo.base_branch
        result = self._git("diff", f"{base}...HEAD")
        return result.stdout

    # --- terminal cleanup (§8.3) ----------------------------------------------------------

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
