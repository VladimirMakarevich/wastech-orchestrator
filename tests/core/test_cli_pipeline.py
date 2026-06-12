"""Tests for the ``run`` / ``watch`` CLI wiring (§5.12) and the §14 end-to-end happy path."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from wastech_orchestrator import cli
from wastech_orchestrator.core.orchestrator import PipelineResult
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.observability import logging as obslog
from wastech_orchestrator.state_store import StateStore, TaskRow


@pytest.fixture(autouse=True)
def _reset_package_logger() -> Iterator[None]:
    pkg = logging.getLogger(obslog.LOGGER_NAME)
    saved = pkg.handlers[:]
    pkg.handlers.clear()
    obslog._configured = False
    yield
    for handler in pkg.handlers:
        handler.close()
    pkg.handlers.clear()
    pkg.handlers.extend(saved)
    obslog._configured = False


# --- watch_once unit tests (fake orchestrator) -------------------------------------------


class _FakeOrch:
    def __init__(self, *, resume=None, runs=None) -> None:
        self._resume = resume
        self._runs = list(runs or [])
        self.run_calls: list[str] = []
        self.resume_calls = 0

    def resume(self):
        self.resume_calls += 1
        return self._resume

    def acquire_slot(self, task_id: str) -> bool:
        return True

    def run_task(self, task_file: str):
        self.run_calls.append(task_file)
        return self._runs.pop(0)


def _pending(tmp_path: Path, *names: str) -> Path:
    folder = tmp_path / "pending"
    folder.mkdir()
    for name in names:
        (folder / name).write_text("x", encoding="utf-8")
    return folder


def _done(task_id: str) -> PipelineResult:
    return PipelineResult(task_id=task_id, final_status=Status.DONE)


def test_watch_auto_off_processes_one(make_git_config, git_repo, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, auto_mode=False)
    orch = _FakeOrch(runs=[_done("a"), _done("b")])
    folder = _pending(tmp_path, "a.md", "b.md")
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert len(results) == 1
    assert len(orch.run_calls) == 1  # only the first pending task


def test_watch_auto_on_processes_all(make_git_config, git_repo, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, auto_mode=True)
    orch = _FakeOrch(runs=[_done("a"), _done("b")])
    folder = _pending(tmp_path, "a.md", "b.md")
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert len(results) == 2
    assert len(orch.run_calls) == 2


def test_watch_manual_blocks_continuation(make_git_config, git_repo, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, auto_mode=True)
    manual = PipelineResult(task_id="a", final_status=Status.MANUAL_ACTION_REQUIRED)
    orch = _FakeOrch(runs=[manual, _done("b")])
    folder = _pending(tmp_path, "a.md", "b.md")
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert len(results) == 1  # the manual task blocks the second
    assert results[0].final_status is Status.MANUAL_ACTION_REQUIRED


def test_watch_resume_manual_blocks(make_git_config, git_repo, tmp_path: Path) -> None:
    config = make_git_config(git_repo.clone, auto_mode=True)
    manual = PipelineResult(task_id="r", final_status=Status.MANUAL_ACTION_REQUIRED)
    orch = _FakeOrch(resume=manual, runs=[_done("a")])
    folder = _pending(tmp_path, "a.md")
    results = cli.watch_once(orch, config, folder)  # type: ignore[arg-type]
    assert results == [manual]
    assert orch.run_calls == []  # resume's manual outcome blocks picking pending


# --- end-to-end via main() with fake CLIs ------------------------------------------------


def _write_cli_config(
    project: Path,
    clone: Path,
    *,
    claude_cmd: str,
    codex_cmd: str,
    create_pr: bool = False,
    auto_mode: bool = False,
) -> Path:
    env = ["PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"]
    env_lines = "\n".join(f"    - {e}" for e in env)
    external = project / "external"
    config = project / "config.yaml"
    config.write_text(
        f"""
orchestrator:
  auto_mode:
    enabled: {str(auto_mode).lower()}
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
      command: {claude_cmd!r}
    codex:
      command: {codex_cmd!r}
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
  footprint:
    location: external
    tracking: none
    external_root: {str(external)!r}
""",
        encoding="utf-8",
    )
    return config


def _complete_task_file(path: Path, task_id: str) -> None:
    path.write_text(
        f'---\nid: {task_id}\ntitle: "Add a thing"\nrefined: true\n---\n\n'
        "## Description\n\nDo the thing.\n\n## Acceptance criteria\n\n- works\n",
        encoding="utf-8",
    )


def test_cmd_run_happy_path(
    git_repo, fake_cli, git_run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    claude_cmd = fake_cli("success_edit", "claude")
    codex_cmd = fake_cli("success_edit", "codex")
    config = _write_cli_config(project, git_repo.clone, claude_cmd=claude_cmd, codex_cmd=codex_cmd)
    task_file = project / "task-100.md"
    _complete_task_file(task_file, "task-100")
    operator_log = project / "operator.jsonl"

    code = cli.main(
        [
            "--config",
            str(config),
            "--log-format",
            "json",
            "--log-file",
            str(operator_log),
            "--heartbeat-seconds",
            "0",
            "run",
            str(task_file),
        ]
    )
    assert code == 0
    # One commit on the task branch; the agent's change is committed; back on main.
    assert git_run(["rev-parse", "--abbrev-ref", "HEAD"], git_repo.clone) == "main"
    branch = "agent/task-100-add-a-thing"
    committed = git_run(["show", "--name-only", "--format=", branch], git_repo.clone)
    assert "agent_change.py" in committed
    # Artifacts + exactly one ledger record under the external root.
    external = project / "external"
    assert (external / "logs" / "task-100" / "summary.md").exists()
    ledger_lines = (external / "logs" / "completed.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(ledger_lines) == 1
    assert json.loads(ledger_lines[0])["final_status"] == "done"
    # The task file moved into its lifecycle folder (done), out of the project root.
    assert (project / "done" / "task-100.md").exists()
    messages = {
        json.loads(line)["msg"] for line in operator_log.read_text(encoding="utf-8").splitlines()
    }
    assert {
        "branch preparation started",
        "branch preparation completed",
        "stage started",
        "stage completed",
        "commit started",
        "commit completed",
        "push started",
        "push completed",
        "terminal cleanup started",
        "terminal cleanup completed",
    } <= messages


def test_cmd_status_reports_active_task(
    git_repo, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(
        project,
        git_repo.clone,
        claude_cmd="claude",
        codex_cmd="codex",
    )
    db_path = project / "external" / "state.db"
    store = StateStore.open(db_path)
    store.insert_task(
        TaskRow(
            task_id="task-active",
            title="Active task",
            status=Status.PLANNING,
            branch="agent/task-active-active-task",
            fix_iterations=2,
            updated_at="2026-06-12T10:00:00+00:00",
        )
    )
    store.close()

    code = cli.main(["--config", str(config), "status"])

    assert code == 0
    output = capsys.readouterr().out
    assert "task_id=task-active" in output
    assert "status=planning" in output
    assert "stage=planning" in output
    assert "configured_primary=claude" in output
    assert "branch=agent/task-active-active-task" in output
    assert "fix_iterations=2" in output


def test_cmd_run_rejected_task(git_repo, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _write_cli_config(project, git_repo.clone, claude_cmd="claude", codex_cmd="codex")
    bad = project / "task-bad.md"
    bad.write_text("no front matter\n", encoding="utf-8")

    code = cli.main(["--config", str(config), "run", str(bad)])
    assert code == 1  # failed
    external = project / "external"
    report = external / "logs" / "task-bad" / "validation_report.json"
    assert report.exists()
    assert json.loads(report.read_text(encoding="utf-8"))["reason"] == "frontmatter_missing"
    # Quarantined, and no branch was created.
    assert (project / "rejected" / "task-bad.md").exists()


def test_cmd_watch_auto_mode_two_tasks(
    git_repo, fake_cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    external = project / "external"
    # watch scans tasks/pending under the artifact root (external_root), not the cwd (§21).
    (external / "tasks" / "pending").mkdir(parents=True)
    claude_cmd = fake_cli("success_edit", "claude")
    codex_cmd = fake_cli("success_edit", "codex")
    config = _write_cli_config(
        project, git_repo.clone, claude_cmd=claude_cmd, codex_cmd=codex_cmd, auto_mode=True
    )
    for tid in ("task-201", "task-202"):
        _complete_task_file(external / "tasks" / "pending" / f"{tid}.md", tid)

    monkeypatch.chdir(project)
    code = cli.main(["--config", str(config), "watch"])
    assert code == 0
    ledger_lines = (external / "logs" / "completed.jsonl").read_text(encoding="utf-8").splitlines()
    ids = {json.loads(line)["id"] for line in ledger_lines}
    assert ids == {"task-201", "task-202"}  # both ran sequentially under auto mode
    # Both task files moved out of pending into tasks/done under the artifact root (§20.2, §21).
    for tid in ("task-201", "task-202"):
        assert (external / "tasks" / "done" / f"{tid}.md").exists()
        assert not (external / "tasks" / "pending" / f"{tid}.md").exists()
