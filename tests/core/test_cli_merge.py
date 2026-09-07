"""CLI surface tests for ``prs`` / ``tasks`` / ``merge-task`` (read-only, refusal, dry-run)."""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator import cli, process_control
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import GitManager
from wastech_orchestrator.state_store import PublishOpRow, StateStore, TaskRow

_ENV = ["PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"]
_URL = "https://github.com/o/r/pull/1"


def _write_config(project: Path, clone: Path) -> Path:
    env_lines = "\n".join(f"    - {e}" for e in _ENV)
    config = project / "config.yaml"
    config.write_text(
        f"""
repo:
  url: "git@example.com:o/r.git"
  local_path: {str(clone)!r}
  base_branch: "main"
  branch_prefix: "worc"
agents:
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
checks:
  command_sets: {{}}
git:
  create_pull_request: true
  pr_base: "main"
""",
        encoding="utf-8",
    )
    return config


def _store(clone: Path) -> StateStore:
    db = clone / ".worc" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    return StateStore.open(db)


def _seed_open_pr(clone: Path, task_id: str = "task-1", status: Status = Status.DONE) -> None:
    store = _store(clone)
    store.insert_task(TaskRow(task_id=task_id, title=task_id, status=status, branch="worc/b"))
    store.record_publish_op(
        PublishOpRow(
            task_id=task_id, kind="pr", fingerprint="worc/b", status="completed", result_ref=_URL
        )
    )
    store.close()


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "project"
    proj.mkdir()
    clone = tmp_path / "clone"
    clone.mkdir()
    return proj, clone


def test_prs_lists_open_prs(project, capsys: pytest.CaptureFixture[str]) -> None:
    proj, clone = project
    config = _write_config(proj, clone)
    _seed_open_pr(clone)

    code = cli.main(["--config", str(config), "prs"])

    assert code == 0
    out = capsys.readouterr().out
    assert "task-1" in out
    assert _URL in out


def test_prs_empty(project, capsys: pytest.CaptureFixture[str]) -> None:
    proj, clone = project
    config = _write_config(proj, clone)
    _store(clone).close()  # create an empty DB

    code = cli.main(["--config", str(config), "prs"])

    assert code == 0
    assert "no open orchestrator PRs" in capsys.readouterr().out


def test_tasks_lists_and_filters(project, capsys: pytest.CaptureFixture[str]) -> None:
    proj, clone = project
    config = _write_config(proj, clone)
    store = _store(clone)
    store.insert_task(TaskRow(task_id="done-1", title="d", status=Status.DONE))
    store.insert_task(TaskRow(task_id="failed-1", title="f", status=Status.FAILED))
    store.close()

    assert cli.main(["--config", str(config), "tasks"]) == 0
    out = capsys.readouterr().out
    assert "done-1" in out and "failed-1" in out

    assert cli.main(["--config", str(config), "tasks", "--status", "done"]) == 0
    out = capsys.readouterr().out
    assert "done-1" in out and "failed-1" not in out


def test_merge_task_refuses_without_recorded_pr(
    project, capsys: pytest.CaptureFixture[str]
) -> None:
    proj, clone = project
    config = _write_config(proj, clone)
    store = _store(clone)
    store.insert_task(TaskRow(task_id="task-1", title="t", status=Status.DONE, branch="worc/b"))
    store.close()

    code = cli.main(["--config", str(config), "merge-task", "task-1"])

    assert code == 1
    assert "no recorded PR" in capsys.readouterr().out


def test_merge_task_dry_run_writes_nothing(
    project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    proj, clone = project
    config = _write_config(proj, clone)
    _seed_open_pr(clone)
    # Avoid a real `gh` network probe; pretend the PR is open.
    monkeypatch.setattr(GitManager, "verify_pr_state", lambda self, url: "OPEN")

    code = cli.main(["--config", str(config), "merge-task", "task-1", "--dry-run"])

    assert code == 0
    out = capsys.readouterr().out
    assert "merge-task plan for task-1" in out
    assert _URL in out
    # Dry-run wrote no merge record.
    store = StateStore.open_readonly(clone / ".worc" / "state.db")
    assert store.get_publish_op("task-1", "pr_merge", None) is None
    store.close()


def _pretend_daemon_running(clone: Path, monkeypatch: pytest.MonkeyPatch, pid: int = 4242) -> None:
    process_control.write_pid_file(process_control.pid_file_path(clone / ".worc"), pid=pid)
    monkeypatch.setattr(process_control, "is_running", lambda pid, **kw: True)


def test_merge_task_dry_run_reports_the_squash_message(
    project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dry run printed status / branch / base / pr / state and "-> merge via 'squash'" — and
    # said nothing about the one thing that lands in the base branch's history forever.
    proj, clone = project
    config = _write_config(proj, clone)
    _seed_open_pr(clone)
    monkeypatch.setattr(GitManager, "verify_pr_state", lambda self, url: "OPEN")

    code = cli.main(["--config", str(config), "merge-task", "task-1", "--dry-run"])

    assert code == 0
    assert "feat(task-1): task-1 (#1)" in capsys.readouterr().out


def test_merge_task_dry_run_is_allowed_while_the_daemon_runs(
    project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guard's own reason — "the merge flow + git ops need the idle slot" — is right for a merge
    # and wrong for a dry run, which mutates nothing. Under auto mode with a human merge gate, the
    # plan is exactly what an operator wants to read without stopping the daemon first.
    proj, clone = project
    config = _write_config(proj, clone)
    _seed_open_pr(clone)
    monkeypatch.setattr(GitManager, "verify_pr_state", lambda self, url: "OPEN")
    _pretend_daemon_running(clone, monkeypatch)

    code = cli.main(["--config", str(config), "merge-task", "task-1", "--dry-run"])

    assert code == 0
    out = capsys.readouterr().out
    assert "merge-task plan for task-1" in out
    assert "watch daemon is running" in out  # said as a note, not as a refusal


def test_merge_task_still_refuses_a_real_merge_while_the_daemon_runs(
    project, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    proj, clone = project
    config = _write_config(proj, clone)
    _seed_open_pr(clone)
    _pretend_daemon_running(clone, monkeypatch)

    code = cli.main(["--config", str(config), "merge-task", "task-1", "-y"])

    assert code == 1
    assert "watch daemon is running" in capsys.readouterr().out
