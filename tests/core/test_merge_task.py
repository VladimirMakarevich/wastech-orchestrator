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
from wastech_orchestrator.core.flow.nodes.base import NodeManualRequired
from wastech_orchestrator.core.orchestrator import PipelineFailed
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import (
    KIND_MERGE_COMMIT,
    KIND_PR,
    KIND_PR_MERGE,
    GitResult,
    ManualActionRequired,
)
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.runtime_layout import RuntimeLayout
from wastech_orchestrator.state_store import PublishOpRow, StateStore, TaskRow

# Every test here is a slow integration test (real git / subprocess / process tree).
pytestmark = pytest.mark.slow

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
        self.merge_argv: list[str] = []

    def __call__(self, args: Sequence[str]) -> GitResult:
        a = list(args)
        if a[:2] == ["pr", "merge"]:
            self.merge_called = True
            self.merge_argv = a
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
    # `source_path` is deliberately realistic: a finished task's row carries its lifecycle path,
    # and an empty one hid the exchange-containment breach in `_run_merge_flow` from every test
    # here (the containment walk skips falsy values, so `""` was never checked).
    store.insert_task(
        TaskRow(
            task_id="m1",
            title="merge me",
            status=status,
            branch=_BRANCH,
            source_path="tasks/done/m1.md",
        )
    )
    store.record_publish_op(
        PublishOpRow(
            task_id="m1", kind=KIND_PR, fingerprint=_BRANCH, status="completed", result_ref=_URL
        )
    )


def _setup_branch(
    git_run: GitRunner, clone: Path, *, conflict: bool, marker_less: bool = False
) -> None:
    """Create ``worc/m1`` with a committed change + push it, then advance origin/main.

    ``conflict``: base and branch edit the same file (README.md) → a conflicting base-merge.
    Otherwise they edit different files → a clean base-merge. ``marker_less`` adds a second,
    modify/delete conflict on ``shared.txt`` (the branch edits it, the base deletes it) — the shape
    Git resolves by leaving OUR file in the tree with no marker in it, so nothing in the file says
    whether anyone decided anything.
    """
    if marker_less:
        (clone / "shared.txt").write_text("base\n", encoding="utf-8")
        git_run(["add", "shared.txt"], clone)
        git_run(["commit", "-m", "seed shared"], clone)
        git_run(["push", "origin", "main"], clone)
    git_run(["checkout", "-b", _BRANCH, "main"], clone)
    target = "README.md" if conflict else "feature.txt"
    (clone / target).write_text("branch side\n", encoding="utf-8")
    git_run(["add", target], clone)
    if marker_less:
        (clone / "shared.txt").write_text("task edit\n", encoding="utf-8")
        git_run(["add", "shared.txt"], clone)
    git_run(["commit", "-m", "task change"], clone)
    git_run(["push", "-u", "origin", _BRANCH], clone)
    git_run(["checkout", "main"], clone)
    base_target = "README.md" if conflict else "BASE.md"
    (clone / base_target).write_text("base side\n", encoding="utf-8")
    git_run(["add", base_target], clone)
    if marker_less:
        git_run(["rm", "shared.txt"], clone)
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
    layout = RuntimeLayout(
        repo_root=Path(config.repo.local_path),
        control_home=Path(config.repo.local_path) / ".worc",
        private_home=tmp_path / "art",
        exchange_root=Path(config.repo.local_path) / ".worc-io",
    )
    return build_orchestrator(config, layout=layout, gh_runner=gh)


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


def test_merge_task_dictates_the_squash_message(
    git_repo, fake_cli, git_run, tmp_path: Path
) -> None:
    # Left to the target repository's settings, a `COMMIT_OR_PR_TITLE` / `COMMIT_MESSAGES` repo
    # writes a subject with no Conventional Commits type and a body listing every branch commit —
    # the orchestrator's own `chore(orchestrator): audit trail` included. Both landed on a real
    # `main`. The subject is the same one the task's code commit carries, plus the PR number.
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=False)

    orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    argv = gh.merge_argv
    assert "--subject" in argv and "--body" in argv
    assert argv[argv.index("--subject") + 1] == "feat(m1): merge me (#1)"
    assert argv[argv.index("--body") + 1] == ""  # the audit trail belongs to the branch, not main


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
    # ``success`` edits nothing, so the conflicted file is byte-identical to what the merge left.
    # The decision gate now names that before the commit seam's marker guard would (it runs
    # earlier, and refuses "nobody decided" rather than "a marker survived"), so the class is the
    # human-needed one; the marker guard's own coverage is
    # `test_the_marker_guard_still_fires_when_the_gate_passes` below.
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=True)

    with pytest.raises(ManualActionRequired):
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


def test_a_staging_gate_refusal_still_aborts_the_merge(
    git_repo, fake_cli, git_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `commit_merge_resolution`'s own gates raise ManualActionRequired, not GitCommandError, so
    # they escape past a GitCommandError-only abort and leave the clone mid-merge, which blocks
    # cleanup and the next task. The tree must be restored, and the outcome must keep its class
    # (a block for a human), not be downgraded to a pipeline failure.
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=False)

    def refuse(task_id: str, message: str) -> str | None:
        assert orch._git.merge_in_progress() is True  # a merge really is in flight here
        raise ManualActionRequired("a staged entry needs a human")

    monkeypatch.setattr(orch._git, "commit_merge_resolution", refuse)

    with pytest.raises(ManualActionRequired):
        orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    assert orch._git.merge_in_progress() is False  # restored, not wedged mid-merge
    assert gh.merge_called is False


def test_a_merge_flow_node_stopped_before_its_provider_closes_its_row(
    git_repo, fake_cli, git_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `merge_task` converts the node-layer `NodeManualRequired` into `ManualActionRequired` and
    # aborts the git merge cleanly — the transactional promise covers git, not the state store. Two
    # real runs of this left `node_runs` rows `running` with no `finished_at` on a task that is
    # `done`, and no reader can tell such a row from a node still executing. The lifetime is owned
    # at the node layer now, so this holds for any node that stops before its provider call, on any
    # path that does not go through the task driver's terminal.
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="resolve_conflicts", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=True)

    def _breach(request: object, exchange_root: str) -> None:
        raise NodeManualRequired("exchange containment violation: a private path reached a request")

    monkeypatch.setattr(
        "wastech_orchestrator.core.flow.nodes.agent.assert_request_contained", _breach
    )

    with pytest.raises(ManualActionRequired):
        orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    runs = orch._store.get_node_runs("m1")
    assert runs, "the merge flow must have opened a node run for this to be a regression test"
    assert not [r for r in runs if r.status == "running" or r.finished_at is None]
    closed = runs[-1]
    assert closed.status == "aborted" and closed.finished_at
    assert "containment violation" in (closed.abort_reason or "")
    # The reason belongs to `abort_reason`, never `skip_reason`: this node ran and was interrupted,
    # which is the opposite of a `when`-false node that was never run at all.
    assert closed.skip_reason is None and not closed.skipped
    assert orch._git.merge_in_progress() is False  # the transactional git promise still holds


def test_the_conflict_report_reaches_the_agent_and_the_audit_tree(
    git_repo, fake_cli, git_run, tmp_path: Path
) -> None:
    # The agent is told what conflicted instead of being sent to find markers itself: a private
    # audit copy under logs/, and the redacted exchange copy the role prompt reads.
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="resolve_conflicts_all", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=True, marker_less=True)

    orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    private = task_artifact_dir(orch._artifacts_root, "m1") / "merge" / "conflicts.md"
    published = git_repo.clone / ".worc-io" / "m1" / "merge" / "conflicts.md"
    assert private.is_file() and published.is_file()
    report = private.read_text(encoding="utf-8")
    assert "## README.md — both sides modified it (`UU`)" in report
    assert "## shared.txt — we modified it, base deleted it (`UD`)" in report
    assert "no conflict markers" in report  # the marker-less one is named as such


def test_a_marker_less_conflict_nobody_decided_is_refused(
    git_repo, fake_cli, git_run, tmp_path: Path
) -> None:
    # The defect this gate exists for: `git add -A` would have taken the side Git happened to leave
    # in the tree and committed it, with `git diff --cached --check` seeing nothing to complain
    # about. `resolve_conflicts` strips markers only, so `shared.txt` is left exactly as-is.
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="resolve_conflicts", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=True, marker_less=True)

    with pytest.raises(ManualActionRequired) as exc:
        orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    message = str(exc.value)
    assert "shared.txt" in message and "no decision" in message
    assert "README.md" not in message  # the marker conflict WAS resolved — only the silent one
    assert gh.merge_called is False
    assert orch._git.merge_in_progress() is False  # transactional: the tree is restored
    assert orch._store.get_task("m1").status is Status.DONE  # never downgraded
    # The gate runs before the commit seam, so the ledger carries no started-never-finished merge.
    assert orch._store.get_publish_op("m1", KIND_MERGE_COMMIT, None) is None


def test_a_marker_less_conflict_resolved_by_rewriting_is_merged(
    git_repo, fake_cli, git_run, tmp_path: Path
) -> None:
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="resolve_conflicts_all", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=True, marker_less=True)

    result = orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    assert result.final_status is Status.DONE
    assert gh.merge_called is True
    assert "resolved by the merge agent" in (git_repo.clone / "shared.txt").read_text(
        encoding="utf-8"
    )


def test_a_marker_less_conflict_resolved_by_deleting_is_merged(
    git_repo, fake_cli, git_run, tmp_path: Path
) -> None:
    # Accepting the base's deletion is a decision too, and the working tree shows it.
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="resolve_conflicts_by_deleting", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=True, marker_less=True)

    result = orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    assert result.final_status is Status.DONE
    assert gh.merge_called is True
    assert not (git_repo.clone / "shared.txt").exists()


def test_the_marker_guard_still_fires_when_the_gate_passes(
    git_repo, fake_cli, git_run, tmp_path: Path
) -> None:
    # Neither guard subsumes the other: the gate asks "did anyone decide", the commit seam asks
    # "is the decision sane". `mangle_conflicts` moves the bytes without removing the markers.
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="mangle_conflicts", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=True)

    with pytest.raises(PipelineFailed, match="conflict marker"):
        orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    assert gh.merge_called is False
    assert orch._git.merge_in_progress() is False


def test_clean_base_merge_writes_no_conflict_report(
    git_repo, fake_cli, git_run, tmp_path: Path
) -> None:
    gh = FakeGh("OPEN")
    orch = _build(git_repo, fake_cli, tmp_path, scenario="success", gh=gh)
    _seed_task(orch._store)
    _setup_branch(git_run, git_repo.clone, conflict=False)

    orch.merge_task("m1", strategy=MergeStrategy.SQUASH, wait_for_checks=False)

    assert not (task_artifact_dir(orch._artifacts_root, "m1") / "merge").exists()
