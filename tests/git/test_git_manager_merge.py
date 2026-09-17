"""Tests for the operator-driven merge primitives on the Git Manager (worc merge-task).

Exercises ``update_branch_with_base`` / ``merge_in_progress`` / ``merge_abort`` /
``commit_merge_resolution`` / ``push_branch_update`` / ``record_external_merge`` against a real
temporary git repo with a bare ``origin`` remote.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import (
    KIND_MERGE_COMMIT,
    KIND_PR_MERGE,
    ConflictEvidence,
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


def test_commit_merge_resolution_leaves_a_tracked_runtime_file_out_of_the_merge(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # The base merge is the one publishing path that stages the whole tree (`git add -A`), and an
    # installed repo re-includes part of `.worc/` on purpose (`.worc/*` + `!.worc/config.yaml`, so
    # config changes are reviewable in history) — which makes that file TRACKED. Nothing leaked
    # without the exclusion: `assert_exchange_never_staged` refuses the commit. But it refused on an
    # ordinary operator edit, so a routine config change hard-blocked the base merge as a
    # runtime-artifact violation. Excluded, the merge just proceeds without it.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _task(store)
    (git_repo.clone / ".gitignore").write_text(".worc/*\n!.worc/config.yaml\n", encoding="utf-8")
    worc_home = git_repo.clone / ".worc"
    worc_home.mkdir()
    (worc_home / "config.yaml").write_text("poll_interval_seconds: 60\n", encoding="utf-8")
    (worc_home / "state.db").write_text("ignored runtime state\n", encoding="utf-8")
    git_run(["add", ".gitignore", ".worc/config.yaml"], git_repo.clone)
    git_run(["commit", "-m", "track the orchestrator config"], git_repo.clone)
    git_run(["push", "origin", "main"], git_repo.clone)
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "README.md", "branch side\n")
    _advance_base(git_run, git_repo.clone, "README.md", "base side\n")
    assert gm.update_branch_with_base("worc/t1", "main") is True
    (git_repo.clone / "README.md").write_text("resolved\n", encoding="utf-8")
    # The operator edits the orchestrator config while the task is in flight.
    (worc_home / "config.yaml").write_text("poll_interval_seconds: 30\n", encoding="utf-8")

    sha = gm.commit_merge_resolution("task-001", "merge(task-001): resolve")

    assert sha
    committed = git_run(["show", "--pretty=format:", "--name-only", sha], git_repo.clone).split()
    assert "README.md" in committed
    assert not [path for path in committed if path.startswith(".worc/")]
    # The edit is untouched in the working tree — excluded from the commit, not reverted.
    assert (worc_home / "config.yaml").read_text(encoding="utf-8") == "poll_interval_seconds: 30\n"


def test_commit_merge_resolution_still_commits_when_the_runtime_home_is_fully_ignored(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # The default `worc install` shape ignores the whole `.worc/` dir. `git add` refuses (exit 1,
    # "The following paths are ignored … use -f") when a pathspec names a root that exists on disk
    # and is entirely ignored, so a blanket `:(exclude)` would break every base merge on a default
    # install — the same trap `staged_pathspec` documents for the task lifecycle dir.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _task(store)
    (git_repo.clone / ".gitignore").write_text(".worc/\n.worc-io/\n", encoding="utf-8")
    for dirname in (".worc", ".worc-io"):
        (git_repo.clone / dirname).mkdir()
        (git_repo.clone / dirname / "runtime.txt").write_text("ignored\n", encoding="utf-8")
    git_run(["add", ".gitignore"], git_repo.clone)
    git_run(["commit", "-m", "ignore the runtime home"], git_repo.clone)
    git_run(["push", "origin", "main"], git_repo.clone)
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "README.md", "branch side\n")
    _advance_base(git_run, git_repo.clone, "README.md", "base side\n")
    assert gm.update_branch_with_base("worc/t1", "main") is True
    (git_repo.clone / "README.md").write_text("resolved\n", encoding="utf-8")

    sha = gm.commit_merge_resolution("task-001", "merge(task-001): resolve")

    assert sha
    committed = git_run(["show", "--pretty=format:", "--name-only", sha], git_repo.clone).split()
    assert "README.md" in committed
    assert not [path for path in committed if path.startswith((".worc/", ".worc-io/"))]


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


# --- conflicted_paths: the conflict inventory the merge gate and the conflict report read --------


def _seed_base_file(git_run: GitRunner, clone: Path, path: str, content: str) -> None:
    """Commit ``path`` on main and push it, so it exists in the merge base of any later branch."""
    git_run(["checkout", "main"], clone)
    (clone / path).write_text(content, encoding="utf-8")
    git_run(["add", path], clone)
    git_run(["commit", "-m", f"base seed {path}"], clone)
    git_run(["push", "origin", "main"], clone)


def _delete_on_base(git_run: GitRunner, clone: Path, path: str) -> None:
    """Delete ``path`` on main and push it (the base drops a file the branch still has)."""
    git_run(["checkout", "main"], clone)
    git_run(["rm", path], clone)
    git_run(["commit", "-m", f"base deletes {path}"], clone)
    git_run(["push", "origin", "main"], clone)


def _branch_deleting(git_run: GitRunner, clone: Path, branch: str, path: str) -> None:
    """Create ``branch`` off main whose only change is deleting ``path``."""
    git_run(["checkout", "-b", branch, "main"], clone)
    git_run(["rm", path], clone)
    git_run(["commit", "-m", f"task deletes {path}"], clone)
    git_run(["push", "-u", "origin", branch], clone)
    git_run(["checkout", "main"], clone)


def _conflict_on(gm: GitManager, git_run: GitRunner, clone: Path, path: str) -> None:
    """The ordinary both-modified conflict on ``path``, left in flight."""
    _branch_with_change(git_run, clone, "worc/t1", path, "branch side\n")
    _advance_base(git_run, clone, path, "base side\n")
    assert gm.update_branch_with_base("worc/t1", "main") is True


def test_conflicted_paths_is_empty_without_a_merge(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # The gate must be inert on the clean path: no merge in flight means no unmerged index entry.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)

    assert gm.conflicted_paths() == ()


def test_conflicted_paths_reports_both_modified_with_markers(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _conflict_on(gm, git_run, git_repo.clone, "README.md")

    (entry,) = gm.conflicted_paths()

    assert entry.path == "README.md"
    assert entry.code == "UU"
    assert (entry.has_base, entry.has_ours, entry.has_theirs) == (True, True, True)
    assert entry.evidence is ConflictEvidence.WORKTREE_BYTES
    assert entry.exists is True and entry.digest is not None
    assert entry.has_markers is True and entry.binary is False


def test_conflicted_paths_reports_a_modify_delete_without_markers(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # The defect this inventory exists for: git leaves OUR file in the tree with no marker in it,
    # so nothing downstream could tell a resolution from an untouched file.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _seed_base_file(git_run, git_repo.clone, "shared.txt", "base\n")
    _branch_with_change(git_run, git_repo.clone, "worc/t1", "shared.txt", "task edit\n")
    _delete_on_base(git_run, git_repo.clone, "shared.txt")
    assert gm.update_branch_with_base("worc/t1", "main") is True

    (entry,) = gm.conflicted_paths()

    assert entry.code == "UD"
    assert (entry.has_base, entry.has_ours, entry.has_theirs) == (True, True, False)
    assert entry.evidence is ConflictEvidence.WORKTREE_BYTES
    assert entry.exists is True  # our version sits there, alone and unmarked
    assert entry.has_markers is False


def test_conflicted_paths_reports_a_delete_modify_without_markers(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _seed_base_file(git_run, git_repo.clone, "shared.txt", "base\n")
    _branch_deleting(git_run, git_repo.clone, "worc/t1", "shared.txt")
    _advance_base(git_run, git_repo.clone, "shared.txt", "base edit\n")
    assert gm.update_branch_with_base("worc/t1", "main") is True

    (entry,) = gm.conflicted_paths()

    assert entry.code == "DU"
    assert (entry.has_base, entry.has_ours, entry.has_theirs) == (True, False, True)
    assert entry.has_markers is False


def test_conflicted_paths_reports_a_binary_add_add_as_binary(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # A binary conflict can carry no markers at all — there is no textual form to merge.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    clone = git_repo.clone
    git_run(["checkout", "-b", "worc/t1", "main"], clone)
    (clone / "blob.bin").write_bytes(b"\x00branch\x00")
    git_run(["add", "blob.bin"], clone)
    git_run(["commit", "-m", "task adds a binary"], clone)
    git_run(["push", "-u", "origin", "worc/t1"], clone)
    git_run(["checkout", "main"], clone)
    (clone / "blob.bin").write_bytes(b"\x00base\x00")
    git_run(["add", "blob.bin"], clone)
    git_run(["commit", "-m", "base adds a binary"], clone)
    git_run(["push", "origin", "main"], clone)
    assert gm.update_branch_with_base("worc/t1", "main") is True

    (entry,) = gm.conflicted_paths()

    assert entry.code == "AA"
    assert (entry.has_base, entry.has_ours, entry.has_theirs) == (False, True, True)
    assert entry.binary is True and entry.has_markers is False


def test_conflicted_paths_track_the_working_tree_across_an_edit(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # The before/after channel the merge gate reads: the index identity is stable (nobody may run
    # `git add`), and only the working-tree probe moves.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _conflict_on(gm, git_run, git_repo.clone, "README.md")
    (before,) = gm.conflicted_paths()

    (git_repo.clone / "README.md").write_text("resolved\n", encoding="utf-8")
    (after,) = gm.conflicted_paths()

    assert (after.path, after.code, after.evidence) == (before.path, before.code, before.evidence)
    assert after.digest != before.digest
    assert after.has_markers is False

    (git_repo.clone / "README.md").unlink()
    (gone,) = gm.conflicted_paths()
    assert gone.code == before.code  # still the same unmerged entry
    assert gone.exists is False and gone.digest is None


def test_conflicted_paths_handles_a_non_ascii_path(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # `-z` output never C-quotes; a Cyrillic name must survive the parse and resolve on disk.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _conflict_on(gm, git_run, git_repo.clone, "документ.md")

    (entry,) = gm.conflicted_paths()

    assert entry.path == "документ.md"
    assert entry.digest is not None


def test_conflicted_paths_probes_an_unreadable_entry_as_undecidable(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # A path the tree holds as something other than a regular file is evidence of nothing; it must
    # probe as unreadable (the caller then refuses) rather than raise out of the inventory.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _conflict_on(gm, git_run, git_repo.clone, "README.md")
    (git_repo.clone / "README.md").unlink()
    (git_repo.clone / "README.md").mkdir()

    (entry,) = gm.conflicted_paths()

    assert entry.exists is False and entry.digest is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_conflicted_paths_does_not_follow_a_symlink(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    _conflict_on(gm, git_run, git_repo.clone, "README.md")
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("not the conflicted file\n", encoding="utf-8")
    (git_repo.clone / "README.md").unlink()
    (git_repo.clone / "README.md").symlink_to(decoy)

    (entry,) = gm.conflicted_paths()

    assert entry.exists is False and entry.digest is None
