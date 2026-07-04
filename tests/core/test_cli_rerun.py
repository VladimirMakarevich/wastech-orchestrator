"""Tests for the ``rerun`` CLI command — fresh re-attempt and ``--continue``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.core.orchestrator import Orchestrator, PipelineResult
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.ledger import Ledger, LedgerRecord
from wastech_orchestrator.state_store import StateStore, TaskRow

_ENV = ["PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"]


def _write_config(
    project: Path, clone: Path, *, claude: str, codex: str, create_pr: bool = False
) -> Path:
    env_lines = "\n".join(f"    - {e}" for e in _ENV)
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
agents:
  allowed: [claude, codex]
  providers:
    claude:
      command: {claude!r}
      primary: true
    codex:
      command: {codex!r}
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


def _complete_task_file(path: Path, task_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'---\nid: {task_id}\ntitle: "Add a thing"\n---\n\n'
        "## Description\n\nDo the thing.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )


def _ledger_records(clone: Path) -> list[dict]:
    path = clone / ".worc" / "logs" / "completed.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# --- fresh re-attempt: failed -> rerun -> done -------------------------------------------


def test_real_failure_persists_flow_checkpoint(git_repo, fake_cli, tmp_path: Path) -> None:
    """A real terminal failure leaves a flow checkpoint (current_node) for ``--continue``."""
    project = tmp_path / "project"
    project.mkdir()
    config = _write_config(
        project,
        git_repo.clone,
        claude=fake_cli("process_crashed", "claude"),
        codex=fake_cli("process_crashed", "codex"),
    )
    task_file = project / "task-700.md"
    _complete_task_file(task_file, "task-700")

    code = cli.main(["--config", str(config), "--heartbeat-seconds", "0", "run", str(task_file)])
    assert code == 1  # failed

    store = StateStore.open_readonly(git_repo.clone / ".worc" / "state.db")
    row = store.get_task("task-700")
    current_node, _counters, _fingerprint = store.get_flow_checkpoint("task-700")
    store.close()
    assert row is not None and row.status is Status.FAILED
    assert current_node is not None  # the flow checkpoint records where --continue re-enters


def test_rerun_fresh_failed_to_done(git_repo, fake_cli, git_run, tmp_path: Path) -> None:
    """A failed attempt with a local stale branch (no remote/PR — the common pre-publish failure) is
    re-attempted fresh from base to done; the ledger links the attempts and prior artifacts are
    archived."""
    project = tmp_path / "project"
    project.mkdir()
    config = _write_config(
        project,
        git_repo.clone,
        claude=fake_cli("success_edit", "claude"),
        codex=fake_cli("success_edit", "codex"),
    )
    external = git_repo.clone / ".worc"

    # Seed the bookkeeping of a prior pre-publish failure: a FAILED row + ledger line, a stale
    # *local* branch (never pushed), and a stale artifact under logs/<id>/.
    source = project / "failed" / "task-700.md"
    _complete_task_file(source, "task-700")
    db = external / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore.open(db)
    store.insert_task(
        TaskRow(
            task_id="task-700",
            title="Add a thing",
            status=Status.FAILED,
            source_path=str(source),
            branch="worc/task-700-add-a-thing",
            slug="add-a-thing",
            cleanup_completed=True,
        )
    )
    store.close()
    Ledger(external / "logs").append(
        LedgerRecord(id="task-700", title="Add a thing", final_status="failed", finished_at="t1")
    )
    stale = external / "logs" / "task-700" / "plan.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale plan from the failed attempt\n", encoding="utf-8")
    git_run(["branch", "worc/task-700-add-a-thing"], git_repo.clone)  # stale local branch

    code = cli.main(
        ["--config", str(config), "--heartbeat-seconds", "0", "rerun", "task-700", "--yes"]
    )
    assert code == 0  # done

    # Two linked ledger records: the prior failure preserved, the rerun marked attempt 2.
    records = _ledger_records(git_repo.clone)
    assert len(records) == 2
    assert records[0]["final_status"] == "failed"
    assert records[1]["final_status"] == "done"
    assert records[1]["attempt"] == 2 and records[1]["rerun_of"] == "task-700"
    # The change is committed on a branch rebuilt from the current base; HEAD back on main.
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    # The stale branch was deleted and rebuilt under a fresh epoch; resolve the new name.
    branch = git_run(
        ["branch", "--list", "--format=%(refname:short)", "worc/*-task-700-add-a-thing"],
        git_repo.clone,
    )
    assert branch
    committed = git_run(["show", "--name-only", "--format=", branch], git_repo.clone)
    assert "agent_change.py" in committed
    # Prior artifacts archived; the fresh attempt's summary written at the top level.
    assert (external / "logs" / "task-700" / "attempt-1" / "plan.md").exists()
    assert (external / "logs" / "task-700" / "summary.md").exists()
    # The task file ended in done/, and the final state row is done.
    assert (project / "done" / "task-700.md").exists()
    store = StateStore.open_readonly(db)
    final_row = store.get_task("task-700")
    store.close()
    assert final_row is not None and final_row.status is Status.DONE


# --- guards / refusals (no pipeline run) -------------------------------------------------


def _seed(
    project: Path,
    clone: Path,
    row: TaskRow,
    *,
    checkpoint_node: str | None = None,
    node_run: tuple[str, str] | None = None,
) -> Path:
    config = _write_config(project, clone, claude="claude", codex="codex")
    db = clone / ".worc" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore.open(db)
    store.insert_task(row)
    if node_run is not None:  # (node_id, node_kind) for the interrupted node — F14 reads node_kind
        from wastech_orchestrator.state_store import NodeRunRow

        node_id, node_kind = node_run
        store.record_node_run(
            NodeRunRow(
                task_id=row.task_id,
                node_id=node_id,
                node_kind=node_kind,
                subtask_order=None,
                status="failed",
                started_at="2026-01-01T00:00:00+00:00",
            )
        )
    if checkpoint_node is not None:  # an interrupted engine task: a flow checkpoint to resume from
        from wastech_orchestrator.core.flow.registry import FlowRegistry

        store.save_flow_checkpoint(
            row.task_id,
            current_node=checkpoint_node,
            counters_json="{}",
            flow_fingerprint=FlowRegistry().resolve("implementation").flow_fingerprint,
            fix_iterations=row.fix_iterations,  # checkpoint mirrors the seeded task's fix counter
        )
    store.close()
    return config


def test_rerun_refuses_unknown_id(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _seed(project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED))
    code = cli.main(["--config", str(config), "rerun", "task-missing"])
    assert code == 1
    assert "unknown task id" in capsys.readouterr().out


def test_rerun_refuses_non_terminal_task(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _seed(project, git_repo.clone, TaskRow("task-1", "T", Status.RUNNING))
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    assert code == 1
    assert "is running" in capsys.readouterr().out


def test_rerun_refuses_when_daemon_running(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _seed(project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED))
    monkeypatch.setattr(cli.process_control, "running_daemon_pid", lambda _p: 4321)
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    assert code == 1
    assert "watch daemon is running" in capsys.readouterr().out


def test_rerun_resolves_task_file_moved_between_lifecycle_folders(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The stored source_path points at tasks/failed/, but the file now lives in tasks/pending/
    # (a manual/external move). The resolver finds it by id across lifecycle folders instead of
    # refusing — the main "failed → rerun" barrier.
    project = tmp_path / "project"
    project.mkdir()
    actual = project / "pending" / "task-1.md"
    _complete_task_file(actual, "task-1")
    stale = project / "failed" / "task-1.md"  # recorded path, but no file there
    config = _seed(
        project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED, source_path=str(stale))
    )
    code = cli.main(["--config", str(config), "rerun", "task-1", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "would re-attempt task-1" in out
    assert "source file is missing" not in out


def test_rerun_refuses_when_task_file_ambiguous(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two files match the id across lifecycle folders → never guessed; the rerun refuses and names
    # both so the operator leaves exactly one.
    project = tmp_path / "project"
    project.mkdir()
    _complete_task_file(project / "pending" / "task-1.md", "task-1")
    _complete_task_file(project / "done" / "task-1.md", "task-1")
    stale = project / "failed" / "task-1.md"
    config = _seed(
        project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED, source_path=str(stale))
    )
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    assert code == 1
    assert "ambiguous" in capsys.readouterr().out


def test_rerun_refuses_when_task_file_truly_missing(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No file matches the id in any lifecycle folder → the original "missing" refusal.
    project = tmp_path / "project"
    project.mkdir()
    stale = project / "failed" / "task-1.md"  # never created anywhere
    config = _seed(
        project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED, source_path=str(stale))
    )
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    assert code == 1
    assert "source file is missing" in capsys.readouterr().out


def test_rerun_refuses_fresh_in_operator_owned_branch_mode(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # branch-mode ADR: a fresh rerun resets the branch to base — forbidden in existing/current mode,
    # where the branch is the operator's. The refusal directs them to `rerun --continue` instead.
    from wastech_orchestrator.config.schema import BranchMode
    from wastech_orchestrator.task.model import NormalizedTask
    from wastech_orchestrator.task.parser import write_normalized

    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow("task-1", "T", Status.FAILED, source_path=str(source), branch="feature/keep"),
    )
    # The rerun guard reads the effective branch mode from the persisted normalized manifest.
    write_normalized(
        NormalizedTask(
            id="task-1",
            title="T",
            description="x",
            branch_mode=BranchMode.EXISTING,
            branch_ref="feature/keep",
        ),
        git_repo.clone / ".worc",
    )
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    out = capsys.readouterr().out
    assert code == 1
    assert "branch_mode 'existing'" in out and "operator-owned" in out
    assert "rerun --continue" in out


def test_rerun_dry_run_writes_nothing(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow("task-1", "T", Status.FAILED, source_path=str(source)),
    )
    code = cli.main(["--config", str(config), "rerun", "task-1", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    # Nothing changed: still failed, no ledger record appended.
    store = StateStore.open_readonly(git_repo.clone / ".worc" / "state.db")
    row = store.get_task("task-1")
    store.close()
    assert row is not None and row.status is Status.FAILED
    assert _ledger_records(git_repo.clone) == []


# --- continue mode -----------------------------------------------------------------------


def test_rerun_continue_refuses_without_recoverable_stage(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    # No flow checkpoint recorded -> --continue cannot know which node to re-enter.
    config = _seed(
        project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED, source_path=str(source))
    )
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue"])
    assert code == 1
    assert "no recoverable node" in capsys.readouterr().out


def test_rerun_continue_in_publish_allows_uncommitted_code(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F14: when ``commit_code`` failed inside the publish node, the agent's code is left
    uncommitted in the working tree. ``rerun --continue`` re-enters publish (commit_code is
    idempotent and docommits it), so that dirty state must NOT be refused as "unaccounted changes".
    """
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow(
            "task-1",
            "T",
            Status.MANUAL_ACTION_REQUIRED,
            source_path=str(source),
            branch="worc/task-1-t",
            finished_at="2026-01-01T00:00:00+00:00",
        ),
        checkpoint_node="publish",
        node_run=("publish", "publish"),
    )
    # Leave uncommitted code in the working tree (the failed publish's staged-but-uncommitted work).
    (git_repo.clone / "feature.py").write_text("print('shipped')\n", encoding="utf-8")

    def fake_resume(self: Orchestrator) -> PipelineResult:
        return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(Orchestrator, "resume", fake_resume)
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    assert code == 0  # not refused for a dirty tree; --continue proceeds into publish


def test_rerun_continue_outside_publish_still_refuses_dirty_tree(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F14 regression: outside the publish region a dirty working tree is still unexpected work and
    is refused, so relaxing the gate for publish does not weaken it elsewhere.
    """
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow(
            "task-1",
            "T",
            Status.FAILED,
            source_path=str(source),
            branch="worc/task-1-t",
        ),
        checkpoint_node="review",
        node_run=("review", "evaluator"),
    )
    (git_repo.clone / "stray.py").write_text("x = 1\n", encoding="utf-8")
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    assert code == 1
    assert "unaccounted changes" in capsys.readouterr().out


def test_rerun_continue_revives_then_delegates_to_resume(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "failed" / "task-1.md"
    _complete_task_file(source, "task-1")
    config = _seed(
        project,
        git_repo.clone,
        TaskRow(
            "task-1",
            "T",
            Status.FAILED,
            source_path=str(source),
            branch="worc/task-1-t",
            fix_iterations=2,
            finished_at="2026-01-01T00:00:00+00:00",
            cleanup_completed=True,
        ),
        checkpoint_node="review",  # the engine checkpoint --continue re-enters at
    )

    calls = {"resume": 0}

    def fake_resume(self: Orchestrator) -> PipelineResult:
        calls["resume"] += 1
        return PipelineResult(task_id="task-1", final_status=Status.DONE)

    monkeypatch.setattr(Orchestrator, "resume", fake_resume)
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue", "--yes"])
    assert code == 0  # the stubbed resume returns done
    assert calls["resume"] == 1  # continue delegated to the resume engine

    # The row was revived to the failed stage with the terminal markers cleared, work preserved.
    store = StateStore.open_readonly(git_repo.clone / ".worc" / "state.db")
    row = store.get_task("task-1")
    store.close()
    assert row is not None
    assert row.status is Status.RUNNING  # revived as active; resume re-enters at the checkpoint
    assert row.finished_at is None and row.cleanup_completed is None
    assert row.branch == "worc/task-1-t" and row.fix_iterations == 2
