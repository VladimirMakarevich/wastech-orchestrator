"""Integration tests for the operator-driven merge routine (``worc merge-task``).

Drives ``Orchestrator.merge_task`` against a real temporary git repo (real GitManager) with a fake
``gh`` runner and fake provider CLIs, covering the clean / conflict-resolve / conflict-fail /
idempotent / refuse-active / no-resolve / status-flip paths.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.composition import build_orchestrator
from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import MergeStrategy
from wastech_orchestrator.core.orchestrator import PipelineFailed
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import KIND_PR, KIND_PR_MERGE, GitResult
from wastech_orchestrator.state_store import PublishOpRow, StateStore, TaskRow

GitRunner = Callable[[Sequence[str], Path], str]
_ENV = [
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "GIT_EXEC_PATH",
]
_URL = "https://github.com/o/r/pull/1"
_BRANCH = "worc/m1"


def _result(stdout: str = "", code: int = 0) -> GitResult:
    return GitResult(exit_code=code, stdout=stdout, stderr="", timed_out=False, launch_error=None)


class FakeGh:
    """A minimal ``gh`` stand-in: reports a PR state and records whether ``pr merge`` was called."""

    def __init__(self, state: str = "OPEN") -> None:
        self.state = state
        self.merge_called = False

    def __call__(self, args: Sequence[str]) -> GitResult:
        a = list(args)
        if a[:2] == ["pr", "merge"]:
            self.merge_called = True
            return _result()
        if a[:2] == ["pr", "view"]:
            if "-q" in a and ".state" in a:
                return _result(self.state)
            return _result("")  # mergeCommit probe / pr_merge_state → no sha
        return _result(code=1)


def _config(clone: Path, claude_cmd: str, codex_cmd: str) -> object:
    env_lines = "\n".join(f"    - {e}" for e in _ENV)
    text = f"""
repo:
  url: "git@example.com:o/r.git"
  local_path: {str(clone)!r}
  base_branch: "main"
  branch_prefix: "worc"
agents:
  allowed: [claude, codex]
  providers:
    claude:
      command: {claude_cmd!r}
      primary: true
    codex:
      command: {codex_cmd!r}
security:
  allowed_environment:
{env_lines}
checks:
  command_sets: {{}}
  timeout_seconds: 30
git:
  create_pull_request: true
  pr_base: "main"
"""
    return loads_config(text).config


def _seed_task(store: StateStore, status: Status = Status.DONE) -> None:
    store.insert_task(TaskRow(task_id="m1", title="merge me", status=status, branch=_BRANCH))
    store.record_publish_op(
        PublishOpRow(
            task_id="m1", kind=KIND_PR, fingerprint=_BRANCH, status="completed", result_ref=_URL
        )
    )


def _setup_branch(git_run: GitRunner, clone: Path, *, conflict: bool) -> None:
    """Create ``worc/m1`` with a committed change + push it, then advance origin/main.

    ``conflict``: base and branch edit the same file (README.md) → a conflicting base-merge.
    Otherwise they edit different files → a clean base-merge.
    """
    git_run(["checkout", "-b", _BRANCH, "main"], clone)
    target = "README.md" if conflict else "feature.txt"
    (clone / target).write_text("branch side\n", encoding="utf-8")
    git_run(["add", target], clone)
    git_run(["commit", "-m", "task change"], clone)
    git_run(["push", "-u", "origin", _BRANCH], clone)
    git_run(["checkout", "main"], clone)
    base_target = "README.md" if conflict else "BASE.md"
    (clone / base_target).write_text("base side\n", encoding="utf-8")
    git_run(["add", base_target], clone)
    git_run(["commit", "-m", "base change"], clone)
    git_run(["push", "origin", "main"], clone)


def _build(git_repo, fake_cli, tmp_path: Path, *, scenario: str, gh: FakeGh):
    from tests.conftest import seed_builtin_flows

    claude = fake_cli(scenario, "claude")
    codex = fake_cli(scenario, "codex")
    config = _config(git_repo.clone, claude, codex)
    seed_builtin_flows(
        git_repo.clone
    )  # deliver the built-in flows (incl. `merge`) as install would
    return build_orchestrator(config, artifacts_root=tmp_path / "art", gh_runner=gh)


def test_clean_base_merge_merges(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=False)

    result = orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    assert result.final_status is Status.DONE
    assert gh.merge_called is True
    assert orch._git.merge_in_progress() is False
    assert orch._store.get_publish_op("m1", KIND_PR_MERGE, None) is not None


def test_conflict_resolved_by_flow_then_merges(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="resolve_conflicts", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=True)

    result = orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    assert result.final_status is Status.DONE
    assert gh.merge_called is True
    assert orch._git.merge_in_progress() is False
    # The merge commit (markers resolved) was pushed to the remote branch.
    assert "<<<<<<<" not in (git_repo.clone / "README.md").read_text(encoding="utf-8")


def test_conflict_unresolved_aborts_and_keeps_pr_open(
    git_repo, fake_cli, git_run, tmp_path: Path
) -> None:
    gh = FakeGh("OPEN")
    # ``success`` edits nothing, so the conflict markers survive the flow.
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=True)

    with pytest.raises(PipelineFailed):
        orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    assert gh.merge_called is False  # never reached the merge
    assert orch._git.merge_in_progress() is False  # the conflict path aborted the merge
    assert orch._store.get_task("m1").status is Status.DONE  # DONE never downgraded
    assert orch._store.get_publish_op("m1", KIND_PR_MERGE, None) is None


def test_already_merged_is_idempotent(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    gh = FakeGh("MERGED")  # the PR was merged out of band already
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)

    result = orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    assert result.final_status is Status.DONE
    assert gh.merge_called is False  # no re-merge
    assert orch._store.get_publish_op("m1", KIND_PR_MERGE, None) is not None  # recorded


def test_refuses_when_another_task_active(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)
    orch._store.insert_task(TaskRow(task_id="other", title="busy", status=Status.RUNNING))

    with pytest.raises(PipelineFailed, match="owns the processing slot"):
        orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)
    assert gh.merge_called is False


def test_no_resolve_aborts_on_conflict(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=True)

    with pytest.raises(PipelineFailed, match="--no-resolve"):
        orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False, resolve=False)

    assert gh.merge_called is False
    assert orch._git.merge_in_progress() is False


def test_manual_action_required_flips_to_done(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store, status=Status.MANUAL_ACTION_REQUIRED)
    _setup_branch(git_run, git_repo.clone, conflict=False)

    result = orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    assert result.final_status is Status.DONE
    assert orch._store.get_task("m1").status is Status.DONE  # flipped via finalize


def test_sync_dry_run_writes_nothing(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    gh = FakeGh("MERGED")  # the PR was merged directly on GitHub
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)

    entries = orch.sync_external_merges(write=False)

    assert [e.action for e in entries] == ["record-merge"]
    assert orch._store.get_publish_op("m1", KIND_PR_MERGE, None) is None  # dry-run wrote nothing


def test_sync_write_records_and_is_idempotent(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    gh = FakeGh("MERGED")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)

    entries = orch.sync_external_merges(write=True)

    assert [e.action for e in entries] == ["record-merge"]
    assert entries[0].finalized_done is False  # a DONE task is not re-finalized
    assert orch._store.get_publish_op("m1", KIND_PR_MERGE, None) is not None
    # Idempotent: the task now has a pr_merge op, so it drops out of the open-PR set.
    assert orch.sync_external_merges(write=True) == []


def test_sync_closed_pr_no_change(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    gh = FakeGh("CLOSED")  # closed without merging
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)

    entries = orch.sync_external_merges(write=True)

    assert [e.action for e in entries] == ["closed-no-merge"]
    assert orch._store.get_publish_op("m1", KIND_PR_MERGE, None) is None


def test_plan_merge_is_read_only(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)

    plan = orch.plan_merge("m1")

    assert plan.found is True
    assert plan.pr_url == _URL
    assert plan.verify_state == "OPEN"
    assert not plan.refusals
    assert gh.merge_called is False
    assert orch._store.get_publish_op("m1", KIND_PR_MERGE, None) is None  # nothing written
