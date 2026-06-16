"""Tests for the ``finalize`` CLI command — record + tidy a human-handled task (§finalize)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import GitManager
from wastech_orchestrator.ledger import Ledger, LedgerRecord
from wastech_orchestrator.state_store import PublishOpRow, StateStore, TaskRow

_ENV = ["PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"]


def _write_config(project: Path, clone: Path, *, create_pr: bool = False) -> Path:
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
      command: "claude"
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
    branch: str | None = "agent/task-1-t",
    pr_url: str | None = None,
    create_pr: bool = False,
) -> Path:
    """Seed a terminal task (state row + source file) and return the config path."""
    config = _write_config(project, clone, create_pr=create_pr)
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
    git_run(["branch", "agent/task-1-t"], git_repo.clone)  # stray local branch, kept by default

    code = cli.main(["--config", str(config), "finalize", "task-1", "--as", "failed", "--yes"])
    assert code == 1  # failed exit code

    assert _status(git_repo.clone) is Status.FAILED
    records = _ledger_records(git_repo.clone)
    assert len(records) == 1
    assert records[0]["final_status"] == "failed" and records[0]["manual"] is True
    assert (project / "failed" / "task-1.md").exists()  # moved into its lifecycle folder
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    # No commit/push/PR: the branch is kept and not pushed to the remote.
    assert git_run(["ls-remote", "--heads", "origin", "agent/task-1-t"], git_repo.clone) == ""
    assert "agent/task-1-t" in git_run(["branch", "--list", "agent/task-1-t"], git_repo.clone)


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
    git_run(["branch", "agent/task-1-t"], git_repo.clone)

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
    assert git_run(["branch", "--list", "agent/task-1-t"], git_repo.clone) == ""  # deleted


def test_finalize_refuses_while_daemon_running(
    git_repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    config = _seed(project, git_repo.clone)
    monkeypatch.setattr(cli.process_control, "read_pid", lambda _p: 4321)
    monkeypatch.setattr(cli.process_control, "is_running", lambda _pid: True)

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
    assert _status(git_repo.clone) is Status.FAILED  # unchanged
    assert _ledger_records(git_repo.clone) == []
