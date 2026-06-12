"""Tests for the Git Manager (§21) against a real temporary git repo."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import (
    EXCLUDED_DIRS,
    GitCommandError,
    GitManager,
    GitResult,
    ManualActionRequired,
)
from wastech_orchestrator.state_store import StateStore, TaskRow

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
    location: str = "external",
    tracking: str = "none",
    create_pr: bool = True,
    audit_on_branch: str = "task",
    gh_runner=None,
) -> GitManager:
    config = make_git_config(
        git_repo.clone,
        location=location,
        tracking=tracking,
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
    (git_repo.clone / "logs").mkdir()
    (git_repo.clone / "logs" / "run.log").write_text("x\n", encoding="utf-8")
    paths = gm.changed_code_paths()
    assert "a.py" in paths
    assert not any(p.startswith("logs/") for p in paths)


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


def test_exclude_local_appends_idempotently(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(
        git_repo,
        store,
        tmp_path / "art",
        make_git_config,
        location="in_repo",
        tracking="exclude_local",
    )
    gm.ensure_exclude_local()
    gm.ensure_exclude_local()  # idempotent
    exclude = (git_repo.clone / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    for d in EXCLUDED_DIRS:
        assert exclude.count(f"{d}/") == 1


def test_exclude_local_noop_when_external(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.ensure_exclude_local()
    exclude_path = git_repo.clone / ".git" / "info" / "exclude"
    content = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    assert "tasks/" not in content


def test_preflight_tracked_artifact_path_requires_manual(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    (git_repo.clone / "logs").mkdir()
    (git_repo.clone / "logs" / "keep.txt").write_text("x\n", encoding="utf-8")
    git_run(["add", "logs/keep.txt"], git_repo.clone)
    git_run(["commit", "-m", "track logs"], git_repo.clone)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    with pytest.raises(ManualActionRequired):
        gm.preflight_footprint()


def test_audit_commit_only_when_tracking_commit(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    _task(store)
    gm = _manager(
        git_repo,
        store,
        tmp_path / "art",
        make_git_config,
        location="in_repo",
        tracking="commit",
    )
    gm.prepare_branch("task-001", "x")
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")
    gm.commit_code("task-001", "feat")
    (git_repo.clone / "tasks").mkdir(exist_ok=True)
    (git_repo.clone / "tasks" / "task-001.md").write_text("x\n", encoding="utf-8")
    (git_repo.clone / "logs").mkdir(exist_ok=True)
    (git_repo.clone / "logs" / "run.log").write_text("x\n", encoding="utf-8")

    sha = gm.commit_audit("task-001")
    assert sha is not None
    msg = git_run(["log", "-1", "--format=%s", "HEAD"], git_repo.clone)
    assert "audit trail for task-001" in msg


def test_no_audit_commit_when_tracking_none(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x")
    assert gm.commit_audit("task-001") is None


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
    assert gm.push("task-001", branch) is True  # idempotent (§13)
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
    assert gh_calls[0][:3] == ["gh", "pr", "create"]
    assert "--body-file" in gh_calls[0]
    url2 = gm.create_pr("task-001", branch, title="My PR", body_path=str(body))
    assert url2 == url
    assert len(gh_calls) == 1  # idempotent: gh not invoked again


def test_create_pr_disabled_returns_none(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, create_pr=False)
    branch = gm.prepare_branch("task-001", "x")
    assert gm.create_pr("task-001", branch, title="t", body_path="x") is None


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
    # §12.12: publishing is PR-only; a push aimed at base_branch is refused, never executed.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    with pytest.raises(GitCommandError):
        gm.push("task-001", "main")


def test_current_diff_is_redacted(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # §12.6: current.diff (the failure report reads it back) must carry no secrets — token-shaped
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
