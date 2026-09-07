"""Tests for the ``finalize`` CLI command — record + tidy a human-handled task."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wastech_orchestrator import cli, process_control
from wastech_orchestrator.core.flow.run_state import FlowRunState
from wastech_orchestrator.core.loop_control import LoopCounters
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import GitManager
from wastech_orchestrator.ledger import Ledger, LedgerRecord
from wastech_orchestrator.state_store import NodeRunRow, PublishOpRow, StateStore, TaskRow
from wastech_orchestrator.task.parser import read_subtask_refs

# Every test here is a slow integration test (real git / subprocess / process tree).
pytestmark = pytest.mark.slow

_ENV = ["PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"]


def _write_config(
    project: Path,
    clone: Path,
    *,
    create_pr: bool = False,
    checkout_base_on_cleanup: bool | None = None,
) -> Path:
    env_lines = "\n".join(f"    - {e}" for e in _ENV)
    cleanup_line = (
        f"  checkout_base_on_cleanup: {str(checkout_base_on_cleanup).lower()}\n"
        if checkout_base_on_cleanup is not None
        else ""
    )
    config = project / "config.yaml"
    config.write_text(
        f"""
orchestrator:
  auto_mode:
    enabled: false
  poll_interval_seconds: 0
repo:
  url: "git@example.com:o/r.git"
  local_path: {str(clone)!r}
  base_branch: "main"
  branch_prefix: "worc"
{cleanup_line}agents:
  allowed: [claude, codex]
  providers:
    claude:
      command: "claude"
      primary: true
    codex:
      command: "codex"
security:
  allowed_environment:
{env_lines}
validation:
  quarantine_folder: {str(project / "rejected")!r}
checks:
  commands: []
git:
  create_pull_request: {str(create_pr).lower()}
  pr_base: "main"
""",
        encoding="utf-8",
    )
    return config


def _ledger_records(clone: Path) -> list[dict]:
    path = clone / ".worc" / "logs" / "completed.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _seed(
    project: Path,
    clone: Path,
    *,
    status: Status = Status.FAILED,
    branch: str | None = "worc/task-1-t",
    pr_url: str | None = None,
    create_pr: bool = False,
    checkout_base_on_cleanup: bool | None = None,
) -> Path:
    """Seed a terminal task (state row + source file) and return the config path."""
    config = _write_config(
        project, clone, create_pr=create_pr, checkout_base_on_cleanup=checkout_base_on_cleanup
    )
    source = project / "task-1.md"
    source.write_text("---\nid: task-1\ntitle: T\n---\n\nbody\n", encoding="utf-8")
    db = clone / ".worc" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore.open(db)
    store.insert_task(
        TaskRow(task_id="task-1", title="T", status=status, source_path=str(source), branch=branch)
    )
    if pr_url is not None:
        store.record_publish_op(
            PublishOpRow(
                task_id="task-1",
                kind="pr",
                fingerprint=branch or "b",
                status="completed",
                result_ref=pr_url,
            )
        )
    store.close()
    return config


def _status(clone: Path) -> Status:
    store = StateStore.open_readonly(clone / ".worc" / "state.db")
    row = store.get_task("task-1")
    store.close()
    assert row is not None
    return row.status


# --- core outcomes -----------------------------------------------------------------------


def test_finalize_failed_reconciles(git_repo, git_run, tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone, status=Status.MANUAL_ACTION_REQUIRED)
    git_run(["branch", "worc/task-1-t"], git_repo.clone)  # stray local branch, kept by default

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "--yes"])
    assert code == 1  # failed exit code

    assert _status(git_repo.clone) is Status.FAILED
    records = _ledger_records(git_repo.clone)
    assert len(records) == 1
    assert records[0]["final_status"] == "failed" and records[0]["manual"] is True
    assert (project / "failed" / "task-1.md").exists()  # moved into its lifecycle folder
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    # No commit/push/PR: the branch is kept and not pushed to the remote.
    assert git_run(["ls-remote", "--heads", "origin", "worc/task-1-t"], git_repo.clone) == ""
    assert "worc/task-1-t" in git_run(["branch", "--list", "worc/task-1-t"], git_repo.clone)


def test_finalize_syncs_loop_counters_from_checkpoint(git_repo, tmp_path: Path) -> None:
    # A task stopped mid-flow and finished by hand keeps stale operator-facing counter
    # columns — they mirror only at a clean terminal transition, which a killed run never reaches.
    # finalize must re-sync them from the authoritative flow checkpoint so status/ledger report the
    # real fix-loop churn (here: 3 review_fix reworks) rather than the last synced value.
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone, status=Status.MANUAL_ACTION_REQUIRED)
    db = git_repo.clone / ".worc" / "state.db"
    store = StateStore.open(db)
    store.save_flow_checkpoint(
        "task-1",
        current_node="testing",
        counters_json=json.dumps(
            {
                "review_fix": 2,
                FlowRunState.total_key("review_fix"): 3,
                FlowRunState.GLOBAL_FIX_KEY: 3,
            }
        ),
        flow_fingerprint="fp",
        fix_iterations=3,
    )
    # Stale mirror: the totals lag the checkpoint (fix_iterations stays current — it is mirrored on
    # every checkpoint, so only the *_total / *_cycles columns drift).
    store.save_counters("task-1", LoopCounters(review_fix_total=1, fix_iterations=3))
    store.close()

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "--yes"])
    assert code == 1

    store = StateStore.open_readonly(db)
    counters = store.get_counters("task-1")
    store.close()
    assert counters.review_fix_total == 3  # re-synced from the checkpoint, not the stale 1
    assert counters.review_fix_cycles == 2
    assert counters.fix_iterations == 3


def test_finalize_reconciles_orphan_node_runs(git_repo, tmp_path: Path) -> None:
    # A --force-full stop SIGKILLs the daemon mid-node, leaving a node run stranded 'running'
    # and its provider attempt unbilled. The hand-finish path must close the orphan to 'aborted' and
    # record a provider_attempts row (usage 'unknown') so the aborted run is auditable, not free.
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone, status=Status.MANUAL_ACTION_REQUIRED)
    db = git_repo.clone / ".worc" / "state.db"
    store = StateStore.open(db)
    orphan = store.record_node_run(
        NodeRunRow(
            task_id="task-1",
            node_id="implementation",
            node_kind="agent",
            route_primary="claude",
            status="running",
            started_at="2026-07-25T00:00:00+00:00",
        )
    )
    store.close()

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "--yes"])
    assert code == 1

    store = StateStore.open_readonly(db)
    runs = {r.id: r for r in store.get_node_runs("task-1")}
    attempts = store.get_provider_attempts_for_task("task-1")
    store.close()
    # The orphan is closed with a finish time + the operator-action reason.
    assert runs[orphan].status == "aborted"
    assert runs[orphan].finished_at is not None
    assert runs[orphan].error_class == "cancelled"
    assert runs[orphan].skip_reason  # names the finalize action
    # The killed attempt is on the ledger — provider from route_primary, usage marked 'unknown'.
    aborted = [a for a in attempts if a.node_run_id == orphan]
    assert len(aborted) == 1
    assert aborted[0].provider == "claude"
    assert aborted[0].usage_delta_status == "unknown"
    assert aborted[0].usage_cost is None  # never a guessed dollar figure


def test_finalize_done_uses_recorded_pr_url(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone, pr_url="https://example/pull/9", create_pr=False)
    monkeypatch.setattr(GitManager, "verify_pr_state", lambda self, url: "MERGED")

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "done", "--yes"])
    assert code == 0

    assert _status(git_repo.clone) is Status.DONE
    rec = _ledger_records(git_repo.clone)[0]
    assert rec["final_status"] == "done" and rec["manual"] is True
    assert rec["pr_url"] == "https://example/pull/9"  # picked up from the recorded publish op
    assert (project / "done" / "task-1.md").exists()


def test_finalize_done_unmerged_pr_needs_confirmation(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone, pr_url="https://example/pull/9")
    monkeypatch.setattr(GitManager, "verify_pr_state", lambda self, url: "OPEN")
    monkeypatch.setattr("builtins.input", lambda *_: "n")  # decline the WARNING prompt

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "done"])
    assert code == 0  # aborted cleanly
    assert _status(git_repo.clone) is Status.FAILED  # unchanged
    assert _ledger_records(git_repo.clone) == []


def test_finalize_done_no_url_warns_then_finalizes(git_repo, tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone, branch=None)  # no branch, no recorded PR

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "done", "--yes"])
    assert code == 0
    assert _status(git_repo.clone) is Status.DONE
    rec = _ledger_records(git_repo.clone)[0]
    assert rec["manual"] is True and rec["pr_url"] is None  # recorded done without a URL


def test_finalize_abandoned_marks_outcome(git_repo, tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone)

    code = cli.main(
        [
            "--config",
            str(config),
            "finalize",
            "task-1",
            "--as",
            "abandoned",
            "--note",
            "obsolete",
            "--yes",
        ]
    )
    assert code == 2  # manual_action_required exit code
    assert _status(git_repo.clone) is Status.MANUAL_ACTION_REQUIRED
    rec = _ledger_records(git_repo.clone)[0]
    assert rec["outcome"] == "abandoned" and rec["manual"] is True and rec["note"] == "obsolete"


# --- guards ------------------------------------------------------------------------------


def test_finalize_fail_closed_on_dirty_tree(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone)
    (git_repo.clone / "README.md").write_text("locally edited\n", encoding="utf-8")  # unaccounted

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "--yes"])
    assert code == 1
    assert "unaccounted changes" in capsys.readouterr().out
    assert _status(git_repo.clone) is Status.FAILED  # status row untouched
    assert _ledger_records(git_repo.clone) == []


def test_finalize_delete_branch(git_repo, git_run, tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone)
    git_run(["branch", "worc/task-1-t"], git_repo.clone)

    code = cli.main(
        [
            "--config",
            str(config),
            "finalize",
            "task-1",
            "--as",
            "failed",
            "--delete-branch",
            "--yes",
        ]
    )
    assert code == 1
    assert git_run(["branch", "--list", "worc/task-1-t"], git_repo.clone) == ""  # deleted


def test_finalize_refuses_while_daemon_running(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone)
    monkeypatch.setattr(cli.process_control, "running_daemon_pid", lambda _p: 4321)

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed"])
    assert code == 1
    assert "watch daemon is running" in capsys.readouterr().out


def test_finalize_idempotent_refuses_second_time(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone)
    # A prior finalize already wrote a manual record.
    Ledger(git_repo.clone / ".worc" / "logs").append(
        LedgerRecord(id="task-1", title="T", final_status="failed", finished_at="t", manual=True)
    )

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "--yes"])
    assert code == 1
    assert "already finalized" in capsys.readouterr().out


def test_finalize_dry_run_writes_nothing(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone, pr_url="https://example/pull/9")

    code = cli.main(
        [
            "--config",
            str(config),
            "finalize",
            "task-1",
            "--as",
            "done",
            "--dry-run",
            "--no-verify-pr",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "recorded" in out  # the PR-url source is named
    assert "checkout base 'main'" in out  # default (new mode) returns to base
    assert _status(git_repo.clone) is Status.FAILED  # unchanged
    assert _ledger_records(git_repo.clone) == []


def test_finalize_dry_run_stays_on_branch_when_disabled(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone, checkout_base_on_cleanup=False)

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "stay on branch" in out and "checkout base" not in out


def test_finalize_dry_run_is_allowed_while_the_daemon_runs(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # `plan_finalize` is read-only ("mutates nothing"), so the guard that protects the shared clone
    # has nothing to protect here — the same line `prs --sync` already draws for its dry run.
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone, pr_url="https://example/pull/9")
    process_control.write_pid_file(
        process_control.pid_file_path(git_repo.clone / ".worc"), pid=4242
    )
    monkeypatch.setattr(process_control, "is_running", lambda pid, **kw: True)

    code = cli.main(
        [
            "--config",
            str(config),
            "finalize",
            "task-1",
            "--as",
            "done",
            "--dry-run",
            "--no-verify-pr",
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "watch daemon is running" in out  # a note, so the plan is not read as executable now


# --- the task-file move ------------------------------------------------------------------


def _seed_tracked_task_file(git_repo, git_run, project: Path) -> Path:
    """Commit ``tasks/pending/task-1.md`` inside the clone, the way a git-distributed task arrives.

    The default harness keeps the task file outside the repository, where a move is invisible to
    git. This is the shape an operator actually has: the lifecycle folders are deliberately tracked
    (a finished task's file and its summary are the human-readable audit trail), so moving one is a
    tracked change in their working tree.
    """
    pending = git_repo.clone / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    source = pending / "task-1.md"
    source.write_text("---\nid: task-1\ntitle: T\n---\n\nbody\n", encoding="utf-8")
    git_run(["add", "tasks/pending/task-1.md"], git_repo.clone)
    git_run(["commit", "-m", "chore: queue task-1"], git_repo.clone)
    return source


def _seed_decomposition_root(git_repo, git_run, project: Path, *, n: int = 2) -> Path:
    """Commit a ``subtasks:`` root plus its spec files, the shape ``promote`` puts in ``pending/``.

    Mirrors the promote contract deliberately: the specs live in a ``subtasks/`` subfolder (never
    beside the root, where the queue scan would claim them as standalone tasks) and the refs are
    relative to the root file's own directory, which is what the relocation has to preserve.
    """
    pending = git_repo.clone / "tasks" / "pending"
    (pending / "subtasks").mkdir(parents=True, exist_ok=True)
    refs = [f"subtasks/{i:02d}-step.md" for i in range(1, n + 1)]
    root = pending / "epic.md"
    manifest = "".join(f"  - {ref}\n" for ref in refs)
    root.write_text(
        f"---\nid: epic\ntitle: Epic\nsubtasks:\n{manifest}---\n\nbody\n", encoding="utf-8"
    )
    for i, ref in enumerate(refs, start=1):
        (pending / ref).write_text(
            f"---\ntitle: Step {i}\nslug: step-{i}\n---\n\nAcceptance criteria\n\n- ok\n",
            encoding="utf-8",
        )
    git_run(["add", "tasks/pending"], git_repo.clone)
    git_run(["commit", "-m", "chore: queue epic"], git_repo.clone)
    return root


def test_finalize_carries_the_subtask_specs_with_the_root(
    git_repo, git_run, tmp_path: Path
) -> None:
    # The defect this pins: the root reached `done/` and its specs stayed in `pending/subtasks/`,
    # so the finished root could no longer resolve its own manifest — every `subtasks:` ref is
    # relative to the root file's directory. `promote` carries the pair in together and states the
    # invariant ("a root never appears in pending/ without its specs"); the terminal move has to be
    # the same operation in reverse, or the orchestrator separates a set its own code calls
    # inseparable. Fails against the previous relocation, which moved exactly one file.
    project = tmp_path / "p"
    project.mkdir()
    root = _seed_decomposition_root(git_repo, git_run, project)
    config = _seed(project, git_repo.clone, status=Status.MANUAL_ACTION_REQUIRED)
    store = StateStore.open(git_repo.clone / ".worc" / "state.db")
    store.update_task("task-1", source_path=str(root))
    store.close()

    assert cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "-y"]) == 1

    failed = git_repo.clone / "tasks" / "failed"
    assert (failed / "epic.md").is_file()
    # Beside the root in its new home, under the same relative path the manifest names…
    assert (failed / "subtasks" / "01-step.md").is_file()
    assert (failed / "subtasks" / "02-step.md").is_file()
    # …and gone from the queue, which is what "pending" is supposed to mean.
    assert not (git_repo.clone / "tasks" / "pending" / "subtasks" / "01-step.md").exists()
    # The point of the move, not a side effect: the manifest resolves from where the root now is.
    for ref in read_subtask_refs(failed / "epic.md"):
        assert (failed / ref).is_file(), ref


def test_finalize_does_not_nest_a_lifecycle_folder_inside_the_queue(
    git_repo, git_run, tmp_path: Path
) -> None:
    # The trap in the obvious implementation: `lifecycle_destination` is single-file and derives the
    # tasks root from the file's parent, so asking it where a spec belongs answers
    # `tasks/pending/subtasks/failed/01-step.md` — a lifecycle folder nested inside the queue. The
    # destination has to come from the root's move instead.
    project = tmp_path / "p"
    project.mkdir()
    root = _seed_decomposition_root(git_repo, git_run, project, n=1)
    config = _seed(project, git_repo.clone, status=Status.MANUAL_ACTION_REQUIRED)
    store = StateStore.open(git_repo.clone / ".worc" / "state.db")
    store.update_task("task-1", source_path=str(root))
    store.close()

    assert cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "-y"]) == 1

    pending = git_repo.clone / "tasks" / "pending"
    assert not (pending / "subtasks" / "failed").exists()
    assert not (pending / "subtasks" / "done").exists()


def test_finalize_announces_the_specs_that_travel_with_the_root(
    git_repo, git_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same reason the root's own move is announced: these are tracked files the finalize dirties
    # and deliberately does not commit, and several of them appearing in `git status` unannounced
    # is worse than one. The dry run must predict the same count it will move.
    project = tmp_path / "p"
    project.mkdir()
    root = _seed_decomposition_root(git_repo, git_run, project, n=3)
    config = _seed(project, git_repo.clone, status=Status.MANUAL_ACTION_REQUIRED)
    store = StateStore.open(git_repo.clone / ".worc" / "state.db")
    store.update_task("task-1", source_path=str(root))
    store.close()

    assert (
        cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "--dry-run"])
        == 0
    )
    assert "+3 subtask specs" in capsys.readouterr().out

    assert cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "-y"]) == 1
    assert "+3 subtask specs" in capsys.readouterr().out


def test_finalize_of_an_ordinary_task_announces_no_specs(
    git_repo, git_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A task with no `subtasks:` manifest must read exactly as it did before: one file, no suffix.
    project = tmp_path / "p"
    project.mkdir()
    source = _seed_tracked_task_file(git_repo, git_run, project)
    config = _seed(project, git_repo.clone, status=Status.MANUAL_ACTION_REQUIRED)
    store = StateStore.open(git_repo.clone / ".worc" / "state.db")
    store.update_task("task-1", source_path=str(source))
    store.close()

    assert cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "-y"]) == 1

    out = capsys.readouterr().out
    assert "tasks/failed/task-1.md" in out
    assert "subtask spec" not in out


def test_finalize_leaves_the_task_file_move_uncommitted(git_repo, git_run, tmp_path: Path) -> None:
    # The observation the finding rests on, pinned as behavior rather than as a defect: finalize
    # moves the file and commits nothing ("Runs no pipeline and never commits/pushes/PRs"), so the
    # move lands in the operator's working tree. That contract is right — the operator may be on
    # `main`, and committing there behind their back is worse than a change they can see — which is
    # why the fix is to SAY so, not to commit it.
    project = tmp_path / "p"
    project.mkdir()
    source = _seed_tracked_task_file(git_repo, git_run, project)
    config = _seed(project, git_repo.clone, status=Status.MANUAL_ACTION_REQUIRED)
    store = StateStore.open(git_repo.clone / ".worc" / "state.db")
    store.update_task("task-1", source_path=str(source))
    store.close()

    assert cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "-y"]) == 1

    assert (git_repo.clone / "tasks" / "failed" / "task-1.md").exists()
    status = git_run(["status", "--porcelain"], git_repo.clone)
    assert "tasks/pending/task-1.md" in status  # the tracked deletion is uncommitted…
    assert "tasks/failed" in status  # …and the arrival is untracked (porcelain folds the dir)


def test_finalize_names_the_move_it_makes(
    git_repo, git_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # It was invisible on every surface: the plan reported status / pr url / cleanup / branch /
    # ledger, the result reported the status, and neither mentioned a file move that had just
    # dirtied the operator's tree — and that rides into the next task's review diff.
    project = tmp_path / "p"
    project.mkdir()
    source = _seed_tracked_task_file(git_repo, git_run, project)
    config = _seed(project, git_repo.clone, status=Status.MANUAL_ACTION_REQUIRED)
    store = StateStore.open(git_repo.clone / ".worc" / "state.db")
    store.update_task("task-1", source_path=str(source))
    store.close()

    assert cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "-y"]) == 1

    out = capsys.readouterr().out
    assert "tasks/failed/task-1.md" in out
    assert "not committed" in out


def test_finalize_dry_run_announces_the_move(
    git_repo, git_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    source = _seed_tracked_task_file(git_repo, git_run, project)
    config = _seed(project, git_repo.clone, status=Status.MANUAL_ACTION_REQUIRED)
    store = StateStore.open(git_repo.clone / ".worc" / "state.db")
    store.update_task("task-1", source_path=str(source))
    store.close()

    assert (
        cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "--dry-run"])
        == 0
    )

    out = capsys.readouterr().out
    assert "tasks/pending/task-1.md" in out and "tasks/failed/task-1.md" in out
    assert (git_repo.clone / "tasks" / "pending" / "task-1.md").exists()  # dry run moved nothing


def test_finalize_abandoned_announces_no_move(
    git_repo, git_run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--as abandoned` leaves the file in `pending/` for the operator to resolve, so there is no
    # move to report and the line must not appear at all.
    project = tmp_path / "p"
    project.mkdir()
    source = _seed_tracked_task_file(git_repo, git_run, project)
    config = _seed(project, git_repo.clone, status=Status.FAILED)
    store = StateStore.open(git_repo.clone / ".worc" / "state.db")
    store.update_task("task-1", source_path=str(source))
    store.close()

    cli.main(["--config", str(config), "finalize", "task-1", "--as", "abandoned", "-y"])

    out = capsys.readouterr().out
    assert "not committed" not in out
    assert (git_repo.clone / "tasks" / "pending" / "task-1.md").exists()
