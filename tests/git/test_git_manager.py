"""Tests for the Git Manager against a real temporary git repo."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import MergeStrategy
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import (
    EXCLUDED_DIRS,
    KIND_PR_MERGE,
    GitCommandError,
    GitManager,
    GitResult,
)
from wastech_orchestrator.state_store import PublishOpRow, StateStore, TaskRow

GitRunner = Callable[[Sequence[str], Path], str]
ConfigFactory = Callable[..., object]


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore.open(tmp_path / "state.db")


def _task(store: StateStore, task_id: str = "task-001") -> None:
    store.insert_task(TaskRow(task_id=task_id, title="t", status=Status.NEW))


def _manager(
    git_repo,
    store: StateStore,
    artifacts_root: Path,
    make_git_config: ConfigFactory,
    *,
    create_pr: bool = True,
    audit_on_branch: str = "task",
    gh_runner=None,
) -> GitManager:
    config = make_git_config(
        git_repo.clone,
        create_pr=create_pr,
        audit_on_branch=audit_on_branch,
    )
    return GitManager(config, store=store, artifacts_root=str(artifacts_root), gh_runner=gh_runner)


def test_prepare_branch_creates_task_branch(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    branch = gm.prepare_branch("task-001", "add-thing")
    assert branch == "agent/task-001-add-thing"
    head = git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone)
    assert head == "agent/task-001-add-thing"


def test_prepare_branch_reused_on_restart(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    branch = gm.prepare_branch("task-001", "x")  # restart must not fail recreating the branch
    assert branch == "agent/task-001-x"


def test_scoped_staging_excludes_artifact_dirs(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    (git_repo.clone / "src.py").write_text("print('hi')\n", encoding="utf-8")
    for d in EXCLUDED_DIRS:
        (git_repo.clone / d).mkdir(exist_ok=True)
        (git_repo.clone / d / "junk.txt").write_text("x", encoding="utf-8")

    sha = gm.commit_code("task-001", "feat: add src")
    assert sha is not None
    committed = git_run(["show", "--name-only", "--format=", "HEAD"], git_repo.clone).split()
    assert "src.py" in committed
    for d in EXCLUDED_DIRS:
        assert not any(f.startswith(f"{d}/") for f in committed)


def test_changed_code_paths_filters_artifacts(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    (git_repo.clone / "a.py").write_text("x\n", encoding="utf-8")
    (git_repo.clone / ".worc" / "logs").mkdir(parents=True)
    (git_repo.clone / ".worc" / "logs" / "run.log").write_text("x\n", encoding="utf-8")
    paths = gm.changed_code_paths()
    assert "a.py" in paths
    assert not any(p.startswith(".worc/") for p in paths)


def test_never_uses_git_add_dot(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    calls: list[list[str]] = []
    real = GitManager._run

    def spy(self: GitManager, argv: Sequence[str]) -> GitResult:
        calls.append(list(argv))
        return real(self, argv)

    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    GitManager._run = spy  # type: ignore[method-assign]
    try:
        gm.prepare_branch("task-001", "x")
        (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")
        gm.commit_code("task-001", "feat")
    finally:
        GitManager._run = real  # type: ignore[method-assign]

    add_calls = [c for c in calls if len(c) >= 2 and c[1] == "add"]
    assert add_calls, "expected at least one git add"
    for c in add_calls:
        assert "." not in c
        assert "-A" not in c and "--all" not in c


def test_changed_code_paths_excludes_worc_home(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # Everything the orchestrator generates lives under the in-repo .worc/ home (state.db,
    # config.yaml, …); nothing under it may ever be staged into a code commit.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    (git_repo.clone / ".worc").mkdir(exist_ok=True)
    (git_repo.clone / ".worc" / "state.db").write_text("db\n", encoding="utf-8")
    (git_repo.clone / ".worc" / "config.yaml").write_text("cfg\n", encoding="utf-8")
    (git_repo.clone / "real.py").write_text("code\n", encoding="utf-8")
    paths = gm.changed_code_paths()
    assert "real.py" in paths
    assert not any(p.startswith(".worc/") for p in paths)


def test_resolved_profile_not_in_code_commit(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # The generated checks profile (a runtime cache) lives under .worc/checks/ and must never
    # ride a code commit.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    (git_repo.clone / "real.py").write_text("code\n", encoding="utf-8")
    (git_repo.clone / ".worc" / "checks").mkdir(parents=True, exist_ok=True)
    (git_repo.clone / ".worc" / "checks" / "resolved-profile.json").write_text(
        "{}\n", encoding="utf-8"
    )
    sha = gm.commit_code("task-001", "feat: real")
    assert sha is not None
    committed = git_run(["show", "--name-only", "--format=", "HEAD"], git_repo.clone).split()
    assert "real.py" in committed
    assert not any(f.startswith(".worc/") for f in committed)
    assert ".worc/checks/resolved-profile.json" not in gm.changed_code_paths()


def test_ensure_runtime_excludes_writes_worc_line(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.ensure_runtime_excludes()
    gm.ensure_runtime_excludes()  # idempotent
    gitignore = (git_repo.clone / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count(".worc/") == 1


def test_diff_stat_returns_stat_only(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # diff_stat() feeds the compact minimal summary: files + counts, never the patch body.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    (git_repo.clone / "mod.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    gm.commit_code("task-001", "feat: mod")
    stat = gm.diff_stat()
    assert "mod.py" in stat and "changed" in stat
    assert "diff --git" not in stat and "@@" not in stat


def test_refresh_base_pulls_pushed_commits(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # A second clone pushes a new task file to origin/main; refresh_base brings it into the
    # orchestrator clone without a manual pull (periodic discovery).
    other = tmp_path / "other"
    git_run(["clone", str(git_repo.remote), str(other)], tmp_path)
    git_run(["config", "user.email", "o@example.com"], other)
    git_run(["config", "user.name", "Other"], other)
    git_run(["config", "commit.gpgsign", "false"], other)
    (other / "pushed-task.md").write_text("task\n", encoding="utf-8")
    git_run(["add", "pushed-task.md"], other)
    git_run(["commit", "-m", "add task via git"], other)
    git_run(["push", "origin", "main"], other)

    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    assert not (git_repo.clone / "pushed-task.md").exists()  # not seen yet
    gm.refresh_base()
    assert (git_repo.clone / "pushed-task.md").exists()  # pulled in


def test_refresh_base_is_noop_off_base_branch(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    branch = gm.prepare_branch("task-001", "x")  # now on the task branch, not base
    gm.refresh_base()  # must not switch branches or pull onto an active task branch
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == branch


def test_audit_commit_commits_tasks_and_summary(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")
    gm.commit_code("task-001", "feat")
    (git_repo.clone / "tasks" / "done").mkdir(parents=True, exist_ok=True)
    (git_repo.clone / "tasks" / "done" / "task-001.md").write_text("x\n", encoding="utf-8")
    (git_repo.clone / "tasks" / "done" / "task-001.summary.md").write_text("s\n", encoding="utf-8")
    # Working artifacts live under .worc/; they must never enter the audit commit.
    (git_repo.clone / ".worc" / "logs").mkdir(parents=True, exist_ok=True)
    (git_repo.clone / ".worc" / "logs" / "run.log").write_text("x\n", encoding="utf-8")

    sha = gm.commit_audit("task-001")
    assert sha is not None
    msg = git_run(["log", "-1", "--format=%s", "HEAD"], git_repo.clone)
    assert "audit trail for task-001" in msg
    # The task lifecycle + summary are committed; .worc/ is deliberately NOT (kept out of git).
    tracked = git_run(["ls-files"], git_repo.clone)
    assert "tasks/done/task-001.md" in tracked
    assert "tasks/done/task-001.summary.md" in tracked
    assert ".worc/logs/run.log" not in tracked


def test_audit_commit_noop_when_no_tasks(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # With nothing staged under tasks/, the audit commit is a no-op (returns None).
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    assert gm.commit_audit("task-001") is None


def test_audit_commit_stages_lifecycle_move_deletion(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # A task file already tracked under tasks/failed/ that is moved to tasks/done/ must have its
    # *deletion* staged too — otherwise the dangling removal rides back onto the base branch as a
    # `D` status after terminal cleanup (the task-023 regression).
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    failed = git_repo.clone / "tasks" / "failed"
    failed.mkdir(parents=True, exist_ok=True)
    (failed / "task-001.md").write_text("t\n", encoding="utf-8")
    git_run(["add", "tasks/failed/task-001.md"], git_repo.clone)
    git_run(["commit", "-m", "track failed task file"], git_repo.clone)
    # Lifecycle move: failed -> done (the orchestrator's _relocate_task_file does this on disk).
    (failed / "task-001.md").unlink()
    done = git_repo.clone / "tasks" / "done"
    done.mkdir(parents=True, exist_ok=True)
    (done / "task-001.md").write_text("t\n", encoding="utf-8")
    (done / "task-001.summary.md").write_text("s\n", encoding="utf-8")

    sha = gm.commit_audit("task-001")
    assert sha is not None
    # The move is committed: the old path is gone from the tree (git may record it as a delete or
    # a rename) and the new path is tracked — so the removal is part of the commit, not dangling.
    tracked = git_run(["ls-files"], git_repo.clone)
    assert "tasks/failed/task-001.md" not in tracked
    assert "tasks/done/task-001.md" in tracked
    # The working tree is clean: no dangling `D tasks/failed/task-001.md` to ride back to base.
    assert "tasks/failed/task-001.md" not in git_run(["status", "--porcelain"], git_repo.clone)


def test_snapshot_capture_and_partial_change(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    before = gm.capture()
    assert gm.partial_change_since(before) is None  # nothing changed yet
    (git_repo.clone / "new.py").write_text("x\n", encoding="utf-8")
    git_run(["add", "new.py"], git_repo.clone)
    partial = gm.partial_change_since(before)
    assert partial is not None
    assert Path(partial.diff_path).exists()
    assert partial.before.diff_checksum == before.diff_checksum


def test_push_idempotent(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    branch = gm.prepare_branch("task-001", "x")
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")
    gm.commit_code("task-001", "feat")
    assert gm.push("task-001", branch) is True
    assert gm.push("task-001", branch) is True  # idempotent
    op = store.get_publish_op("task-001", "push")
    assert op is not None and op.status == "completed"


def test_create_pr_with_fake_gh(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    gh_calls: list[list[str]] = []

    def fake_gh(argv: Sequence[str]) -> GitResult:
        gh_calls.append(list(argv))
        return GitResult(
            exit_code=0,
            stdout="https://github.com/o/r/pull/1\n",
            stderr="",
            timed_out=False,
            launch_error=None,
        )

    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=fake_gh)
    branch = gm.prepare_branch("task-001", "x")
    body = tmp_path / "summary.md"
    body.write_text("# summary\n", encoding="utf-8")
    url = gm.create_pr("task-001", branch, title="My PR", body_path=str(body))
    assert url == "https://github.com/o/r/pull/1"
    assert gh_calls[0][:2] == ["pr", "create"]
    assert "--body-file" in gh_calls[0]
    url2 = gm.create_pr("task-001", branch, title="My PR", body_path=str(body))
    assert url2 == url
    assert len(gh_calls) == 1  # idempotent: gh not invoked again


def test_create_pr_real_runner_adds_gh_executable_once(
    git_repo,
    store: StateStore,
    tmp_path: Path,
    make_git_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _task(store)
    calls: list[list[str]] = []
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)

    def fake_run(argv: Sequence[str]) -> GitResult:
        calls.append(list(argv))
        return GitResult(
            exit_code=0,
            stdout="https://github.com/o/r/pull/1\n",
            stderr="",
            timed_out=False,
            launch_error=None,
        )

    monkeypatch.setattr(gm, "_run", fake_run)
    body = tmp_path / "summary.md"
    body.write_text("# summary\n", encoding="utf-8")

    gm.create_pr("task-001", "agent/task-001-x", title="My PR", body_path=str(body))

    assert calls[0][:3] == ["gh", "pr", "create"]
    assert calls[0].count("gh") == 1


def test_create_pr_disabled_returns_none(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, create_pr=False)
    branch = gm.prepare_branch("task-001", "x")
    assert gm.create_pr("task-001", branch, title="t", body_path="x") is None


# --- merge_pr (auto-merge bypass, idempotency) ---

_PR_URL = "https://github.com/o/r/pull/1"


def _merge_gh(
    calls: list[list[str]], *, merge_exit: int = 0, merge_stderr: str = "", sha: str = "deadbeef"
) -> Callable[[Sequence[str]], GitResult]:
    def gh(argv: Sequence[str]) -> GitResult:
        calls.append(list(argv))
        head = list(argv[:2])
        if head == ["pr", "view"]:
            return GitResult(
                exit_code=0, stdout=f"{sha}\n", stderr="", timed_out=False, launch_error=None
            )
        if head == ["pr", "merge"]:
            return GitResult(
                exit_code=merge_exit,
                stdout="",
                stderr=merge_stderr,
                timed_out=False,
                launch_error=None,
            )
        return GitResult(exit_code=0, stdout="", stderr="", timed_out=False, launch_error=None)

    return gh


def test_merge_pr_immediate_argv_sha_and_no_admin(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    calls: list[list[str]] = []
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=_merge_gh(calls))
    out = gm.merge_pr("task-001", _PR_URL, strategy=MergeStrategy.SQUASH, wait_for_checks=False)
    assert out == "deadbeef"
    assert calls[0] == ["pr", "merge", _PR_URL, "--squash"]
    # Never weakens branch protection.
    assert all(
        "--admin" not in c and not any(t.startswith("--dangerously") for t in c) for c in calls
    )
    op = store.get_publish_op("task-001", KIND_PR_MERGE)
    assert op is not None and op.status == "completed" and op.result_ref == "deadbeef"


def test_merge_pr_wait_for_checks_arms_auto(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    calls: list[list[str]] = []
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=_merge_gh(calls))
    out = gm.merge_pr("task-001", _PR_URL, strategy=MergeStrategy.MERGE, wait_for_checks=True)
    assert out == "armed"
    assert calls[0] == ["pr", "merge", _PR_URL, "--merge", "--auto"]
    # Arming is async — no synchronous SHA lookup.
    assert not any(list(c[:2]) == ["pr", "view"] for c in calls)


def test_merge_pr_is_idempotent(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    calls: list[list[str]] = []
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=_merge_gh(calls))
    first = gm.merge_pr("task-001", _PR_URL, strategy=MergeStrategy.SQUASH, wait_for_checks=False)
    n = len(calls)
    second = gm.merge_pr("task-001", _PR_URL, strategy=MergeStrategy.SQUASH, wait_for_checks=False)
    assert first == second == "deadbeef"
    assert len(calls) == n  # the completed op short-circuits — gh is not invoked again


def test_merge_pr_blocked_raises_and_stays_incomplete(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    calls: list[list[str]] = []
    gh = _merge_gh(calls, merge_exit=1, merge_stderr="required status checks are pending")
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    with pytest.raises(GitCommandError):
        gm.merge_pr("task-001", _PR_URL, strategy=MergeStrategy.SQUASH, wait_for_checks=False)
    op = store.get_publish_op("task-001", KIND_PR_MERGE)
    assert op is not None and op.status != "completed"  # resume may retry


def test_merge_pr_already_merged_is_idempotent_success(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    calls: list[list[str]] = []
    gh = _merge_gh(calls, merge_exit=1, merge_stderr="GraphQL: Pull request is already merged")
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    out = gm.merge_pr("task-001", _PR_URL, strategy=MergeStrategy.SQUASH, wait_for_checks=False)
    assert out == "merged"
    op = store.get_publish_op("task-001", KIND_PR_MERGE)
    assert op is not None and op.status == "completed"


def test_terminal_cleanup_safe(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    _task(store)
    art = tmp_path / "art"
    gm = _manager(git_repo, store, art, make_git_config)
    gm.prepare_branch("task-001", "x")
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")
    gm.commit_code("task-001", "feat")
    outcome = gm.terminal_cleanup("task-001")
    assert outcome.safe is True
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    assert (art / "logs" / "task-001" / "publish" / "terminal-cleanup.json").exists()


def test_terminal_cleanup_unsafe_when_dirty(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    (git_repo.clone / "README.md").write_text("changed\n", encoding="utf-8")  # tracked, uncommitted
    outcome = gm.terminal_cleanup("task-001")
    assert outcome.safe is False
    assert outcome.error is not None


def test_write_current_diff(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    art = tmp_path / "art"
    gm = _manager(git_repo, store, art, make_git_config)
    gm.prepare_branch("task-001", "x")
    (git_repo.clone / "README.md").write_text("changed\n", encoding="utf-8")
    path = gm.write_current_diff("task-001")
    assert Path(path).exists()
    assert "README.md" in Path(path).read_text(encoding="utf-8")


def test_push_to_base_branch_is_refused(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    #: publishing is PR-only; a push aimed at base_branch is refused, never executed.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    with pytest.raises(GitCommandError):
        gm.push("task-001", "main")


def test_current_diff_is_redacted(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    #: current.diff (the failure report reads it back) must carry no secrets — token-shaped
    # ones via pattern, denied_read_paths values via the content-scan seed.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    token = "ghp_" + "D" * 20
    file_secret = "plainOpaqueSecret12345"
    (git_repo.clone / ".env").write_text(f"APP_SECRET={file_secret}\n", encoding="utf-8")
    (git_repo.clone / "README.md").write_text(
        f"# project\nleak {token}\ncopied {file_secret}\n", encoding="utf-8"
    )
    diff = Path(gm.write_current_diff("task-001")).read_text(encoding="utf-8")
    assert token not in diff
    assert file_secret not in diff


# --- finalize building blocks: read-only PR verify + local branch delete -----------------


def test_verify_pr_state_returns_state(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    def gh(args: Sequence[str]) -> GitResult:
        assert list(args[:2]) == ["pr", "view"]
        assert "state" in args
        return GitResult(
            exit_code=0, stdout="MERGED\n", stderr="", timed_out=False, launch_error=None
        )

    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    assert gm.verify_pr_state("https://example/pull/1") == "MERGED"


def test_verify_pr_state_none_when_gh_unavailable(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    def gh(_args: Sequence[str]) -> GitResult:
        return GitResult(
            exit_code=1, stdout="", stderr="not found", timed_out=False, launch_error=None
        )

    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    assert gm.verify_pr_state("https://example/pull/1") is None


def _view_state_gh(payload: dict[str, object], *, ok: bool = True) -> Callable[..., GitResult]:
    def gh(args: Sequence[str]) -> GitResult:
        assert list(args[:2]) == ["pr", "view"]
        assert "state,mergeCommit" in args  # the single readiness probe, not two calls
        return GitResult(
            exit_code=0 if ok else 1,
            stdout=json.dumps(payload) if ok else "",
            stderr="" if ok else "not found",
            timed_out=False,
            launch_error=None,
        )

    return gh


def test_pr_merge_state_merged_returns_state_and_sha(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gh = _view_state_gh({"state": "MERGED", "mergeCommit": {"oid": "abc123"}})
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    assert gm.pr_merge_state(_PR_URL) == ("MERGED", "abc123")


def test_pr_merge_state_open_has_no_sha(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gh = _view_state_gh({"state": "OPEN", "mergeCommit": None})
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    assert gm.pr_merge_state(_PR_URL) == ("OPEN", None)


def test_pr_merge_state_probe_failure_is_none(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(
        git_repo, store, tmp_path / "art", make_git_config, gh_runner=_view_state_gh({}, ok=False)
    )
    assert gm.pr_merge_state(_PR_URL) == (None, None)


def _seed_armed_merge(store: StateStore, task_id: str = "task-001") -> None:
    store.record_publish_op(
        PublishOpRow(
            task_id=task_id,
            kind=KIND_PR_MERGE,
            fingerprint=_PR_URL,
            status="completed",
            result_ref="armed",
        )
    )


def test_backfill_merge_sha_replaces_armed(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    _seed_armed_merge(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.backfill_merge_sha("task-001", "realsha")
    op = store.get_publish_op("task-001", KIND_PR_MERGE)
    assert op is not None and op.status == "completed" and op.result_ref == "realsha"


def test_backfill_merge_sha_idempotent_and_skips_unknown(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    # No recorded merge op → no-op (no op is created).
    gm.backfill_merge_sha("task-001", "realsha")
    assert store.get_publish_op("task-001", KIND_PR_MERGE) is None
    # Already the real SHA → stays put.
    _seed_armed_merge(store)
    gm.backfill_merge_sha("task-001", "realsha")
    gm.backfill_merge_sha("task-001", "realsha")
    op = store.get_publish_op("task-001", KIND_PR_MERGE)
    assert op is not None and op.result_ref == "realsha"


def test_delete_branch_is_idempotent(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    git_run(["branch", "agent/task-001-x"], git_repo.clone)
    assert gm.delete_branch("agent/task-001-x") is True  # deleted
    assert gm.delete_branch("agent/task-001-x") is False  # already gone — no-op
