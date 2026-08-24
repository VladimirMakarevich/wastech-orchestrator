"""Tests for the operator-driven merge primitives on the Git Manager (worc merge-task).

Exercises ``update_branch_with_base`` / ``merge_in_progress`` / ``merge_abort`` /
``commit_merge_resolution`` / ``push_branch_update`` / ``record_external_merge`` against a real
temporary git repo with a bare ``origin`` remote.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import (
    KIND_MERGE_COMMIT,
    KIND_PR_MERGE,
    GitCommandError,
    GitManager,
    GitResult,
    ManualActionRequired,
)
from wastech_orchestrator.state_store import StateStore, TaskRow

# Every test here is a slow integration test (real git / subprocess / process tree).
pytestmark = pytest.mark.slow

GitRunner = Callable[[Sequence[str], Path], str]
ConfigFactory = Callable[..., object]


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore.open(tmp_path / "state.db")


def _offline_gh(argv: Sequence[str]) -> GitResult:
    """Default ``gh`` for these tests: answers the fingerprint's PR probe, refuses anything else.

    The per-attempt fingerprint asks ``gh pr list`` whether the task branch has an open PR, so
    without a stub every capture would launch the real ``gh`` against whatever ``repo.url`` the
    test config names — a network call inside a unit test. Any other verb fails loudly so a test
    that actually needs ``gh`` wires its own runner instead of leaning on this one.
    """
    if list(argv[:2]) == ["pr", "list"]:
        return GitResult(exit_code=0, stdout="[]", stderr="", timed_out=False, launch_error=None)
    return GitResult(
        exit_code=1,
        stdout="",
        stderr="no gh runner wired in this test",
        timed_out=False,
        launch_error=None,
    )


def _manager(
    git_repo, store: StateStore, artifacts_root: Path, make_git_config: ConfigFactory
) -> GitManager:
    config = make_git_config(git_repo.clone)
    return GitManager(
        config, store=store, artifacts_root=str(artifacts_root), gh_runner=_offline_gh
    )


def _task(store: StateStore, task_id: str = "task-001") -> None:
    store.insert_task(TaskRow(task_id=task_id, title="t", status=Status.NEW))


def _branch_with_change(
    git_run: GitRunner, clone: Path, branch: str, path: str, content: str
) -> None:
    """Create ``branch`` off main with one committed change to ``path``, push it, return to main."""
    git_run(["checkout", "-b", branch, "main"], clone)
    (clone / path).write_text(content, encoding="utf-8")
    git_run(["add", path], clone)
    git_run(["commit", "-m", f"task change {path}"], clone)
    git_run(["push", "-u", "origin", branch], clone)
    git_run(["checkout", "main"], clone)


def _advance_base(git_run: GitRunner, clone: Path, path: str, content: str) -> None:
    """Commit a change to ``path`` on main and push it to ``origin/main`` (the base moves on)."""
    git_run(["checkout", "main"], clone)
    (clone / path).write_text(content, encoding="utf-8")
    git_run(["add", path], clone)
    git_run(["commit", "-m", f"base change {path}"], clone)
    git_run(["push", "origin", "main"], clone)


def test_update_branch_with_base_clean_stages_without_committing(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "feature.txt", "feature\n")
    _advance_base(git_run, git_repo.clone, "BASE.md", "base\n")  # different file → no conflict

    conflicted = gm.update_branch_with_base("worc/t1", "main")

    assert conflicted is False
    # `--no-commit` leaves a clean 3-way merge STAGED with MERGE_HEAD live (not
    # auto-committed), so the orchestrator finalizes it through the gated commit_merge_resolution.
    assert gm.merge_in_progress() is True
    head = git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone)
    assert head == "worc/t1"
    # Both changes are present in the (staged) working tree.
    assert (git_repo.clone / "BASE.md").read_text(encoding="utf-8") == "base\n"
    assert (git_repo.clone / "feature.txt").read_text(encoding="utf-8") == "feature\n"


def test_update_branch_with_base_conflict_leaves_markers(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "README.md", "branch side\n")
    _advance_base(git_run, git_repo.clone, "README.md", "base side\n")  # same file → conflict

    conflicted = gm.update_branch_with_base("worc/t1", "main")

    assert conflicted is True
    assert gm.merge_in_progress() is True
    assert "<<<<<<<" in (git_repo.clone / "README.md").read_text(encoding="utf-8")


def test_merge_abort_restores_tree(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "README.md", "branch side\n")
    _advance_base(git_run, git_repo.clone, "README.md", "base side\n")
    assert gm.update_branch_with_base("worc/t1", "main") is True

    gm.merge_abort()

    assert gm.merge_in_progress() is False
    # The working tree is restored to the branch's pre-merge content (no markers).
    assert (git_repo.clone / "README.md").read_text(encoding="utf-8") == "branch side\n"
    gm.merge_abort()  # idempotent: a second abort with no merge in flight is a no-op


def test_commit_merge_resolution_commits_and_is_idempotent(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _task(store)
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "README.md", "branch side\n")
    _advance_base(git_run, git_repo.clone, "README.md", "base side\n")
    assert gm.update_branch_with_base("worc/t1", "main") is True
    # Resolve the conflict the way the agent would (markers removed); the orchestrator commits.
    (git_repo.clone / "README.md").write_text("resolved\n", encoding="utf-8")

    sha = gm.commit_merge_resolution("task-001", "merge(task-001): resolve")

    assert sha
    assert gm.merge_in_progress() is False
    parents = git_run(["rev-list", "--parents", "-n", "1", "HEAD"], git_repo.clone).split()
    assert len(parents) == 3  # a merge commit: itself + two parents
    op = store.get_publish_op("task-001", KIND_MERGE_COMMIT, None)
    assert op is not None and op.status == "completed" and op.result_ref == sha
    # Idempotent: a re-run returns the same sha and makes no new commit.
    assert gm.commit_merge_resolution("task-001", "merge(task-001): resolve") == sha


def test_commit_merge_resolution_refuses_leftover_markers(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _task(store)
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "README.md", "branch side\n")
    _advance_base(git_run, git_repo.clone, "README.md", "base side\n")
    assert gm.update_branch_with_base("worc/t1", "main") is True
    # Do NOT resolve: the conflict markers are still in the tree.

    with pytest.raises(GitCommandError, match="conflict marker"):
        gm.commit_merge_resolution("task-001", "merge(task-001): resolve")


def test_push_branch_update_fast_forwards_remote(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "feature.txt", "feature\n")
    _advance_base(git_run, git_repo.clone, "BASE.md", "base\n")
    assert gm.update_branch_with_base("worc/t1", "main") is False
    local_head = git_run(["rev-parse", "refs/heads/worc/t1"], git_repo.clone)

    gm.push_branch_update("task-001", "worc/t1")

    # Query the remote from the clone (the bare repo refuses in-repo git under safe.bareRepository).
    remote_line = git_run(["ls-remote", "origin", "refs/heads/worc/t1"], git_repo.clone)
    assert remote_line.split()[0] == local_head
    # Idempotent: a re-push of the same commit is a git no-op.
    gm.push_branch_update("task-001", "worc/t1")


def test_push_branch_update_refuses_a_destination_changed_since_branch_prep(
    git_repo,
    store: StateStore,
    tmp_path: Path,
    make_git_config: ConfigFactory,
    git_run: GitRunner,
) -> None:
    # The destination is re-read before EVERY push, and this is the push that happens after the
    # agent has had its run at the clone — in a later process that prepares no branch. With the
    # baseline held only in memory the gate would find nothing to compare and let the branch go to
    # a rewritten `pushurl`, carrying this orchestrator's credentials with it.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _task(store)
    gm.prepare_branch("task-001", "slug", epoch=1)  # stamps the baseline, pre-provider
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "feature.txt", "feature\n")
    elsewhere = tmp_path / "elsewhere.git"
    git_run(["init", "--bare", str(elsewhere)], git_repo.clone)
    git_run(["remote", "set-url", "--push", "origin", str(elsewhere)], git_repo.clone)

    with pytest.raises(ManualActionRequired, match="push destination"):
        gm.push_branch_update("task-001", "worc/t1")

    # Nothing was sent: the refusal happens before the push, not after it.
    assert git_run(["ls-remote", str(elsewhere), "refs/heads/worc/t1"], git_repo.clone) == ""


def test_push_branch_update_uses_the_persisted_baseline_in_a_later_process(
    git_repo,
    store: StateStore,
    tmp_path: Path,
    make_git_config: ConfigFactory,
    git_run: GitRunner,
) -> None:
    # The half that makes the refusal above reachable at all: `merge-task` runs with a fresh Git
    # Manager, so the comparison has to come from the task's own record rather than from memory.
    prep = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _task(store)
    prep.prepare_branch("task-001", "slug", epoch=1)
    assert store.get_push_url_digest("task-001") is not None
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "feature.txt", "feature\n")
    elsewhere = tmp_path / "elsewhere2.git"
    git_run(["init", "--bare", str(elsewhere)], git_repo.clone)
    git_run(["remote", "set-url", "--push", "origin", str(elsewhere)], git_repo.clone)

    later = _manager(git_repo, store, tmp_path / "art", make_git_config)  # no branch prep here
    with pytest.raises(ManualActionRequired, match="push destination"):
        later.push_branch_update("task-001", "worc/t1")


def test_record_external_merge_writes_op_and_is_idempotent(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _task(store)
    url = "https://github.com/o/r/pull/7"

    gm.record_external_merge("task-001", url)

    op = store.get_publish_op("task-001", KIND_PR_MERGE, None)
    assert op is not None and op.status == "completed" and op.result_ref == "merged"
    gm.record_external_merge("task-001", url)  # idempotent: a second call is a no-op
    op2 = store.get_publish_op("task-001", KIND_PR_MERGE, None)
    assert op2 is not None and op2.result_ref == "merged"


def test_merge_in_progress_false_without_merge(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    assert gm.merge_in_progress() is False
