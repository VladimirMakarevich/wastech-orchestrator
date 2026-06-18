"""Tests for the ``rerun`` CLI command — fresh re-attempt and ``--continue`` (§rerun)."""

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
  branch_prefix: "agent"
agents:
  allowed: [claude, codex]
  routing:
    refinement: {{primary: claude, fallback: codex}}
    planning: {{primary: claude, fallback: codex}}
    implementation: {{primary: claude, fallback: codex}}
    review: {{primary: codex, fallback: claude}}
    fixing: {{primary: claude, fallback: codex}}
    summary: {{primary: claude, fallback: codex}}
  providers:
    claude:
      command: {claude!r}
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
        f'---\nid: {task_id}\ntitle: "Add a thing"\nrefined: true\n---\n\n'
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


def test_real_failure_persists_interrupted_status(git_repo, fake_cli, tmp_path: Path) -> None:
    """A real terminal failure records the stage it stopped at, so ``--continue`` can re-enter."""
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
            branch="agent/task-700-add-a-thing",
            slug="add-a-thing",
            cleanup_completed=True,
            interrupted_status="planning",
        )
    )
    store.close()
    Ledger(external / "logs").append(
        LedgerRecord(id="task-700", title="Add a thing", final_status="failed", finished_at="t1")
    )
    stale = external / "logs" / "task-700" / "plan.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("stale plan from the failed attempt\n", encoding="utf-8")
    git_run(["branch", "agent/task-700-add-a-thing"], git_repo.clone)  # stale local branch

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
    committed = git_run(
        ["show", "--name-only", "--format=", "agent/task-700-add-a-thing"], git_repo.clone
    )
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


def _seed(project: Path, clone: Path, row: TaskRow, *, checkpoint_node: str | None = None) -> Path:
    config = _write_config(project, clone, claude="claude", codex="codex")
    db = clone / ".worc" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = StateStore.open(db)
    store.insert_task(row)
    if checkpoint_node is not None:  # an interrupted engine task: a flow checkpoint to resume from
        from wastech_orchestrator.core.flow.registry import FlowRegistry

        store.save_flow_checkpoint(
            row.task_id,
            current_node=checkpoint_node,
            counters_json="{}",
            flow_fingerprint=FlowRegistry().resolve("implementation").flow_fingerprint,
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
    config = _seed(project, git_repo.clone, TaskRow("task-1", "T", Status.PLANNING))
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    assert code == 1
    assert "is planning" in capsys.readouterr().out


def test_rerun_refuses_when_daemon_running(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _seed(project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED))
    monkeypatch.setattr(cli.process_control, "read_pid", lambda _p: 4321)
    monkeypatch.setattr(cli.process_control, "is_running", lambda _pid: True)
    code = cli.main(["--config", str(config), "rerun", "task-1"])
    assert code == 1
    assert "watch daemon is running" in capsys.readouterr().out


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
        TaskRow(
            "task-1", "T", Status.FAILED, source_path=str(source), interrupted_status="planning"
        ),
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
    # interrupted_status unset -> continue cannot know where to re-enter.
    config = _seed(
        project, git_repo.clone, TaskRow("task-1", "T", Status.FAILED, source_path=str(source))
    )
    code = cli.main(["--config", str(config), "rerun", "task-1", "--continue"])
    assert code == 1
    assert "no recoverable stage" in capsys.readouterr().out


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
            branch="agent/task-1-t",
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
    assert row.branch == "agent/task-1-t" and row.fix_iterations == 2
