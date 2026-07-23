"""Tests for the Git Manager against a real temporary git repo."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import BranchMode, MergeStrategy
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import (
    _PUSH_RETRY_BACKOFF_SECONDS,
    KIND_PR_MERGE,
    RUNTIME_EXCLUDED_DIRS,
    GitCommandError,
    GitManager,
    GitResult,
    ManualActionRequired,
    append_runtime_excludes,
)
from wastech_orchestrator.providers.artifacts import sha256_file
from wastech_orchestrator.providers.process import ProcessResult
from wastech_orchestrator.state_store import PublishOpRow, StateStore, TaskRow

# Every test here is a slow integration test (real git / subprocess / process tree).
pytestmark = pytest.mark.slow

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
    tasks_dir: str = "tasks",
    checkout_base_on_cleanup: bool | None = None,
    gh_runner=None,
) -> GitManager:
    config = make_git_config(
        git_repo.clone,
        create_pr=create_pr,
        audit_on_branch=audit_on_branch,
        tasks_dir=tasks_dir,
        checkout_base_on_cleanup=checkout_base_on_cleanup,
    )
    return GitManager(config, store=store, artifacts_root=str(artifacts_root), gh_runner=gh_runner)


# A fixed epoch keeps the auto-generated branch deterministic for assertions.
_EPOCH = 1700000000

# Dirs kept out of the code commit under the default config: the gitignored runtime home plus the
# default task lifecycle dir (`paths.tasks_dir` defaults to "tasks").
_DEFAULT_EXCLUDED_DIRS = (*RUNTIME_EXCLUDED_DIRS, "tasks")


def test_git_calls_use_trusted_containment_but_gh_does_not(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    """P0: hardened ``git`` runs under the trusted (no-``ps``-sweep) containment, while ``gh`` —
    less constrained — keeps the full WRI-012 barrier. Asserts the ``trusted`` flag the runner
    receives per binary, so the fast path is scoped to git alone."""
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    seen: list[tuple[str, bool]] = []

    def spy_run_process(argv, **kwargs) -> ProcessResult:
        seen.append((argv[0], bool(kwargs.get("trusted", False))))
        Path(kwargs["stdout_path"]).write_text("", encoding="utf-8")
        return ProcessResult(
            exit_code=0,
            timed_out=False,
            launch_error=None,
            duration_seconds=0.0,
            stdout_path=str(kwargs["stdout_path"]),
            stderr_text="",
        )

    gm._run_process = spy_run_process  # type: ignore[assignment]
    gm._git("status", "--porcelain")
    gm._run(["gh", "--version"])

    assert ("git", True) in seen  # hardened git → trusted fast path
    assert ("gh", False) in seen  # gh → full descendant-tracking barrier


def test_prepare_branch_creates_task_branch(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    branch = gm.prepare_branch("task-001", "add-thing", epoch=_EPOCH)
    assert branch == "worc/1700000000-task-001-add-thing"
    head = git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone)
    assert head == "worc/1700000000-task-001-add-thing"


def test_prepare_branch_reused_on_restart(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    # Same epoch → same name; restart must not fail recreating the branch.
    branch = gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    assert branch == "worc/1700000000-task-001-x"


def test_prepare_branch_uses_explicit_branch_name(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    branch = gm.prepare_branch(
        "task-001",
        "ignored-slug",
        epoch=_EPOCH,
        branch_name="feature/ABC-123-customer-branch",
    )
    assert branch == "feature/ABC-123-customer-branch"  # override shadows the epoch + slug
    head = git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone)
    assert head == "feature/ABC-123-customer-branch"


def test_reset_branch_to_base_uses_explicit_branch_name(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    branch = gm.prepare_branch("task-001", "x", epoch=_EPOCH, branch_name="feature/ABC-123-reset")
    assert branch == "feature/ABC-123-reset"
    reset = gm.reset_branch_to_base("task-001", "x", branch_name=branch)
    assert reset == branch
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    assert git_run(["branch", "--list", branch], git_repo.clone) == ""


def test_branch_name_truncates_long_slug_to_total_cap(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    long_slug = "implement-user-authentication-with-oauth2-and-google-provider"
    name = gm.branch_name("task-001", long_slug, epoch=_EPOCH)
    assert len(name) <= 50
    assert name.startswith("worc/1700000000-task-001-")
    assert not name.endswith("-")  # trailing dash from the cut is stripped


def test_branch_name_omits_slug_when_prefix_fills_budget(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    long_id = "task-" + "a" * 40  # prefix + epoch + task_id already exceeds the 50-char cap
    name = gm.branch_name(long_id, "some-slug", epoch=_EPOCH)
    assert name == f"worc/1700000000-{long_id}"  # task_id is never truncated; slug dropped


def test_branch_name_override_wins_regardless_of_length(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    long_override = "feature/" + "x" * 100  # the 50-char soft cap is the gate's job, not here
    assert gm.branch_name("task-001", "slug", epoch=_EPOCH, override=long_override) == long_override


def test_scoped_staging_excludes_artifact_dirs(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "src.py").write_text("print('hi')\n", encoding="utf-8")
    for d in _DEFAULT_EXCLUDED_DIRS:
        (git_repo.clone / d).mkdir(exist_ok=True)
        (git_repo.clone / d / "junk.txt").write_text("x", encoding="utf-8")

    sha = gm.commit_code("task-001", "feat: add src")
    assert sha is not None
    committed = git_run(["show", "--name-only", "--format=", "HEAD"], git_repo.clone).split()
    assert "src.py" in committed
    for d in _DEFAULT_EXCLUDED_DIRS:
        assert not any(f.startswith(f"{d}/") for f in committed)


def test_commit_code_root_file_when_tasks_dir_gitignored(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    """Regression (F15): a root-level code change must still commit when the task lifecycle dir is
    gitignored (what ``worc install`` seeds). The ``:(exclude)tasks/`` guard makes ``git add`` abort
    with "paths ignored: tasks" (exit 1) beside a repo-root path, so it must be dropped when the dir
    is ignored — otherwise no root-level change (package.json / tsconfig.json / …) can be committed.
    """
    _task(store)
    (git_repo.clone / ".gitignore").write_text("tasks/\n.worc/\n", encoding="utf-8")
    git_run(["add", ".gitignore"], git_repo.clone)
    git_run(["commit", "-m", "chore: gitignore tasks/"], git_repo.clone)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    # Mirror finalize: the task file is moved into the (ignored) lifecycle dir before the commit.
    (git_repo.clone / "tasks" / "done").mkdir(parents=True)
    (git_repo.clone / "tasks" / "done" / "task-001.md").write_text("moved\n", encoding="utf-8")
    (git_repo.clone / "tsconfig.json").write_text("{}\n", encoding="utf-8")  # a repo-root change

    sha = gm.commit_code("task-001", "feat: root-level change")
    assert sha is not None
    committed = git_run(["show", "--name-only", "--format=", "HEAD"], git_repo.clone).split()
    assert "tsconfig.json" in committed
    assert not any(f.startswith("tasks/") for f in committed)


def test_staged_pathspec_conditional_on_tasks_dir_ignore(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    """``staged_pathspec`` keeps the ``:(exclude)`` guard when the lifecycle dir is *tracked* but
    drops it when the dir is *gitignored* (the guard is redundant there and breaks ``git add``)."""
    # Tracked (the fixture ships no .gitignore): the exclude guard is present.
    gm_tracked = _manager(git_repo, store, tmp_path / "art", make_git_config)
    assert gm_tracked.staged_pathspec(["a.py"]) == ["a.py", ":(exclude)tasks/"]

    # Gitignored: the guard is dropped (fresh manager so the ignore-state cache reflects the file).
    (git_repo.clone / ".gitignore").write_text("tasks/\n", encoding="utf-8")
    gm_ignored = _manager(git_repo, store, tmp_path / "art2", make_git_config)
    assert gm_ignored.staged_pathspec(["a.py"]) == ["a.py"]


def test_commit_code_when_agent_staged_deletion(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    """Regression (F18): when the agent itself stages a delete/move (``git rm`` / ``git mv`` that
    git did not record as a rename), ``changed_code_paths`` reports the removed path, which is
    absent from the working tree and already fully in the index. ``git add -- <removed>`` would
    abort with exit 128 ("pathspec did not match any files"). ``commit_code`` must still pass, and
    the commit must contain both the deletion and the new file.
    """
    _task(store)
    # A tracked file exists on the branch base; commit it first so it can later be `git rm`-ed.
    (git_repo.clone / "old.py").write_text("old\n", encoding="utf-8")
    git_run(["add", "old.py"], git_repo.clone)
    git_run(["commit", "-m", "chore: add old.py"], git_repo.clone)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    # Agent stages a removal + creates a new untracked file (git does not see it as a rename).
    git_run(["rm", "old.py"], git_repo.clone)
    (git_repo.clone / "new.py").write_text("new\n", encoding="utf-8")

    sha = gm.commit_code("task-001", "feat: relocate module")
    assert sha is not None
    committed = git_run(["show", "--name-status", "--format=", "HEAD"], git_repo.clone)
    assert "D\told.py" in committed  # the staged deletion rode the commit
    assert "A\tnew.py" in committed  # and so did the new file


def test_commit_code_only_staged_deletion(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    """F18 edge: when the *only* change is a fully-staged deletion, the positive pathspec is empty,
    so ``git add`` is skipped and the commit is made straight from the index.
    """
    _task(store)
    (git_repo.clone / "gone.py").write_text("bye\n", encoding="utf-8")
    git_run(["add", "gone.py"], git_repo.clone)
    git_run(["commit", "-m", "chore: add gone.py"], git_repo.clone)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    git_run(["rm", "gone.py"], git_repo.clone)

    sha = gm.commit_code("task-001", "feat: drop module")
    assert sha is not None
    committed = git_run(["show", "--name-status", "--format=", "HEAD"], git_repo.clone)
    assert "D\tgone.py" in committed


def test_staged_pathspec_drops_fully_staged_deletion(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    """Unit (F18): a fully-staged deletion is excluded from the ``git add`` pathspec while ordinary
    modified/untracked code paths are kept.
    """
    (git_repo.clone / "keep.py").write_text("v1\n", encoding="utf-8")
    git_run(["add", "keep.py"], git_repo.clone)
    (git_repo.clone / "drop.py").write_text("v1\n", encoding="utf-8")
    git_run(["add", "drop.py"], git_repo.clone)
    git_run(["commit", "-m", "chore: seed"], git_repo.clone)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    # Stage a deletion of drop.py and an unstaged modification of keep.py.
    git_run(["rm", "drop.py"], git_repo.clone)
    (git_repo.clone / "keep.py").write_text("v2\n", encoding="utf-8")

    pathspec = gm.staged_pathspec(["keep.py", "drop.py"])
    assert "keep.py" in pathspec  # ordinary change stays
    assert "drop.py" not in pathspec  # fully-staged deletion dropped


def test_push_retries_transient_failure_then_succeeds(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    """F13: a transient git failure (here: ``.git/index.lock`` contention) is retried and then
    succeeds — no ``manual_action_required`` for a self-healing blip."""
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    slept: list[float] = []
    gm._sleep = slept.append  # type: ignore[assignment]
    calls = {"n": 0}
    real = gm._git_checked

    def flaky(*args: str) -> str:
        if args[:1] == ("push",):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GitCommandError("fatal: Unable to create '.git/index.lock': File exists")
        return real(*args)

    gm._git_checked = flaky  # type: ignore[assignment]
    assert gm._git_checked_retryable("push", "origin", "main") is not None
    assert calls["n"] == 2  # failed once, retried once, then succeeded
    assert slept == [_PUSH_RETRY_BACKOFF_SECONDS]


def test_push_does_not_retry_deterministic_failure(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    """F13 regression: a deterministic failure (non-fast-forward reject) is NOT retried — it must
    fail loudly and immediately so F12 surfaces the real cause, never be masked by retries."""
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    slept: list[float] = []
    gm._sleep = slept.append  # type: ignore[assignment]
    calls = {"n": 0}

    def rejecting(*args: str) -> str:
        calls["n"] += 1
        raise GitCommandError("! [rejected] main -> main (non-fast-forward)")

    gm._git_checked = rejecting  # type: ignore[assignment]
    with pytest.raises(GitCommandError, match="non-fast-forward"):
        gm._git_checked_retryable("push", "origin", "main")
    assert calls["n"] == 1  # no retry
    assert slept == []


def test_changed_code_paths_filters_artifacts(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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
        gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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


def test_ensure_runtime_excludes_writes_worc_line_to_local_exclude(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # The per-run fallback writes to the clone-local .git/info/exclude, never the tracked
    # .gitignore — so the runtime-home ignore never rides into a task's code commit / PR diff.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.ensure_runtime_excludes()
    gm.ensure_runtime_excludes()  # idempotent
    local_exclude = (git_repo.clone / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert local_exclude.count(".worc/") == 1
    # The tracked .gitignore is left untouched by the per-run fallback.
    gitignore = git_repo.clone / ".gitignore"
    assert not gitignore.exists() or ".worc/" not in gitignore.read_text(encoding="utf-8")


def test_ensure_runtime_excludes_respects_operators_flows_tracking_scheme(
    git_repo,
    store: StateStore,
    tmp_path: Path,
    make_git_config: ConfigFactory,
    git_run: GitRunner,
) -> None:
    # An operator who wants `.worc/flows/` tracked in git (docs/how-to.md) replaces the blanket
    # `.worc/` line with `.worc/*` + `!.worc/flows/`. The per-run fallback must not append a
    # blanket `.worc/` line on top of that — it would silently re-exclude `.worc/flows/`, since a
    # parent-directory exclusion from any source blocks re-inclusion of its children.
    (git_repo.clone / ".gitignore").write_text(".worc/*\n!.worc/flows/\n", encoding="utf-8")
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.ensure_runtime_excludes()
    local_exclude = git_repo.clone / ".git" / "info" / "exclude"
    assert not local_exclude.exists() or ".worc/" not in local_exclude.read_text(encoding="utf-8")
    # `.worc/flows/` stays trackable; a runtime-only path is still ignored.
    (git_repo.clone / ".worc" / "flows").mkdir(parents=True, exist_ok=True)
    flow_file = git_repo.clone / ".worc" / "flows" / "my_flow.yaml"
    flow_file.write_text("flow: {}\n", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        git_run(["check-ignore", "-q", str(flow_file.relative_to(git_repo.clone))], git_repo.clone)
    git_run(["check-ignore", "-q", ".worc/state.db"], git_repo.clone)


def test_append_runtime_excludes_respects_operators_flows_tracking_scheme(
    git_repo, git_run: GitRunner
) -> None:
    # `install --reconfigure` calls append_runtime_excludes() again; it must not append a blanket
    # `.worc/` line after an operator's own `.worc/*` + `!.worc/flows/` scheme (docs/how-to.md) —
    # that would silently re-exclude `.worc/flows/` from the tracked .gitignore. It still adds the
    # `.worc-io/` exchange line, which the operator scheme does not cover (WRI-001, per-root).
    gitignore = git_repo.clone / ".gitignore"
    gitignore.write_text(".worc/*\n!.worc/flows/\n", encoding="utf-8")
    appended = append_runtime_excludes(git_repo.clone)
    assert ".worc/" not in appended  # blanket line NOT re-appended (operator negation preserved)
    assert ".worc-io/" in appended  # exchange line added
    text = gitignore.read_text(encoding="utf-8")
    assert ".worc/*\n!.worc/flows/\n" in text  # operator scheme untouched
    assert ".worc-io/" in text
    (git_repo.clone / ".worc" / "flows").mkdir(parents=True, exist_ok=True)
    flow_file = git_repo.clone / ".worc" / "flows" / "my_flow.yaml"
    flow_file.write_text("flow: {}\n", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        git_run(["check-ignore", "-q", str(flow_file.relative_to(git_repo.clone))], git_repo.clone)
    # The exchange is now ignored (exit 0 = ignored).
    git_run(["check-ignore", "-q", ".worc-io/probe"], git_repo.clone)


def test_diff_stat_returns_stat_only(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # diff_stat() feeds the compact minimal summary: files + counts, never the patch body.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "mod.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    gm.commit_code("task-001", "feat: mod")
    stat = gm.diff_stat()
    assert "mod.py" in stat and "changed" in stat
    assert "diff --git" not in stat and "@@" not in stat


def test_changed_code_paths_since_base_includes_committed_change(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # The regression behind a decomposed subtask shipping unchecked code: once the change is
    # committed the working tree is clean, so the working-tree-only changed_code_paths() is empty —
    # but the base-inclusive changed_code_paths_since_base() still sees it, so the checks node can
    # still select its command sets.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "shipped.py").write_text("x = 1\n", encoding="utf-8")
    gm.commit_code("task-001", "feat: shipped")
    assert gm.changed_code_paths() == []  # clean working tree
    assert "shipped.py" in gm.changed_code_paths_since_base()


def test_changed_code_paths_since_base_includes_uncommitted_and_untracked(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # Covers the two-dot working-tree diff (a committed file edited again) plus a brand-new
    # untracked file (git diff never reports those — ls-files --others does).
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    gm.commit_code("task-001", "feat: tracked")
    (git_repo.clone / "tracked.py").write_text("x = 2\n", encoding="utf-8")  # uncommitted edit
    (git_repo.clone / "fresh.py").write_text("y = 1\n", encoding="utf-8")  # untracked
    paths = gm.changed_code_paths_since_base()
    assert "tracked.py" in paths and "fresh.py" in paths


def test_changed_code_paths_since_base_excludes_artifacts(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # Selection must never be driven by orchestration artifacts (.worc/, tasks/), same as the
    # staging set — otherwise an artifact write would trigger unrelated command sets.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "real.py").write_text("code\n", encoding="utf-8")
    for d in _DEFAULT_EXCLUDED_DIRS:
        (git_repo.clone / d).mkdir(exist_ok=True)
        (git_repo.clone / d / "junk.txt").write_text("x\n", encoding="utf-8")
    paths = gm.changed_code_paths_since_base()
    assert "real.py" in paths
    assert not any(p.startswith(tuple(f"{d}/" for d in _DEFAULT_EXCLUDED_DIRS)) for p in paths)


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
    branch = gm.prepare_branch("task-001", "x", epoch=_EPOCH)  # now on the task branch, not base
    gm.refresh_base()  # must not switch branches or pull onto an active task branch
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == branch


def test_audit_commit_commits_tasks_and_summary(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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


def test_configured_tasks_dir_drives_audit_and_code_exclusion(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # A custom `paths.tasks_dir` (here a repo-relative subpath) is what the audit commit stages and
    # what the code commit excludes — the literal "tasks/" must not be assumed anywhere.
    tasks_dir = "ops/worktasks"
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, tasks_dir=tasks_dir)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    # A code file plus junk under the configured tasks dir: the code commit must skip the tasks dir.
    (git_repo.clone / "src.py").write_text("print('hi')\n", encoding="utf-8")
    (git_repo.clone / tasks_dir / "pending").mkdir(parents=True, exist_ok=True)
    (git_repo.clone / tasks_dir / "pending" / "task-001.md").write_text("t\n", encoding="utf-8")
    code_sha = gm.commit_code("task-001", "feat: add src")
    assert code_sha is not None
    committed = git_run(["show", "--name-only", "--format=", "HEAD"], git_repo.clone).split()
    assert "src.py" in committed
    assert not any(f.startswith(f"{tasks_dir}/") for f in committed)

    # The lifecycle file + summary land in the configured dir; the audit commit stages those.
    done = git_repo.clone / tasks_dir / "done"
    done.mkdir(parents=True, exist_ok=True)
    (done / "task-001.md").write_text("t\n", encoding="utf-8")
    (done / "task-001.summary.md").write_text("s\n", encoding="utf-8")
    audit_sha = gm.commit_audit("task-001")
    assert audit_sha is not None
    tracked = git_run(["ls-files"], git_repo.clone)
    assert f"{tasks_dir}/done/task-001.md" in tracked
    assert f"{tasks_dir}/done/task-001.summary.md" in tracked


def test_audit_commit_noop_when_no_tasks(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # With nothing staged under tasks/, the audit commit is a no-op (returns None).
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    assert gm.commit_audit("task-001") is None


def test_audit_commit_stages_lifecycle_move_deletion(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # A task file already tracked under tasks/failed/ that is moved to tasks/done/ must have its
    # *deletion* staged too — otherwise the dangling removal rides back onto the base branch as a
    # `D` status after terminal cleanup (the task-023 regression).
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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


def test_audit_commit_stages_pending_to_failed_move_deletion(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # A task file tracked under tasks/pending/ (committed to base by hand) that the orchestrator
    # moves to tasks/failed/ on a failed run must have its *deletion* from pending staged too —
    # otherwise the dangling removal rides back onto the base branch as a `D` after terminal cleanup
    # (the ion-list regression: the prior fix covered only failed->done, not pending->failed/done).
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / "task-001.md").write_text("t\n", encoding="utf-8")
    git_run(["add", "tasks/pending/task-001.md"], git_repo.clone)
    git_run(["commit", "-m", "track pending task file"], git_repo.clone)
    # Lifecycle move: pending -> failed (the orchestrator's _relocate_task_file does this on disk).
    (pending / "task-001.md").unlink()
    failed = git_repo.clone / "tasks" / "failed"
    failed.mkdir(parents=True, exist_ok=True)
    (failed / "task-001.md").write_text("t\n", encoding="utf-8")
    (failed / "task-001.summary.md").write_text("s\n", encoding="utf-8")

    sha = gm.commit_audit("task-001")
    assert sha is not None
    tracked = git_run(["ls-files"], git_repo.clone)
    assert "tasks/pending/task-001.md" not in tracked
    assert "tasks/failed/task-001.md" in tracked
    # The working tree is clean: no dangling `D tasks/pending/task-001.md` to ride back to base.
    assert "tasks/pending/task-001.md" not in git_run(["status", "--porcelain"], git_repo.clone)


def test_snapshot_capture_and_partial_change(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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
    branch = gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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
        if list(argv[:2]) == ["pr", "list"]:  # the reuse probe: no open PR to reuse
            return GitResult(
                exit_code=0, stdout="[]\n", stderr="", timed_out=False, launch_error=None
            )
        return GitResult(
            exit_code=0,
            stdout="https://github.com/o/r/pull/1\n",
            stderr="",
            timed_out=False,
            launch_error=None,
        )

    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=fake_gh)
    branch = gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    body = tmp_path / "summary.md"
    body.write_text("# summary\n", encoding="utf-8")
    url = gm.create_pr("task-001", branch, title="My PR", body_path=str(body))
    assert url == "https://github.com/o/r/pull/1"
    # A reuse probe (`pr list`) then the create; the create carries the body file.
    assert [c[:2] for c in gh_calls] == [["pr", "list"], ["pr", "create"]]
    assert "--body-file" in gh_calls[1]
    url2 = gm.create_pr("task-001", branch, title="My PR", body_path=str(body))
    assert url2 == url
    assert len(gh_calls) == 2  # idempotent: gh not invoked again (publish-op short-circuits)


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
        if list(argv[1:3]) == ["pr", "list"]:  # the reuse probe: no open PR to reuse
            return GitResult(
                exit_code=0, stdout="[]\n", stderr="", timed_out=False, launch_error=None
            )
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

    gm.create_pr("task-001", "worc/task-001-x", title="My PR", body_path=str(body))

    create = next(c for c in calls if c[1:3] == ["pr", "create"])
    assert create[:3] == ["gh", "pr", "create"]  # `gh` executable added exactly once
    assert create.count("gh") == 1


def test_create_pr_disabled_returns_none(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, create_pr=False)
    branch = gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "README.md").write_text("changed\n", encoding="utf-8")  # tracked, uncommitted
    outcome = gm.terminal_cleanup("task-001")
    assert outcome.safe is False
    assert outcome.error is not None


def test_write_current_diff(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    art = tmp_path / "art"
    gm = _manager(git_repo, store, art, make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "README.md").write_text("changed\n", encoding="utf-8")
    path = gm.write_current_diff("task-001")
    assert Path(path).exists()
    assert "README.md" in Path(path).read_text(encoding="utf-8")


def test_write_current_diff_includes_committed_change(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # current.diff is the task change vs base, so an already-committed change (e.g. a decomposed
    # subtask) is captured — `git diff HEAD` would have shown nothing once committed, badly
    # understating the diff in the PR body / failure report.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "feature.py").write_text("value = 42\n", encoding="utf-8")
    gm.commit_code("task-001", "feat: feature")
    path = gm.write_current_diff("task-001")
    body = Path(path).read_text(encoding="utf-8")
    assert "feature.py" in body and "value = 42" in body


def test_write_current_diff_includes_untracked_file(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # F20: plain `git diff` never reports untracked files — a brand-new file must still show up
    # (full content), and the transient intent-to-add bracket must not leave it staged afterward.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "new_module.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    diff = Path(gm.write_current_diff("task-001")).read_text(encoding="utf-8")
    assert "new_module.py" in diff
    assert "return 1" in diff
    # The bracket restored the pre-existing untracked state — no persistent index mutation.
    entries = gm.changed_code_entries()
    assert any(e.status == "??" and e.path == "new_module.py" for e in entries)


def test_write_current_diff_renders_nul_content_as_text(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # F20: a NUL-delimited file trips Git's binary heuristic ("Binary files differ"), hiding the
    # actual change; `--text` forces the real diff content to appear instead.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    nul_path = git_repo.clone / "fixture.dat"
    nul_path.write_bytes(b"before\x00marker\n")
    git_run(["add", "fixture.dat"], git_repo.clone)
    git_run(["commit", "-m", "add nul fixture"], git_repo.clone)
    nul_path.write_bytes(b"after\x00marker\n")
    diff = Path(gm.write_current_diff("task-001")).read_text(encoding="utf-8")
    assert "Binary files differ" not in diff
    assert "after" in diff


def test_control_byte_paths_flags_only_nul_files(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # F35: a NUL byte makes a file git-binary (invisible to diff/review even with --text). The
    # detector surfaces the offending file so the recurrence is visible at the orchestrator level.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "clean.ts").write_text("const k = `${a} ${b}`;\n", encoding="utf-8")
    (git_repo.clone / "withnul.ts").write_bytes(b"const k = `${a}\x00${b}`;\n")
    assert gm.control_byte_paths() == ["withnul.ts"]


def test_control_byte_paths_clean_returns_empty(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "ok.py").write_text("x = 1\n", encoding="utf-8")
    assert gm.control_byte_paths() == []


def test_push_to_base_branch_is_refused(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    #: publishing is PR-only; a push aimed at base_branch is refused, never executed.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    with pytest.raises(GitCommandError):
        gm.push("task-001", "main")


# --- branch-mode: existing / current (branch-mode ADR) -----------------------------------


def test_prepare_branch_existing_checks_out_local_ref(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # `existing` mode works in an operator-owned local branch: plain checkout, no fresh branch.
    git_run(["branch", "feature/keep"], git_repo.clone)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    branch = gm.prepare_branch(
        "task-001", "x", epoch=_EPOCH, mode=BranchMode.EXISTING, branch_ref="feature/keep"
    )
    assert branch == "feature/keep"
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "feature/keep"


def test_prepare_branch_existing_creates_local_tracking_from_remote(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # Only a remote ref exists → create a local branch tracking origin/<ref> (never a reset).
    git_run(["checkout", "-b", "feature/remote-only"], git_repo.clone)
    (git_repo.clone / "r.txt").write_text("x\n", encoding="utf-8")
    git_run(["add", "r.txt"], git_repo.clone)
    git_run(["commit", "-m", "remote work"], git_repo.clone)
    git_run(["push", "-u", "origin", "feature/remote-only"], git_repo.clone)
    git_run(["checkout", "main"], git_repo.clone)
    git_run(["branch", "-D", "feature/remote-only"], git_repo.clone)  # local ref gone; remote stays

    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    branch = gm.prepare_branch(
        "task-001", "x", epoch=_EPOCH, mode=BranchMode.EXISTING, branch_ref="feature/remote-only"
    )
    assert branch == "feature/remote-only"
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "feature/remote-only"
    assert (git_repo.clone / "r.txt").exists()  # the remote work is present


def test_current_diff_on_chain_branch_excludes_prior_task(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # F32: on a shared chain branch (existing mode) the diff must show only THIS task's change, not
    # the prior task's commits already on the branch — review saw the whole cumulative chain before.
    git_run(["checkout", "-b", "feature/chain"], git_repo.clone)
    (git_repo.clone / "prior_task.py").write_text("prior = 1\n", encoding="utf-8")
    git_run(["add", "prior_task.py"], git_repo.clone)
    git_run(["commit", "-m", "feat(p1): prior task"], git_repo.clone)
    git_run(["checkout", "main"], git_repo.clone)

    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch(
        "task-002", "x", epoch=_EPOCH, mode=BranchMode.EXISTING, branch_ref="feature/chain"
    )
    (git_repo.clone / "this_task.py").write_text("this = 2\n", encoding="utf-8")
    diff = Path(gm.write_current_diff("task-002")).read_text(encoding="utf-8")
    assert "this_task.py" in diff  # this task's change is present
    assert "prior_task.py" not in diff  # the prior chain task is NOT (base = branch tip at start)


def test_changed_code_paths_since_task_base_excludes_prior_task_on_chain(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # F48: the memory packet's path relevance uses the per-task chain base, so on a shared chain
    # branch it returns only THIS task's files. The coarse base_branch variant still sees the whole
    # chain (right for check-set selection, wrong for packet ranking).
    git_run(["checkout", "-b", "feature/chain"], git_repo.clone)
    (git_repo.clone / "prior_task.py").write_text("prior = 1\n", encoding="utf-8")
    git_run(["add", "prior_task.py"], git_repo.clone)
    git_run(["commit", "-m", "feat(p1): prior task"], git_repo.clone)
    git_run(["checkout", "main"], git_repo.clone)

    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch(
        "task-002", "x", epoch=_EPOCH, mode=BranchMode.EXISTING, branch_ref="feature/chain"
    )
    (git_repo.clone / "this_task.py").write_text("this = 2\n", encoding="utf-8")

    task_scoped = gm.changed_code_paths_since_task_base()
    assert "this_task.py" in task_scoped  # this task's (untracked) change
    assert "prior_task.py" not in task_scoped  # committed before the chain base -> excluded
    # The coarse base_branch variant still sees the whole chain (unchanged behavior).
    assert set(gm.changed_code_paths_since_base()) >= {"this_task.py", "prior_task.py"}


def test_current_diff_new_mode_unchanged_uses_base_branch(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # new mode leaves base_ref None -> diffs vs base_branch exactly as before (no chain semantics).
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    assert gm._diff_base() == "main"


def test_prepare_branch_current_uses_head_without_switch_or_pull(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # `current` mode uses HEAD as-is: no switch, no pull, and a dirty tree is left untouched.
    git_run(["checkout", "-b", "operator/wip"], git_repo.clone)
    (git_repo.clone / "unrelated.txt").write_text("operator work\n", encoding="utf-8")  # dirty
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    branch = gm.prepare_branch("task-001", "x", epoch=_EPOCH, mode=BranchMode.CURRENT)
    assert branch == "operator/wip"
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "operator/wip"
    assert (git_repo.clone / "unrelated.txt").read_text(encoding="utf-8") == "operator work\n"


def test_push_to_base_allowed_in_current_mode(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # In `current`/`existing` mode pushing the working branch — even when it is the base — is the
    # legitimate head==base publish path, not a corrupted-state signal.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")
    gm.commit_code("task-001", "feat")
    assert gm.push("task-001", "main", mode=BranchMode.CURRENT) is True
    # the commit reached origin/main
    remote_log = git_run(["log", "--oneline", "origin/main"], git_repo.clone)
    assert "feat" in remote_log


def test_current_branch_none_when_detached(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    assert gm.current_branch() == "main"
    git_run(["checkout", "--detach", "HEAD"], git_repo.clone)
    assert gm.current_branch() is None


def test_local_or_remote_branch_exists(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    assert gm.local_or_remote_branch_exists("main") is True
    assert gm.local_or_remote_branch_exists("no-such-branch") is False
    git_run(["branch", "local-only"], git_repo.clone)
    assert gm.local_or_remote_branch_exists("local-only") is True


def test_terminal_cleanup_current_leaves_working_branch(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # `current` owns the operator's (possibly dirty) tree — cleanup must not force-checkout base.
    _task(store)
    git_run(["checkout", "-b", "operator/wip"], git_repo.clone)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH, mode=BranchMode.CURRENT)
    (git_repo.clone / "dirty.txt").write_text("keep me\n", encoding="utf-8")
    outcome = gm.terminal_cleanup("task-001", mode=BranchMode.CURRENT)
    assert outcome.safe is True
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "operator/wip"
    assert (git_repo.clone / "dirty.txt").exists()  # operator state preserved


def test_terminal_cleanup_existing_stays_on_branch_by_default(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # `existing` shares an operator-owned branch across tasks; by default cleanup stays on it.
    _task(store)
    git_run(["branch", "feature/keep"], git_repo.clone)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch(
        "task-001", "x", epoch=_EPOCH, mode=BranchMode.EXISTING, branch_ref="feature/keep"
    )
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")
    gm.commit_code("task-001", "feat")
    outcome = gm.terminal_cleanup("task-001", mode=BranchMode.EXISTING)
    assert outcome.safe is True
    assert outcome.target_branch == "feature/keep"
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "feature/keep"


def test_terminal_cleanup_flag_false_stays_on_new_branch(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # A global ``checkout_base_on_cleanup: false`` disables the switch-back even for a `new` branch.
    _task(store)
    gm = _manager(
        git_repo, store, tmp_path / "art", make_git_config, checkout_base_on_cleanup=False
    )
    branch = gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")
    gm.commit_code("task-001", "feat")
    outcome = gm.terminal_cleanup("task-001")  # default mode = NEW
    assert outcome.safe is True
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == branch


def test_terminal_cleanup_flag_true_returns_existing_to_base(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # Explicit ``checkout_base_on_cleanup: true`` forces even `existing` back to base.
    _task(store)
    git_run(["branch", "feature/keep"], git_repo.clone)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, checkout_base_on_cleanup=True)
    gm.prepare_branch(
        "task-001", "x", epoch=_EPOCH, mode=BranchMode.EXISTING, branch_ref="feature/keep"
    )
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")
    gm.commit_code("task-001", "feat")
    outcome = gm.terminal_cleanup("task-001", mode=BranchMode.EXISTING)
    assert outcome.safe is True
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"


def _reuse_gh(
    calls: list[list[str]],
    *,
    list_stdout: str,
    create_url: str = "https://x/pull/9",
    body_stdout: str = "",
) -> Callable[[Sequence[str]], GitResult]:
    def gh(argv: Sequence[str]) -> GitResult:
        calls.append(list(argv))
        verb = list(argv[:2])
        if verb == ["pr", "list"]:
            stdout = list_stdout
        elif verb == ["pr", "view"]:  # reused-PR body probe (F27 append)
            stdout = body_stdout
        elif verb == ["pr", "edit"]:  # reused-PR body append (F27)
            stdout = ""
        else:
            stdout = f"{create_url}\n"
        return GitResult(exit_code=0, stdout=stdout, stderr="", timed_out=False, launch_error=None)

    return gh


def test_create_pr_skips_when_head_equals_base(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # head == pr_base: a PR is impossible, so create_pr skips (returns None), never touching gh.
    _task(store)
    calls: list[list[str]] = []
    gh = _reuse_gh(calls, list_stdout="[]")
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    assert gm.create_pr("task-001", "main", title="t", body_path="x") is None
    assert calls == []  # neither the reuse probe nor create runs
    assert store.get_publish_op("task-001", "pr") is None


def test_create_pr_reuses_open_pr(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # A chain of tasks on one branch converges on one PR: an already-open head→base PR is reused.
    _task(store)
    calls: list[list[str]] = []
    gh = _reuse_gh(calls, list_stdout='[{"url": "https://x/pull/7", "updatedAt": "2026-01-01"}]')
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    url = gm.create_pr("task-001", "feature/shared", title="t", body_path="x")
    assert url == "https://x/pull/7"
    # reused; no `pr create` — but the body probe + append run (F27).
    assert [c[:2] for c in calls] == [["pr", "list"], ["pr", "view"], ["pr", "edit"]]
    op = store.get_publish_op("task-001", "pr")
    assert op is not None and op.result_ref == "https://x/pull/7"


def test_create_pr_reuse_appends_task_keyed_section(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # F27: a reused chain PR keeps task 1's title/body; append this task's section under it, keyed
    # by a task-id marker, so the PR reflects the whole chain instead of only its first task.
    _task(store)
    calls: list[list[str]] = []
    body = tmp_path / "summary.md"
    body.write_text("This task added the query layer.\n", encoding="utf-8")
    gh = _reuse_gh(
        calls,
        list_stdout='[{"url": "https://x/pull/9", "updatedAt": "2026-01-01"}]',
        body_stdout="Original body (task 1).",
    )
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    gm.create_pr("task-001", "feature/shared", title="P4.02 — Query", body_path=str(body))

    edits = [c for c in calls if c[:2] == ["pr", "edit"]]
    assert len(edits) == 1
    written = Path(edits[0][edits[0].index("--body-file") + 1]).read_text(encoding="utf-8")
    assert "Original body (task 1)." in written  # prior content preserved, not overwritten
    assert "<!-- worc-task:task-001 -->" in written  # keyed for idempotency
    assert "## P4.02 — Query" in written
    assert "This task added the query layer." in written


def test_create_pr_reuse_append_is_idempotent(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # A rerun whose section marker is already in the body must not append a second copy.
    _task(store)
    calls: list[list[str]] = []
    gh = _reuse_gh(
        calls,
        list_stdout='[{"url": "https://x/pull/9", "updatedAt": "2026-01-01"}]',
        body_stdout="Body\n\n<!-- worc-task:task-001 -->\n\n## P4.02\n\nalready here",
    )
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    gm.create_pr("task-001", "feature/shared", title="P4.02", body_path="x")
    assert not any(c[:2] == ["pr", "edit"] for c in calls)  # marker present → no re-append


def test_create_pr_reuse_picks_most_recent_of_multiple(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    calls: list[list[str]] = []
    rows = (
        '[{"url": "https://x/pull/3", "updatedAt": "2026-01-01"},'
        ' {"url": "https://x/pull/8", "updatedAt": "2026-02-01"}]'
    )
    gh = _reuse_gh(calls, list_stdout=rows)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    url = gm.create_pr("task-001", "feature/shared", title="t", body_path="x")
    assert url == "https://x/pull/8"  # most recent by updatedAt


def test_create_pr_creates_new_when_no_open_pr(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # No open PR (closed/merged are filtered by `--state open`) → proceed to create a fresh one.
    _task(store)
    calls: list[list[str]] = []
    gh = _reuse_gh(calls, list_stdout="[]")
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config, gh_runner=gh)
    url = gm.create_pr("task-001", "feature/x", title="t", body_path="x")
    assert url == "https://x/pull/9"
    assert [c[:2] for c in calls] == [["pr", "list"], ["pr", "create"]]


def test_current_diff_is_redacted(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    #: current.diff (the failure report reads it back) must carry no secrets — token-shaped
    # ones via pattern, denied_read_paths values via the content-scan seed.
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
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
    git_run(["branch", "worc/task-001-x"], git_repo.clone)
    assert gm.delete_branch("worc/task-001-x") is True  # deleted
    assert gm.delete_branch("worc/task-001-x") is False  # already gone — no-op


def test_files_in_commit_lists_changed_paths_posix(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # The subtask handoff floor names a predecessor commit's changed files. Paths come back
    # git-posix (forward slashes) regardless of host OS, so the assertion holds cross-platform.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    (git_repo.clone / "a.txt").write_text("a\n", encoding="utf-8")
    (git_repo.clone / "pkg").mkdir()
    (git_repo.clone / "pkg" / "b.txt").write_text("b\n", encoding="utf-8")
    git_run(["add", "a.txt", "pkg/b.txt"], git_repo.clone)
    git_run(["commit", "-m", "add two files"], git_repo.clone)
    sha = git_run(["rev-parse", "HEAD"], git_repo.clone)

    files = gm.files_in_commit(sha)
    assert set(files) == {"a.txt", "pkg/b.txt"}
    assert all("\\" not in f for f in files)  # git yields posix separators


def test_files_in_commit_bad_sha_returns_empty(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    # Best-effort: an unknown/malformed sha yields [] rather than raising, so the handoff
    # degrades to the rest of the floor.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    assert gm.files_in_commit("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef") == []


# --- Unicode / unusual-path Git parsing hardening --------------------------------------
#
# Git's default text output C-quotes non-ASCII/space/quote paths (e.g. a Cyrillic filename becomes
# `"\320\274..."`), which broke `git add -- <path>` on a real publish. These use literal non-ASCII
# names (never escaped literals) so a parsing regression shows up as a real assertion failure.


def _show_paths(git_run: GitRunner, clone: Path, ref: str = "HEAD") -> set[str]:
    """The literal (unescaped) paths committed at ``ref``, via ``-z`` inspection."""
    raw = git_run(["show", "-z", "--name-only", "--format=", ref], clone)
    return {p for p in raw.split("\0") if p}


def test_commit_code_untracked_unicode_file(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    unicode_name = "20260206-моя-история-(EN).md"
    (git_repo.clone / unicode_name).write_text("привет\n", encoding="utf-8")

    sha = gm.commit_code("task-001", "feat: add unicode file")
    assert sha is not None
    assert _show_paths(git_run, git_repo.clone) == {unicode_name}


def test_commit_code_modified_tracked_unicode_file(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    unicode_name = "документ.md"
    (git_repo.clone / unicode_name).write_text("v1\n", encoding="utf-8")
    git_run(["add", "--", unicode_name], git_repo.clone)
    git_run(["commit", "-m", "chore: seed unicode file"], git_repo.clone)

    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / unicode_name).write_text("v2\n", encoding="utf-8")

    sha = gm.commit_code("task-001", "feat: update unicode file")
    assert sha is not None
    assert _show_paths(git_run, git_repo.clone) == {unicode_name}


def test_commit_code_succeeds_with_unicode_rename(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # The exact shape of the incident: a staged rename whose destination is non-ASCII must still
    # reach `git add -- <path>` with the real UTF-8 path, not a C-quoted escape.
    old_name = "старое.md"
    (git_repo.clone / old_name).write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
    git_run(["add", "--", old_name], git_repo.clone)
    git_run(["commit", "-m", "chore: seed rename source"], git_repo.clone)

    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    new_name = "20260206-моя-история-(EN).md"
    git_run(["mv", old_name, new_name], git_repo.clone)

    sha = gm.commit_code("task-001", "feat: rename to unicode name")
    assert sha is not None
    assert _show_paths(git_run, git_repo.clone) == {new_name}


def test_changed_code_entries_returns_literal_unicode_paths(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    tracked_name = "трекнутый.py"
    (git_repo.clone / tracked_name).write_text("x = 1\n", encoding="utf-8")
    git_run(["add", "--", tracked_name], git_repo.clone)
    git_run(["commit", "-m", "chore: seed tracked unicode file"], git_repo.clone)

    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / tracked_name).write_text("x = 2\n", encoding="utf-8")
    untracked_name = "новый-файл.py"
    (git_repo.clone / untracked_name).write_text("y = 1\n", encoding="utf-8")

    paths = {e.path for e in gm.changed_code_entries()}
    assert tracked_name in paths
    assert untracked_name in paths


def test_changed_code_entries_unicode_rename(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # `--name-status -z` keeps the old-then-new field order (unlike `status --porcelain -z`, which
    # reverses it) — the parser must consume both tokens as one record, not split them into two
    # unrelated entries.
    old_name = "старое-имя.txt"
    (git_repo.clone / old_name).write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
    git_run(["add", "--", old_name], git_repo.clone)
    git_run(["commit", "-m", "chore: seed rename source"], git_repo.clone)

    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    new_name = "новое имя (v2).txt"
    git_run(["mv", old_name, new_name], git_repo.clone)

    entries = gm.changed_code_entries()
    renamed = next(e for e in entries if e.status.startswith("R"))
    assert renamed.path == new_name
    assert renamed.previous_path == old_name
    # The staging set (`status --porcelain -z`) reports the rename's destination, not its source.
    assert new_name in gm.changed_code_paths()
    assert old_name not in gm.changed_code_paths()


def test_changed_code_paths_since_base_returns_literal_unicode_paths(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    committed_name = "коммит.py"
    (git_repo.clone / committed_name).write_text("x = 1\n", encoding="utf-8")
    gm.commit_code("task-001", "feat: unicode")
    untracked_name = "черновик.py"
    (git_repo.clone / untracked_name).write_text("y = 1\n", encoding="utf-8")

    since_base = gm.changed_code_paths_since_base()
    assert committed_name in since_base
    assert untracked_name in since_base
    since_task_base = gm.changed_code_paths_since_task_base()
    assert committed_name in since_task_base
    assert untracked_name in since_task_base


def test_files_in_commit_returns_literal_unicode_paths(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    unicode_name = "файл-в-коммите.txt"
    (git_repo.clone / unicode_name).write_text("a\n", encoding="utf-8")
    git_run(["add", "--", unicode_name], git_repo.clone)
    git_run(["commit", "-m", "add unicode file"], git_repo.clone)
    sha = git_run(["rev-parse", "HEAD"], git_repo.clone)

    assert gm.files_in_commit(sha) == [unicode_name]


def test_unaccounted_dirty_paths_reports_literal_unicode_path(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # Cleanup diagnostics (the terminal-cleanup fail-closed gate) must name the real path, not a
    # C-quoted escape, so an operator reading the error can actually find the file.
    unicode_name = "незафиксированный.md"
    (git_repo.clone / unicode_name).write_text("v1\n", encoding="utf-8")
    git_run(["add", "--", unicode_name], git_repo.clone)
    git_run(["commit", "-m", "chore: seed"], git_repo.clone)

    _task(store)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    (git_repo.clone / unicode_name).write_text("v2 (uncommitted)\n", encoding="utf-8")

    assert gm.unaccounted_dirty_paths() == {unicode_name}
    outcome = gm.terminal_cleanup("task-001")
    assert outcome.safe is False
    assert unicode_name in (outcome.error or "")


# --- WRI-009: Git control-state fingerprint -------------------------------------------------


def _armed(git_repo, store, artifacts, make_git_config) -> GitManager:
    """A manager with a task branch attached (so control-state capture has a task ref)."""
    _task(store)
    gm = _manager(git_repo, store, artifacts, make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    return gm


def test_control_state_detects_force_added_exchange_file(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    before = gm.capture_git_control_state()
    (git_repo.clone / ".worc-io").mkdir(exist_ok=True)
    (git_repo.clone / ".worc-io" / "plan.md").write_text("secret\n", encoding="utf-8")
    git_run(["add", "-f", ".worc-io/plan.md"], git_repo.clone)  # provider force-stages the exchange

    drift = gm.compare_git_control_state(before)
    assert drift is not None
    assert any(item.aspect == "index" for item in drift.items)
    assert ".worc-io/plan.md" in drift.summary()


def test_control_state_detects_config_change_without_leaking_value(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    before = gm.capture_git_control_state()
    git_run(["config", "sneaky.key", "SUPERSECRETVALUE"], git_repo.clone)

    drift = gm.compare_git_control_state(before)
    assert drift is not None
    summary = drift.summary()
    assert "sneaky.key" in summary  # the key name is reported
    assert (
        "SUPERSECRETVALUE" not in summary
    )  # the value never is (redaction / hash-only fingerprint)


def test_control_state_detects_head_and_ref_move(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    before = gm.capture_git_control_state()
    git_run(["commit", "--allow-empty", "-m", "provider sneaked a commit"], git_repo.clone)

    drift = gm.compare_git_control_state(before)
    assert drift is not None
    assert any(item.aspect in ("head", "task_ref") for item in drift.items)


def test_control_state_detects_operation_marker(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    before = gm.capture_git_control_state()
    head = git_run(["rev-parse", "HEAD"], git_repo.clone)
    (git_repo.clone / ".git" / "MERGE_HEAD").write_text(head + "\n", encoding="utf-8")

    drift = gm.compare_git_control_state(before)
    assert drift is not None
    assert any(item.aspect == "markers" for item in drift.items)


def test_control_state_detects_intent_to_add(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    before = gm.capture_git_control_state()
    (git_repo.clone / "sneaky.py").write_text("x\n", encoding="utf-8")
    git_run(["add", "-N", "sneaky.py"], git_repo.clone)  # intent-to-add: zero-sha index entry

    drift = gm.compare_git_control_state(before)
    assert drift is not None
    assert any(item.aspect == "index" for item in drift.items)


def test_control_state_clean_working_tree_edit_is_not_drift(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    before = gm.capture_git_control_state()
    # An ordinary working-tree edit is the *point* of the run — it must not be flagged as drift.
    (git_repo.clone / "app.py").write_text("print('edited by the agent')\n", encoding="utf-8")

    assert gm.compare_git_control_state(before) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell hook + chmod; Windows covered by WRI-006")
def test_control_state_detects_installed_hook(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    before = gm.capture_git_control_state()
    hook = git_repo.clone / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)

    drift = gm.compare_git_control_state(before)
    assert drift is not None
    assert any(item.aspect == "hooks" and "pre-commit" in item.detail for item in drift.items)


# --- WRI-009: full staged-set gate at commit time -------------------------------------------


def test_code_commit_rejects_force_added_exchange_file(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")  # a legitimate change
    (git_repo.clone / ".worc-io").mkdir(exist_ok=True)
    (git_repo.clone / ".worc-io" / "plan.md").write_text("secret\n", encoding="utf-8")
    git_run(["add", "-f", ".worc-io/plan.md"], git_repo.clone)

    with pytest.raises(ManualActionRequired):
        gm.commit_code("task-001", "feat")
    # The gate fired before `git commit`, so no commit was made and the exchange file (still staged)
    # never reached the committed tree.
    assert ".worc-io/plan.md" not in _show_paths(git_run, git_repo.clone)


def test_code_commit_rejects_foreign_tasks_file(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")
    (git_repo.clone / "tasks" / "pending").mkdir(parents=True, exist_ok=True)
    (git_repo.clone / "tasks" / "pending" / "task-999.md").write_text(
        "injected\n", encoding="utf-8"
    )
    git_run(["add", "tasks/pending/task-999.md"], git_repo.clone)  # a self-injected task file

    with pytest.raises(ManualActionRequired):
        gm.commit_code("task-001", "feat")


def test_audit_commit_rejects_foreign_task_lifecycle_file(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    done = git_repo.clone / "tasks" / "done"
    done.mkdir(parents=True, exist_ok=True)
    (done / "task-001.md").write_text("t\n", encoding="utf-8")  # this task's lifecycle file
    (done / "task-001.summary.md").write_text("s\n", encoding="utf-8")
    (done / "task-999.md").write_text("another task\n", encoding="utf-8")
    git_run(
        ["add", "tasks/done/task-999.md"], git_repo.clone
    )  # a foreign task swept into the index

    with pytest.raises(ManualActionRequired):
        gm.commit_audit("task-001")


# --- WRI-009: audit-commit lifecycle digest check -------------------------------------------


def test_audit_commit_accepts_matching_task_packet_digest(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    done = git_repo.clone / "tasks" / "done"
    done.mkdir(parents=True, exist_ok=True)
    lifecycle = done / "task-001.md"
    lifecycle.write_text("the authorized task body\n", encoding="utf-8")
    (done / "task-001.summary.md").write_text("s\n", encoding="utf-8")
    digest = sha256_file(lifecycle)

    sha = gm.commit_audit("task-001", task_packet_digest=digest)
    assert sha is not None


def test_audit_commit_refuses_rewritten_task_file(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    done = git_repo.clone / "tasks" / "done"
    done.mkdir(parents=True, exist_ok=True)
    (done / "task-001.md").write_text("REWRITTEN by the agent\n", encoding="utf-8")
    (done / "task-001.summary.md").write_text("s\n", encoding="utf-8")

    # The frozen packet digest is of the *original* task body, so the rewritten file mismatches.
    with pytest.raises(ManualActionRequired):
        gm.commit_audit("task-001", task_packet_digest="0" * 64)


# --- WRI-009: clean-index preflight (branch-mode baseline contract) -------------------------


def test_prepare_branch_refuses_pre_staged_baseline(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    _task(store)
    (git_repo.clone / "operator.py").write_text("pre-staged\n", encoding="utf-8")
    git_run(["add", "operator.py"], git_repo.clone)  # an operator/agent staged baseline
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)

    with pytest.raises(ManualActionRequired):
        gm.prepare_branch("task-001", "x", epoch=_EPOCH)


def test_prepare_branch_allows_unstaged_dirty_tree(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    _task(store)
    (git_repo.clone / "wip.py").write_text("unstaged wip\n", encoding="utf-8")  # not staged
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)

    branch = gm.prepare_branch("task-001", "x", epoch=_EPOCH)  # must not refuse
    assert branch


# --- WRI-009: git-subprocess neutralization -------------------------------------------------


def test_commit_refuses_untrusted_repo_local_filter_driver(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    # An agent-editable repo-local clean filter pointing at a program: refuse before any git that
    # would run it (the operator-authorized-filter allowlist is a deferred follow-up).
    git_run(["config", "filter.evil.clean", "/bin/false"], git_repo.clone)
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")

    with pytest.raises(ManualActionRequired):
        gm.commit_code("task-001", "feat")


def test_reset_branch_to_base_refuses_untrusted_filter_driver(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # M5: reset-to-base (fresh rerun) checks out base + pulls → runs smudge filters. A repo-local
    # program-launching driver must refuse before that git runs, exactly like commit/checkout.
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    git_run(["config", "filter.evil.smudge", "/bin/false"], git_repo.clone)
    with pytest.raises(ManualActionRequired):
        gm.reset_branch_to_base("task-001", "x", branch_name="worc/x")


def test_terminal_cleanup_refuses_untrusted_filter_driver(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # M5: terminal cleanup checks out base → runs smudge filters. This method reports outcomes
    # (never raises), so a poisoned driver leaves the slot fail-closed rather than crashing.
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    git_run(["config", "filter.evil.smudge", "/bin/false"], git_repo.clone)
    outcome = gm.terminal_cleanup("task-001", mode=BranchMode.NEW)
    assert outcome.safe is False
    assert "untrusted" in (outcome.error or "")


def test_refresh_base_skips_pull_on_untrusted_filter_driver(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # M5: refresh_base runs in the watch loop and must NOT raise (that would crash the loop); a
    # poisoned repo-local driver skips the fetch/pull (leaving the working copy untouched) so the
    # smudge filter never runs. Prove the pull was skipped: advance the remote, then assert the
    # clone's base did not move.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    git_run(["checkout", "main"], git_repo.clone)
    before = git_run(["rev-parse", "HEAD"], git_repo.clone)
    # A second clone advances origin/main so an unfiltered refresh WOULD fast-forward.
    other = tmp_path / "other"
    git_run(["clone", str(git_repo.remote), str(other)], tmp_path)
    git_run(["config", "user.email", "t@e.com"], other)
    git_run(["config", "user.name", "T"], other)
    (other / "new.txt").write_text("x\n", encoding="utf-8")
    git_run(["add", "new.txt"], other)
    git_run(["commit", "-m", "advance"], other)
    git_run(["push", "origin", "main"], other)

    git_run(["config", "filter.evil.smudge", "/bin/false"], git_repo.clone)
    gm.refresh_base()  # must not raise
    assert git_run(["rev-parse", "HEAD"], git_repo.clone) == before  # pull was skipped


def test_commit_succeeds_despite_agent_signing_config(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    # An agent-set repo signing config with a bogus program: without the `-c commit.gpgsign=false`
    # override the commit would try to launch it and fail. Signing is force-disabled, so it doesn't.
    git_run(["config", "commit.gpgsign", "true"], git_repo.clone)
    git_run(["config", "gpg.program", "/nonexistent/gpg-bin"], git_repo.clone)
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")

    sha = gm.commit_code("task-001", "feat")
    assert sha is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell hook + chmod; Windows covered by WRI-006")
def test_repo_pre_commit_hook_does_not_run_during_orchestrator_commit(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    # A repo hook that, if it ran, would fail the commit (and leave a sentinel). The `-c
    # core.hooksPath=<empty>` neutralization points git at an empty dir, so it never runs.
    sentinel = tmp_path / "hook-ran"
    hook = git_repo.clone / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel.as_posix()}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")

    sha = gm.commit_code("task-001", "feat")
    assert sha is not None  # the failing hook did not run, so the commit succeeded
    assert not sentinel.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell hook + chmod; Windows covered by WRI-006")
def test_agent_hookspath_target_does_not_run(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _armed(git_repo, store, tmp_path / "art", make_git_config)
    # An agent that redirects core.hooksPath at its own hook dir is defeated: the command-line
    # `-c core.hooksPath` overrides the repo-set value. The hook dir lives outside the clone so it
    # is not itself part of the staged change.
    evil_hooks = tmp_path / "evil-hooks"
    evil_hooks.mkdir()
    sentinel = tmp_path / "evil-hook-ran"
    hook = evil_hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel.as_posix()}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    git_run(["config", "core.hooksPath", str(evil_hooks)], git_repo.clone)
    (git_repo.clone / "src.py").write_text("x\n", encoding="utf-8")

    sha = gm.commit_code("task-001", "feat")
    assert sha is not None
    assert not sentinel.exists()


# --- WRI-002: resolve_control_paths (provider Write/Edit-deny roots)
# -------------------------------


def test_resolve_control_paths_normal_clone(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    wg = gm.resolve_control_paths("/repo/.worc-io")
    clone = Path(git_repo.clone)
    dot_git = (clone / ".git").resolve()
    # A normal clone collapses the gitdir and common dir onto <clone>/.git.
    assert wg.git_dir.resolve() == dot_git
    assert wg.git_common_dir.resolve() == dot_git
    assert wg.hooks_dir.resolve() == (clone / ".git" / "hooks").resolve()
    assert wg.tasks_dir == clone / "tasks"
    assert wg.exchange_root == Path("/repo/.worc-io")
    # denied_write_paths de-dups git_dir==git_common_dir and includes exchange + tasks.
    resolved = {p.resolve() for p in wg.denied_write_paths}
    assert dot_git in resolved
    assert Path("/repo/.worc-io").resolve() in resolved
    assert (clone / "tasks").resolve() in resolved


def test_resolve_control_paths_linked_worktree_splits_gitdir_and_common(
    git_repo,
    store: StateStore,
    tmp_path: Path,
    make_git_config: ConfigFactory,
    git_run: GitRunner,
) -> None:
    # A linked worktree has a per-worktree gitdir distinct from the shared common dir; BOTH must be
    # denied so the sandbox's built-in "shared .git is writable in a linked worktree" allowance is
    # overridden.
    wt = tmp_path / "wt"
    git_run(["worktree", "add", "-b", "wt-branch", str(wt)], git_repo.clone)
    config = make_git_config(str(wt))
    gm = GitManager(config, store=store, artifacts_root=str(tmp_path / "art2"))
    wg = gm.resolve_control_paths()
    assert wg.git_dir.resolve() != wg.git_common_dir.resolve()
    assert wg.git_common_dir.resolve() == (Path(git_repo.clone) / ".git").resolve()
    # Both distinct roots appear in the write-deny set.
    resolved = {p.resolve() for p in wg.denied_write_paths}
    assert wg.git_dir.resolve() in resolved and wg.git_common_dir.resolve() in resolved
